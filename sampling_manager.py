"""
sampling_manager.py — จัดการการสุ่มค่าเซนเซอร์อย่างฉลาด

- ส่งคำสั่ง START_SAMPLING ไปยัง Arduino
- รอให้เรือหยุดและเซนเซอร์เสถียร
- เก็บเฉพาะค่าที่เสถียร
- บันทึก metadata (stability score, elapsed time)
"""

import json
import time
import serial
import threading

class SamplingManager:
    def __init__(self, serial_port, baud_rate=9600):
        self.ser = serial.Serial(serial_port, baud_rate, timeout=2)
        self.sampling_active = False
        self.sample_data = []
        self.lock = threading.Lock()

    def start_sampling(self, duration_seconds=30):
        """
        ส่งคำสั่งให้ Arduino เริ่มสุ่มค่า
        duration: ระยะเวลาที่จะเก็บค่า (วินาที)
        """
        cmd = {
            "cmd": "START_SAMPLING",
            "duration": duration_seconds * 1000  # convert to ms
        }
        try:
            self.ser.write((json.dumps(cmd) + "\n").encode())
            print(f"[SAMPLING] Started for {duration_seconds}s")
            self.sampling_active = True
            self.sample_data = []
            return True
        except Exception as e:
            print(f"[SAMPLING] Error: {e}")
            return False

    def stop_sampling(self):
        """หยุดการสุ่มค่า"""
        cmd = {"cmd": "STOP_SAMPLING"}
        try:
            self.ser.write((json.dumps(cmd) + "\n").encode())
            self.sampling_active = False
            print("[SAMPLING] Stopped")
            return True
        except Exception as e:
            print(f"[SAMPLING] Error: {e}")
            return False

    def read_samples(self):
        """อ่าน data จาก Arduino พร้อม metadata"""
        while self.sampling_active or len(self.sample_data) < 1:
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode('utf-8').strip()
                    if line:
                        try:
                            data = json.loads(line)
                            
                            # ถ้า sampling จบ
                            if data.get("status") == "SAMPLING_DONE":
                                self.sampling_active = False
                                break
                            
                            # เก็บเฉพาะค่าที่เสถียร
                            if data.get("sampling") and \
                               data.get("temp_stable") and \
                               data.get("ph_stable"):
                                with self.lock:
                                    self.sample_data.append({
                                        'depth': data.get('depth', 0),
                                        'temp': data.get('temp', 0),
                                        'ph': data.get('ph', 0),
                                        'timestamp': time.time(),
                                        'elapsed_ms': data.get('elapsed_ms', 0),
                                        'quality': 'STABLE'
                                    })
                                print(f"[SAMPLING] ✓ Stable: T={data['temp']}°C, pH={data['ph']}, "
                                      f"Depth={data['depth']}m")
                            else:
                                # Debug: แสดงสถานะการ stabilize
                                if data.get("sampling"):
                                    print(f"[SAMPLING] Stabilizing... "
                                          f"Temp: {data.get('temp_count')}/{5} stable, "
                                          f"pH: {data.get('ph_count')}/{5} stable")
                        except json.JSONDecodeError:
                            pass
                else:
                    time.sleep(0.1)
            except Exception as e:
                print(f"[SAMPLING] Read error: {e}")
                time.sleep(0.5)

    def get_average_sample(self):
        """คืนค่าเฉลี่ยของ sample ทั้งหมด"""
        with self.lock:
            if not self.sample_data:
                return None
            
            avg = {
                'depth': sum(s['depth'] for s in self.sample_data) / len(self.sample_data),
                'temp': sum(s['temp'] for s in self.sample_data) / len(self.sample_data),
                'ph': sum(s['ph'] for s in self.sample_data) / len(self.sample_data),
                'sample_count': len(self.sample_data),
                'quality': 'AVERAGED'
            }
            return avg

    def save_samples_csv(self, filename):
        """บันทึก sample ลง CSV"""
        import csv
        with self.lock:
            if not self.sample_data:
                return False
            
            try:
                with open(filename, 'w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=['timestamp', 'depth', 'temp', 'ph', 'elapsed_ms', 'quality'])
                    writer.writeheader()
                    writer.writerows(self.sample_data)
                print(f"[SAMPLING] Saved {len(self.sample_data)} samples to {filename}")
                return True
            except Exception as e:
                print(f"[SAMPLING] Save error: {e}")
                return False
