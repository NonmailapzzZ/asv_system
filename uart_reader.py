"""
uart_reader.py — RPi อ่านข้อมูล Arduino ผ่าน UART

ใช้กับ sensor_node_uart.ino
"""

import serial
import json
import time
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class ArduinoUARTReader:
    def __init__(self, port='/dev/ttyS0', baudrate=9600, timeout=2):
        """
        เชื่อมต่อกับ Arduino ผ่าน UART
        
        Args:
            port: UART port (RPi ใช้ /dev/ttyS0)
            baudrate: 9600 (ต้องตรงกับ Arduino)
            timeout: timeout for read
        """
        try:
            self.ser = serial.Serial(port, baudrate, timeout=timeout)
            log.info(f"[UART] Connected to {port} at {baudrate} baud")
            self.connected = True
        except Exception as e:
            log.error(f"[UART] Cannot open {port}: {e}")
            self.connected = False
            self.ser = None
    
    def read_sensor_data(self):
        """
        อ่านข้อมูล sensor จาก Arduino (รอบเดียว)
        
        Returns:
            dict: {"lat": ..., "lon": ..., "depth": ..., "temp": ..., "ph": ..., "do": ...}
            None: ถ้าไม่มีข้อมูล
        """
        if not self.connected or not self.ser:
            return None
        
        try:
            if self.ser.in_waiting > 0:
                # อ่าน 1 line (จบด้วย \n)
                line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                
                if not line or line.startswith('{') is False:
                    return None
                
                # Parse JSON
                sensor_data = json.loads(line)
                log.info(f"[UART] ✓ {line}")
                return sensor_data
            
            return None
            
        except json.JSONDecodeError as e:
            log.warning(f"[UART] JSON parse error: {e}")
            return None
        except Exception as e:
            log.error(f"[UART] Read error: {e}")
            self.connected = False
            return None
    
    def read_sensors_continuous(self, interval=0.1):
        """
        อ่าน sensor ซ้ำๆ
        
        Yields:
            dict: sensor data
        """
        while True:
            data = self.read_sensor_data()
            if data:
                yield data
            time.sleep(interval)
    
    def close(self):
        """ปิด connection"""
        if self.ser:
            self.ser.close()
            self.connected = False
            log.info("[UART] Connection closed")


# TEST STANDALONE
if __name__ == '__main__':
    reader = ArduinoUARTReader('/dev/ttyS0', 9600)
    
    try:
        print("[UART] Reading sensor data... (Ctrl+C to stop)")
        for data in reader.read_sensors_continuous(interval=1.0):
            if data:
                print(f"Lat: {data['lat']:.4f} | Lon: {data['lon']:.4f} | "
                      f"Depth: {data['depth']:.2f}m | Temp: {data['temp']:.1f}°C | "
                      f"pH: {data['ph']:.2f} | DO: {data['do']:.2f} mg/L")
    except KeyboardInterrupt:
        print("\n[UART] Stopped")
        reader.close()
