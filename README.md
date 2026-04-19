# ASV System — Autonomous Surface Vehicle Data Fusion

## โครงสร้างไฟล์

```
asv_system/
├── app.py                  # สมองหลัก (Flask + SocketIO + MAVLink + Arduino + Camera)
├── database.py             # SQLite management
├── mission_manager.py       # .waypoints upload to Pixhawk
├── camera.py               # MJPEG webcam streaming
├── dashboard/
│   ├── index.html          # HTML เดียว ครบทั้ง dashboard
│   ├── style.css           # Marine dark theme
│   └── charts.js           # Chart.js real-time (Depth, pH, DO)
├── uploads/                # ไฟล์ .waypoints ที่อัปโหลด
├── logs/                   # CSV export + SQLite DB
└── README.md
```

## ติดตั้ง

```bash
pip install flask flask-socketio pymavlink pyserial eventlet opencv-python-headless
```

## รันแบบ Simulation (ทดสอบไม่ต้องต่อ hardware)

```bash
cd asv_system
python3 app.py
```

เปิด browser ไปที่ `http://0.0.0.0:5000`

## Configuration

แก้ไข `app.py` บรรทัดที่มี:

```python
PIXHAWK_PORT   = '/dev/ttyACM0'   # ลอง /dev/ttyACM0, /dev/ttyACM1, /dev/ttyUSB0
PIXHAWK_BAUD   = 115200
ARDUINO_BAUD   = 9600             # ต้องตรงกับ Arduino sketch
USE_SIM        = True              # เปลี่ยนเป็น False เมื่อใช้ hardware จริง
```

## Arduino Sketch (ตัวอย่าง)

Arduino ต้องส่งข้อมูลเป็น JSON ผ่าน Serial:

```cpp
#include <ArduinoJson.h>

void setup() {
  Serial.begin(9600);
}

void loop() {
  StaticJsonDocument<200> doc;
  doc["ph"]   = readPH();
  doc["do"]   = readDO();
  doc["temp"] = readTemp();
  
  serializeJson(doc, Serial);
  Serial.println();
  
  delay(1000);
}
```

## Features

- **Real-time Dashboard**: Gauge + Charts (Depth, pH, DO)
- **Camera Streaming**: MJPEG webca, เลือก channel, show/hide ได้
- **GPS + Garmin Depth**: เชื่อม Google Maps โดยตรง
- **Mission Upload**: .waypoints → Pixhawk + Verify
- **Data Logging**: SQLite + CSV export
- **ไม่ Crash**: ถ้ากล้องหลุด hardware disconnect ก็ไม่เสีย dashboard

## Troubleshooting

**กล้องไม่เจอ**: กด Scan > เลือก Camera จากไปdown > กด Open

**Pixhawk ไม่เชื่อม**: ตรวจสอบ port ด้วย `ls /dev/ttyACM*` และ baudrate

**Arduino ไม่อ่านค่า**: ตรวจสอบว่า JSON format ถูกต้อง ด้วย Serial Monitor

**Dashboard กระพริบ**: เปลี่ยน `USE_SIM = True` เพื่อทดสอบ ถ้าจำลองไม่มีปัญหาแสดงว่า hardware ไม่พร้อม

