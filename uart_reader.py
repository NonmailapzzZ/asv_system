"""
uart_reader.py - read Arduino sensor data over UART.

The reader keeps a small status model for the dashboard:
- ok: valid JSON sensor packet received
- warn: bytes arrived but could not be decoded, parsed, or validated
- idle: no bytes waiting on the UART port
- error: UART disconnected or read failure
"""

import json
import logging
import time

import serial

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class ArduinoUARTReader:
    def __init__(self, port='/dev/ttyS0', baudrate=9600, timeout=2):
        self.last_status = 'idle'
        self.last_error = ''

        try:
            self.ser = serial.Serial(port, baudrate, timeout=timeout)
            log.info(f"[UART] Connected to {port} at {baudrate} baud")
            self.connected = True
        except Exception as e:
            log.error(f"[UART] Cannot open {port}: {e}")
            self.connected = False
            self.ser = None
            self.last_status = 'error'
            self.last_error = str(e)

    def read_sensor_data(self):
        """
        Read one UART sensor line.

        Returns:
            dict when a valid packet is received, otherwise None.
        """
        if not self.connected or not self.ser:
            self.last_status = 'error'
            self.last_error = 'UART not connected'
            return None

        try:
            if self.ser.in_waiting <= 0:
                self.last_status = 'idle'
                self.last_error = ''
                return None

            raw = self.ser.readline()
            line = raw.decode('utf-8', errors='strict').strip()

            if not line or not line.startswith('{'):
                self.last_status = 'warn'
                self.last_error = 'Malformed UART line'
                return None

            sensor_data = json.loads(line)
            required = {'lat', 'lon', 'depth', 'temp', 'ph', 'do'}
            if not required.issubset(sensor_data):
                self.last_status = 'warn'
                self.last_error = 'Missing sensor fields'
                return None

            self.last_status = 'ok'
            self.last_error = ''
            log.info(f"[UART] OK {line}")
            return sensor_data

        except UnicodeDecodeError as e:
            log.warning(f"[UART] Decode error: {e}")
            self.last_status = 'warn'
            self.last_error = 'Decode error'
            return None
        except json.JSONDecodeError as e:
            log.warning(f"[UART] JSON parse error: {e}")
            self.last_status = 'warn'
            self.last_error = 'JSON parse error'
            return None
        except Exception as e:
            log.error(f"[UART] Read error: {e}")
            self.connected = False
            self.last_status = 'error'
            self.last_error = str(e)
            return None

    def read_sensors_continuous(self, interval=0.1):
        """Yield valid sensor packets continuously."""
        while True:
            data = self.read_sensor_data()
            if data:
                yield data
            time.sleep(interval)

    def close(self):
        """Close UART connection."""
        if self.ser:
            self.ser.close()
            self.connected = False
            self.last_status = 'error'
            self.last_error = 'UART closed'
            log.info("[UART] Connection closed")


if __name__ == '__main__':
    reader = ArduinoUARTReader('/dev/ttyS0', 9600)

    try:
        print("[UART] Reading sensor data... (Ctrl+C to stop)")
        for data in reader.read_sensors_continuous(interval=1.0):
            if data:
                print(
                    f"Lat: {data['lat']:.4f} | Lon: {data['lon']:.4f} | "
                    f"Depth: {data['depth']:.2f}m | Temp: {data['temp']:.1f}C | "
                    f"pH: {data['ph']:.2f} | DO: {data['do']:.2f} mg/L"
                )
    except KeyboardInterrupt:
        print("\n[UART] Stopped")
        reader.close()
