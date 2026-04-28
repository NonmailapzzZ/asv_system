"""
telemetry_sender.py — RPi sends sensor data to Mission Planner via telemetry radio

วิธีทำงาน:
1. RPi อ่านข้อมูลจาก Arduino (pH, DO, Temp, Depth)
2. RPi รับ GPS จาก Pixhawk (optional) หรือเซนเซอร์อื่น
3. RPi จัดรูปแบบข้อมูลเป็น MAVLink protocol
4. RPi ส่งผ่าน Telemetry Radio (USB serial) ไปยัง Ground Station
5. Ground Station ส่งต่อไปยัง Mission Planner

Schema:
- GLOBAL_POSITION_INT: GPS (lat/lon) + Depth (เป็น relative_alt)
- NAMED_VALUE_FLOAT: pH, DO, Temperature (เป็นค่า custom)
- HEARTBEAT: ให้ Mission Planner รู้ว่า RPi ยังทำงานอยู่
"""

from pymavlink import mavutil
import time
import threading


class TelemetrySender:
    def __init__(self, radio_port='/dev/ttyUSB0', baud=57600):
        """
        สร้าง connection ไปยัง Telemetry Radio
        
        Args:
            radio_port: USB port ที่ต่อ Telemetry Radio (เช่น /dev/ttyUSB0 หรือ COM3)
            baud: Baud rate ของ radio (ปกติคือ 57600)
        
        ⚠️ IMPORTANT: ต่อ RPi ไปยัง Telemetry Radio นี้เท่านั้น
        ไม่ควร connect Pixhawk + RPi ที่ radio เดียวกันเพราะจะ conflict
        """
        try:
            self.mav = mavutil.mavlink_connection(radio_port, baud=baud)
            print(f"[TELEMETRY] Connected to radio at {radio_port} ({baud} baud)")
            self.connected = True
        except Exception as e:
            print(f"[TELEMETRY] ERROR connecting to {radio_port}: {e}")
            self.connected = False
            self.mav = None
        
        # System configuration
        self.source_system = 1          # RPi = system 1 (เอกลักษณ์ของ RPi)
        self.source_component = 0       # Generic component
        
        # ใช้สำหรับ tracking heartbeat
        self.heartbeat_interval = 1.0   # วินาที
        self.last_heartbeat = time.time()

    def send_heartbeat(self):
        """
        ส่ง HEARTBEAT message ให้ Mission Planner รู้ว่า RPi ยังเปิดอยู่
        
        อธิบาย:
        - type=6: MAV_TYPE_SURFACE_BOAT (บอก MP ว่านี่คือ boat)
        - system_status=4: MAV_STATE_ACTIVE (ระบุว่า active)
        - ถ้าไม่ส่ง heartbeat ประมาณ 3 วินาที Mission Planner จะ disconnect
        """
        if not self.connected:
            return
        
        try:
            self.mav.mav.heartbeat_send(
                type=6,                         # MAV_TYPE_SURFACE_BOAT
                autopilot=0,                    # MAV_AUTOPILOT_GENERIC
                base_mode=0,
                custom_mode=0,
                system_status=4                 # MAV_STATE_ACTIVE
            )
            self.last_heartbeat = time.time()
            print(f"[TELEMETRY] ♥ Heartbeat sent")
        except Exception as e:
            print(f"[TELEMETRY] ERROR sending heartbeat: {e}")
            self.connected = False

    def send_global_position_int(self, lat, lon, depth, altitude=0):
        """
        ส่ง GPS position และ Depth ไปยัง Mission Planner
        Mission Planner จะแสดงตำแหน่งบน Map
        
        Args:
            lat: Latitude (-90 ถึง 90 degrees)
            lon: Longitude (-180 ถึง 180 degrees)
            depth: ความลึกน้ำ (เมตร) - แสดงเป็น relative_alt
            altitude: ความสูง (เมตร) - ปกติใช้ 0 สำหรับ boat
        
        อธิบาย MAVLink format:
        - lat/lon ต้อง convert เป็น int × 1e7 (เพราะ MAVLink ใช้ integer)
        - depth ใช้ relative_alt (altitude) field
        - vx, vy, vz = 0 (boat ไม่เคลื่อนที่)
        - hdg = 0 (heading unknown)
        
        ตัวอย่าง: lat=13.7563 → 137563000 (int)
        """
        if not self.connected:
            return
        
        try:
            self.mav.mav.global_position_int_send(
                time_boot_ms=int(time.time() * 1000),  # Timestamp (milliseconds)
                lat=int(lat * 1e7),                     # Latitude × 10^7
                lon=int(lon * 1e7),                     # Longitude × 10^7
                alt=int(altitude * 1000),               # Altitude in mm (ปล่อย 0)
                relative_alt=int(depth * 1000),         # ใช้นี่เป็น Depth (mm)
                vx=0, vy=0, vz=0,                       # Velocity = 0 (stationary)
                hdg=0,                                  # Heading = unknown
                ack=0
            )
            print(f"[TELEMETRY] 📍 GPS: {lat:.4f}, {lon:.4f} | Depth: {depth:.2f}m")
        except Exception as e:
            print(f"[TELEMETRY] ERROR sending position: {e}")
            self.connected = False

    def send_named_value_float(self, name, value):
        """
        ส่ง Sensor value (pH, DO, Temperature) ผ่าน NAMED_VALUE_FLOAT
        Mission Planner จะแสดงค่าเหล่านี้ใน Data panel
        
        Args:
            name: ชื่อ sensor (เช่น "pH", "DO", "Temp")
            value: ค่า sensor (float)
        
        อธิบาย:
        - name ต้อง pad ให้เป็น 10 bytes (MAVLink กำหนด)
        - ถ้า name < 10 chars ต้อง fill null bytes
        - ตัวอย่าง: "pH" → "pH\x00\x00\x00\x00\x00\x00\x00\x00" (10 bytes)
        - Mission Planner จะ parse และแสดง name + value
        """
        if not self.connected:
            return
        
        try:
            # Pad name to exactly 10 bytes with null characters
            # name.ljust(10, '\x00') = ถ้า name < 10 ให้เพิ่ม null bytes
            # [:10] = ตัดให้เหลือแค่ 10 bytes
            # .encode() = convert string เป็น bytes
            name_padded = name.ljust(10, '\x00')[:10].encode()
            
            self.mav.mav.named_value_float_send(
                time_boot_ms=int(time.time() * 1000),
                name=name_padded,                       # Sensor name (10 bytes)
                value=float(value)                      # Sensor value
            )
            print(f"[TELEMETRY] 📊 {name}: {value:.2f}")
        except Exception as e:
            print(f"[TELEMETRY] ERROR sending {name}: {e}")

    def send_all_telemetry(self, lat, lon, depth, ph, do_val, temp):
        """
        ส่งข้อมูลทั้งหมดในครั้งเดียว
        
        ใช้ฟังก์ชันนี้ในลูป data fusion เพื่อส่งทั้ง GPS + Sensors
        
        Args: ทั้งหมดเป็น sensor data
        """
        if not self.connected:
            return
        
        # ส่ง GPS position
        self.send_global_position_int(lat, lon, depth)
        
        # ส่ง sensor values
        self.send_named_value_float('pH', ph)
        self.send_named_value_float('DO', do_val)
        self.send_named_value_float('Temp', temp)
        
        # ส่ง heartbeat ทุก 10 ครั้ง (~20 วินาที)
        if time.time() - self.last_heartbeat >= self.heartbeat_interval:
            self.send_heartbeat()

    def check_connection(self):
        """
        ตรวจสอบว่า radio ยังเชื่อมต่ออยู่หรือไม่
        ใช้สำหรับ error handling
        """
        if not self.connected:
            try:
                # ลองเชื่อมต่ออีกครั้ง
                self.mav.wait_heartbeat(timeout=1)
                self.connected = True
                print("[TELEMETRY] ✅ Reconnected!")
            except:
                self.connected = False
        return self.connected
