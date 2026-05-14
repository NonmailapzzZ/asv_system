"""
sampling_manager.py — จัดการการเก็บข้อมูล sensor เฉพาะตอน Delay

วิธีทำงาน:
1. Pixhawk สั่ง DO_SET_SERVO (Delay) → Arduino รับรู้ → ส่ง "sampling_start"
2. RPi ตั้ง sampling_active = True
3. เก็บค่า sensor แต่ยังไม่ add ลง SQL
4. จนกว่า Delay หมด (Pixhawk สั่ง อีกครั้ง)
5. คำนวณค่าเฉลี่ย → add ลง SQL พร้อม metadata
"""

import time
import json
import threading
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class SamplingManager:
    def __init__(self):
        """
        จัดการการสุ่มค่า sensor
        
        States:
        - IDLE: ไม่เก็บข้อมูล
        - SAMPLING: กำลังเก็บข้อมูล
        - DONE: เก็บเสร็จแล้ว รอ add ลง SQL
        """
        self.sampling_active = False
        self.sampling_start_time = None
        self.sampling_duration = 0  # วินาที
        
        # เก็บค่า sensor แต่ละชุด
        self.samples = []  # [
                          #   {"lat": ..., "lon": ..., "depth": ..., ...},
                          #   {"lat": ..., "lon": ..., "depth": ..., ...},
                          #   ...
                          # ]
        
        self.lock = threading.Lock()
    
    # ===== SAMPLING CONTROL =====
    
    def start_sampling(self, duration_seconds=30):
        """
        เริ่มเก็บข้อมูล (เรียกเมื่อ Pixhawk สั่ง Delay)
        
        Args:
            duration_seconds: ระยะเวลา sampling (วินาที)
        
        ผลลัพธ์:
        - sampling_active = True
        - samples = [] (ล้างค่าเก่า)
        - เริ่มรับค่า sensor
        """
        with self.lock:
            self.sampling_active = True
            self.sampling_start_time = time.time()
            self.sampling_duration = duration_seconds
            self.samples = []  # ล้างค่าเก่า
            
            log.info(f"[SAMPLING] Started for {duration_seconds}s")
            log.info(f"[SAMPLING] Sampling active: {self.sampling_active}")
    
    def stop_sampling(self):
        """
        หยุดเก็บข้อมูล (เรียกเมื่อ Delay หมด หรือสั่ง stop manual)
        
        ผลลัพธ์:
        - sampling_active = False
        - samples อยู่เหมือนเดิม (รอ process)
        - คืนค่า summary (count, avg, std)
        """
        with self.lock:
            self.sampling_active = False
            elapsed = time.time() - self.sampling_start_time
            count = len(self.samples)
            
            log.info(f"[SAMPLING] Stopped")
            log.info(f"[SAMPLING] Collected {count} samples in {elapsed:.1f}s")
            
            return {
                'count': count,
                'elapsed': elapsed,
                'samples': list(self.samples)
            }
    
    # ===== ADD SENSOR DATA =====
    
    def add_sample(self, sensor_data):
        """
        เพิ่มค่า sensor ลงใน buffer (เรียกทุก 1 วินาทีจาก sensor_thread)
        
        Args:
            sensor_data: dict = {
                "lat": 13.7563,
                "lon": 100.5018,
                "depth": 1.25,
                "temp": 28.50,
                "ph": 7.40,
                "do": 6.20
            }
        
        Logic:
        - ถ้า sampling_active = True → เพิ่มลง samples
        - ถ้า sampling_active = False → ข้าม (ไม่เก็บ)
        """
        if not self.sampling_active:
            log.debug(f"[SAMPLER] Sampling not active, skipping sample (active={self.sampling_active})")
            return  # ไม่รับค่า ถ้าไม่ได้อยู่ใน sampling mode
        
        with self.lock:
            # เพิ่ม timestamp ให้กับแต่ละ sample
            sample = dict(sensor_data)
            sample['timestamp'] = datetime.utcnow().isoformat()
            
            self.samples.append(sample)
            
            log.debug(f"[SAMPLING] Added sample #{len(self.samples)}: "
                     f"depth={sample.get('depth', 0):.2f}m, "
                     f"temp={sample.get('temp', 0):.1f}°C")
    
    # ===== CALCULATE & EXPORT =====
    
    def get_average_sample(self):
        """
        คำนวณค่าเฉลี่ยของทั้งหมด samples
        
        Returns:
            dict: {
                "lat": avg,
                "lon": avg,
                "depth": avg,
                "temp": avg,
                "ph": avg,
                "do": avg,
                "sample_count": N,
                "std_dev": {...}
            }
        
        ใช้สำหรับ:
        - บันทึก 1 record ลง SQL แทนการบันทึก N records
        - ลด size ของ database
        """
        with self.lock:
            if not self.samples:
                log.warning("[SAMPLING] No samples collected")
                return None
            
            # Calculate average
            avg_data = {
                'lat': sum(s.get('lat', 0) for s in self.samples) / len(self.samples),
                'lon': sum(s.get('lon', 0) for s in self.samples) / len(self.samples),
                'depth': sum(s.get('depth', 0) for s in self.samples) / len(self.samples),
                'temp': sum(s.get('temp', 0) for s in self.samples) / len(self.samples),
                'ph': sum(s.get('ph', 0) for s in self.samples) / len(self.samples),
                'do': sum(s.get('do', 0) for s in self.samples) / len(self.samples),
            }
            
            # Calculate std dev
            std_data = {}
            for key in ['depth', 'temp', 'ph', 'do']:
                values = [s.get(key, 0) for s in self.samples]
                mean = avg_data[key]
                variance = sum((x - mean) ** 2 for x in values) / len(values)
                std_data[key] = variance ** 0.5
            
            return {
                'lat': round(avg_data['lat'], 4),
                'lon': round(avg_data['lon'], 4),
                'depth': round(avg_data['depth'], 2),
                'temp': round(avg_data['temp'], 1),
                'ph': round(avg_data['ph'], 2),
                'do': round(avg_data['do'], 2),
                'sample_count': len(self.samples),
                'std_depth': round(std_data['depth'], 3),
                'std_temp': round(std_data['temp'], 3),
                'std_ph': round(std_data['ph'], 3),
                'std_do': round(std_data['do'], 3),
            }
    
    def get_all_samples(self):
        """
        คืนค่า samples ทั้งหมด (สำหรับ export ถ้าต้องการ detail)
        """
        with self.lock:
            return list(self.samples)
    
    def clear_samples(self):
        """
        ล้างค่า samples (หลังบันทึก SQL แล้ว)
        """
        with self.lock:
            count = len(self.samples)
            self.samples = []
            log.info(f"[SAMPLING]  Cleared {count} samples")
    
    # ===== STATUS =====
    
    def get_status(self):
        """
        ส่งสถานะ sampling
        
        Returns:
            dict: {
                "active": bool,
                "elapsed": float (วินาที),
                "remaining": float (วินาที),
                "sample_count": int,
                "progress": float (0-100%)
            }
        """
        with self.lock:
            if not self.sampling_active:
                return {
                    'active': False,
                    'elapsed': 0,
                    'remaining': 0,
                    'sample_count': len(self.samples),
                    'progress': 0
                }
            
            elapsed = time.time() - self.sampling_start_time
            remaining = max(0, self.sampling_duration - elapsed)
            progress = min(100, (elapsed / self.sampling_duration) * 100) if self.sampling_duration > 0 else 0
            
            return {
                'active': True,
                'elapsed': round(elapsed, 1),
                'remaining': round(remaining, 1),
                'duration': self.sampling_duration,
                'sample_count': len(self.samples),
                'progress': round(progress, 1)
            }


# TEST STANDALONE
if __name__ == '__main__':
    sampler = SamplingManager()
    
    # Simulate sampling
    print("[TEST] Starting sampling...")
    sampler.start_sampling(duration_seconds=10)
    
    # Simulate sensor data
    for i in range(15):
        fake_data = {
            'lat': 13.7563 + i * 0.0001,
            'lon': 100.5018 + i * 0.0001,
            'depth': 1.25 + (i % 3) * 0.1,
            'temp': 28.5 + (i % 2) * 0.5,
            'ph': 7.4 + (i % 2) * 0.05,
            'do': 6.2 + (i % 2) * 0.1,
        }
        sampler.add_sample(fake_data)
        
        status = sampler.get_status()
        print(f"[TEST] {status['sample_count']} samples, "
              f"progress: {status['progress']:.0f}%, "
              f"remaining: {status['remaining']:.1f}s")
        
        time.sleep(1)
    
    # Stop sampling
    result = sampler.stop_sampling()
    print(f"\n[TEST] Sampling stopped")
    print(f"[TEST] Total samples: {result['count']}")
    print(f"[TEST] Elapsed: {result['elapsed']:.1f}s")
    
    # Calculate average
    avg = sampler.get_average_sample()
    print(f"\n[TEST] Average values:")
    print(f"  Lat: {avg['lat']:.4f}")
    print(f"  Lon: {avg['lon']:.4f}")
    print(f"  Depth: {avg['depth']:.2f}m ± {avg['std_depth']:.3f}")
    print(f"  Temp: {avg['temp']:.1f}°C ± {avg['std_temp']:.3f}")
    print(f"  pH: {avg['ph']:.2f} ± {avg['std_ph']:.3f}")
    print(f"  DO: {avg['do']:.2f} mg/L ± {avg['std_do']:.3f}")
