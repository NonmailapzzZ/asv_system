"""
pixhawk_reader.py — RPi อ่านข้อมูล Pixhawk
"""

import os

# Force MAVLink 2.0
os.environ['MAVLINK20'] = '1'

from pymavlink import mavutil
import time
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ArduRover/ArduBoat Mode Mapping (Standard)
ROVER_MODES = {
    0: 'MANUAL',
    1: 'ACRO',
    3: 'STEERING',
    4: 'HOLD',
    5: 'LOITER',
    6: 'FOLLOW',
    7: 'SIMPLE',
    10: 'AUTO',
    11: 'RTL',
    12: 'SMART_RTL',
    14: 'GUIDED',
    15: 'INITIALIZING'
}

class PixhawkReader:
    def __init__(self, port='/dev/ttyACM0', baud=115200):
        """
        เชื่อมต่อกับ Pixhawk
        """
        self.port = port
        self.baud = baud
        self.master = None
        self.connected = False
        self.mission_items = {} # Cached mission {seq: command_id}
        self.connect()

    def connect(self):
        try:
            self.master = mavutil.mavlink_connection(self.port, baud=self.baud)
            self.master.wait_heartbeat(timeout=10)
            log.info(f"[PIXHAWK] Connected to {self.port} at {self.baud} baud")
            self.connected = True
            
            # Request mission items after connection
            self.download_mission()
            return True
        except Exception as e:
            log.error(f"[PIXHAWK] Cannot connect: {e}")
            self.connected = False
            return False

    def download_mission(self):
        """Download mission list from Pixhawk to identify Delay/Loiter commands."""
        if not self.connected:
            return False
        
        try:
            log.info("[PIXHAWK] Downloading mission...")
            self.master.mav.mission_request_list_send(self.master.target_system, self.master.target_component)
            
            msg = self.master.recv_match(type='MISSION_COUNT', blocking=True, timeout=5)
            if not msg:
                log.warning("[PIXHAWK] No MISSION_COUNT received during download")
                return False
            
            count = msg.count
            self.mission_items = {}
            
            for i in range(count):
                self.master.mav.mission_request_int_send(
                    self.master.target_system, self.master.target_component, i
                )
                item = self.master.recv_match(type=['MISSION_ITEM_INT', 'MISSION_ITEM'], blocking=True, timeout=2)
                if item:
                    self.mission_items[item.seq] = item.command
                else:
                    log.warning(f"[PIXHAWK] Failed to get mission item #{i}")
            
            log.info(f"[PIXHAWK] Mission downloaded: {len(self.mission_items)} items")
            return True
        except Exception as e:
            log.error(f"[PIXHAWK] Mission download error: {e}")
            return False

    def get_mission_command(self, seq):
        """Return the command ID for a given mission sequence number."""
        return self.mission_items.get(seq)

    def read_mode(self):
        """Read Pixhawk flight mode from MAVLink HEARTBEAT."""
        if not self.connected:
            return None

        try:
            msg = self.master.recv_match(type='HEARTBEAT', blocking=False)
            if msg:
                # Custom robust mapping for Rover/Boat
                mode_id = msg.custom_mode
                mode_name = ROVER_MODES.get(mode_id)
                
                if mode_name:
                    return {'mode': mode_name}
                
                # Fallback to library mapping but strip "MODE" prefix
                mode_str = mavutil.mode_string_v10(msg)
                if mode_str:
                    mode_str = mode_str.upper().replace('MODE', '').replace('(', '').replace(')', '').strip()
                    return {'mode': mode_str}
                
                return {'mode': f'UNKNOWN({mode_id})'}
            return None
        except Exception as e:
            log.error(f"[PIXHAWK] Read mode error: {e}")
            return None
    
    def read_gps(self):
        """
        อ่าน GPS location จาก Pixhawk
        
        Returns:
            dict: {"lat": ..., "lon": ..., "alt": ...}
        """
        if not self.connected:
            return None
        
        try:
            # รอข้อมูล GLOBAL_POSITION_INT (GPS)
            msg = self.master.recv_match(
                type='GLOBAL_POSITION_INT',
                blocking=False
            )
            
            if msg:
                return {
                    'lat': msg.lat / 1e7,      # Convert to degrees
                    'lon': msg.lon / 1e7,
                    'alt': msg.relative_alt / 1000.0,  # Convert to meters
                    'hdg': msg.hdg / 100.0     # Heading in degrees
                }
            return None
        except Exception as e:
            log.error(f"[PIXHAWK] Read GPS error: {e}")
            return None
    
    def read_attitude(self):
        """
        อ่าน Attitude (roll, pitch, yaw)
        
        Returns:
            dict: {"roll": ..., "pitch": ..., "yaw": ...}
        """
        if not self.connected:
            return None
        
        try:
            msg = self.master.recv_match(
                type='ATTITUDE',
                blocking=False
            )
            
            if msg:
                return {
                    'roll': msg.roll,
                    'pitch': msg.pitch,
                    'yaw': msg.yaw
                }
            return None
        except Exception as e:
            log.error(f"[PIXHAWK] Read attitude error: {e}")
            return None
    
    def read_battery(self):
        """
        อ่าน Battery status
        
        Returns:
            dict: {"voltage": ..., "current": ..., "remaining": ...}
        """
        if not self.connected:
            return None
        
        try:
            msg = self.master.recv_match(
                type='BATTERY_STATUS',
                blocking=False
            )
            
            if msg:
                return {
                    'voltage': msg.voltages[0] / 1000.0,  # mV to V
                    'current': msg.current_battery / 100.0,  # cA to A
                    'remaining': msg.battery_remaining  # percentage
                }
            return None
        except Exception as e:
            log.error(f"[PIXHAWK] Read battery error: {e}")
            return None
    
    def read_mission_status(self):
        """
        Read mission-related messages.
        
        Returns:
            dict: {
                'mission_current': int,
                'mission_reached': int (optional)
            }
        """
        if not self.connected:
            return None
        
        data = {}
        try:
            # Check for MISSION_CURRENT (the item the boat is currently heading to)
            msg_curr = self.master.recv_match(type='MISSION_CURRENT', blocking=False)
            if msg_curr:
                data['mission_current'] = msg_curr.seq
            
            # Check for MISSION_ITEM_REACHED (the item just completed/arrived at)
            msg_reached = self.master.recv_match(type='MISSION_ITEM_REACHED', blocking=False)
            if msg_reached:
                data['mission_reached'] = msg_reached.seq
                
            return data if data else None
        except Exception as e:
            log.error(f"[PIXHAWK] Read mission status error: {e}")
            return None
    
    def read_all(self):
        """
        อ่านข้อมูลทั้งหมด (GPS + Attitude + Battery + Mode + Mission)
        
        Returns:
            dict: combined data
        """
        data = {}

        mode = self.read_mode()
        if mode:
            data.update(mode)
        
        gps = self.read_gps()
        if gps:
            data.update(gps)
        
        att = self.read_attitude()
        if att:
            data.update(att)
        
        bat = self.read_battery()
        if bat:
            data.update(bat)

        mis = self.read_mission_status()
        if mis:
            data.update(mis)
        
        return data if data else None
    
    def send_command_long(self, command_id, param1=0, param2=0):
        """
        ส่ง command ไปยัง Pixhawk
        
        Args:
            command_id: MAV_CMD enum (e.g., MAV_CMD_DO_CHANGE_MODE)
            param1, param2: parameters
        """
        if not self.connected:
            log.error("[PIXHAWK] Not connected")
            return False
        
        try:
            self.master.mav.command_long_send(
                target_system=1,
                target_component=1,
                command=command_id,
                confirmation=0,
                param1=param1,
                param2=param2,
                param3=0, param4=0, param5=0, param6=0, param7=0
            )
            log.info(f"[PIXHAWK] Command sent: {command_id}")
            return True
        except Exception as e:
            log.error(f"[PIXHAWK] Send command error: {e}")
            return False

    def send_gps_input(self, lat, lon, alt=0, fix_type=3, satellites=10):
        """
        Send external GPS data to Pixhawk (GPS_INPUT #232)
        Note: ArduPilot needs GPS_TYPE=14 (MAV) or similar.
        """
        if not self.connected:
            return False
        
        try:
            # time_usec is usually since boot or epoch
            time_usec = int(time.time() * 1e6)
            
            self.master.mav.gps_input_send(
                time_usec,       # Timestamp (micros since boot or UNIX epoch)
                0,               # ID of the GPS for multiple GPS systems
                0,               # Flags indicating which fields to ignore (0 = use all)
                0,               # GPS week number
                0,               # Time of week
                fix_type,        # 0-1: no fix, 2: 2D fix, 3: 3D fix
                int(lat * 1e7),  # Latitude (WGS84), in degrees * 1E7
                int(lon * 1e7),  # Longitude (WGS84), in degrees * 1E7
                alt,             # Altitude (MSL), in meters
                1.0,             # HDOP
                1.0,             # VDOP
                0,               # velocity north
                0,               # velocity east
                0,               # velocity down
                0.1,             # speed accuracy
                0.1,             # horizontal accuracy
                0.1,             # vertical accuracy
                satellites       # Number of satellites visible
            )
            return True
        except Exception as e:
            log.error(f"[PIXHAWK] Send GPS input error: {e}")
            return False
    
    def set_mode(self, mode_name):
        """
        เปลี่ยน flight mode
        
        Args:
            mode_name: "STABILIZE", "LOITER", "AUTO", etc.
        """
        if not self.connected:
            return False
        
        # Mode ID mapping สำหรับ ArduBoat
        mode_map = {
            'MANUAL': 0,
            'ACRO': 1,
            'STEERING': 3,
            'HOLD': 4,
            'LOITER': 5,
            'FOLLOW': 6,
            'SIMPLE': 7,
            'AUTO': 10,
            'RTL': 11,
            'CIRCLE': 14,
            'GUIDED': 15,
            'INITIALIZING': 16
        }
        
        mode_id = mode_map.get(mode_name.upper())
        if mode_id is None:
            log.error(f"[PIXHAWK] Unknown mode: {mode_name}")
            return False
        
        try:
            self.master.mav.set_mode_send(
                target_system=1,
                base_mode=0,
                custom_mode=mode_id
            )
            log.info(f"[PIXHAWK] Mode changed to: {mode_name}")
            return True
        except Exception as e:
            log.error(f"[PIXHAWK] Set mode error: {e}")
            return False
    
    def arm(self):
        """ติด arm (ยอม motor ตัดไฟ)"""
        return self.send_command_long(
            command_id=400,  # MAV_CMD_COMPONENT_ARM_DISARM
            param1=1  # arm
        )
    
    def disarm(self):
        """ถอด arm"""
        return self.send_command_long(
            command_id=400,
            param1=0  # disarm
        )
    
    def close(self):
        """ปิด connection"""
        if self.master:
            self.master.close()
            self.connected = False
            log.info("[PIXHAWK] Connection closed")


# TEST STANDALONE
if __name__ == '__main__':
    reader = PixhawkReader('/dev/ttyACM0', 115200)
    
    try:
        print("[TEST] Reading Pixhawk data... (Ctrl+C to stop)")
        while True:
            data = reader.read_all()
            if data:
                print(f"GPS: {data.get('lat', 0):.4f}, {data.get('lon', 0):.4f} | "
                      f"Heading: {data.get('hdg', 0):.0f}° | "
                      f"Battery: {data.get('voltage', 0):.2f}V")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[TEST] Stopped")
        reader.close()
