# RPi Telemetry Implementation — Step by Step

## 📋 สรุปการแก้ไข

### ไฟล์ที่ต้องแก้/เพิ่ม:

| ไฟล์ | สถานะ | ทำไม |
|------|-------|-----|
| `telemetry_sender.py` | ✅ **เพิ่มใหม่** | Module สำหรับส่ง telemetry ไปยัง radio |
| `app.py` | 🔧 **แก้ไข** | ลบ Pixhawk thread, เพิ่ม telemetry sender |
| `sensor_node_improved.ino` | ✅ **มี** (ก่อนแล้ว) | Arduino ส่ง Garmin GPS + sensors |

---

## 🔧 ขั้นตอนการแก้ไข

### **ขั้นที่ 1: เพิ่ม telemetry_sender.py**

```bash
cp /mnt/user-data/outputs/asv_system/telemetry_sender.py ~/asv_system/
```

**ไฟล์นี้ทำไร:**
- สร้าง class `TelemetrySender` ที่เชื่อมต่อกับ Telemetry Radio
- มี method 3 แบบ:
  1. `send_heartbeat()` — บอก Mission Planner ว่า RPi ยังเปิดอยู่
  2. `send_global_position_int()` — ส่ง GPS + Depth
  3. `send_named_value_float()` — ส่ง sensor values (pH, DO, Temp)
  4. `send_all_telemetry()` — ส่งทั้งหมดในครั้งเดียว

---

### **ขั้นที่ 2: แก้ไข app.py**

#### **2.1 แก้ section HEAD (imports)**

**ก่อน:**
```python
from pymavlink import mavutil

import database as db
import mission_manager as mm
from camera import camera_manager
```

**หลัง:**
```python
# ❌ ลบ: from pymavlink import mavutil

import database as db
import mission_manager as mm
from camera import camera_manager
from telemetry_sender import TelemetrySender  # ✅ เพิ่ม
```

---

#### **2.2 แก้ CONFIG section**

**ก่อน:**
```python
PIXHAWK_PORT   = '/dev/ttyACM0'
PIXHAWK_BAUD   = 115200
ARDUINO_BAUD   = 9600
FUSION_INTERVAL = 2.0
USE_SIM        = True
```

**หลัง:**
```python
PIXHAWK_PORT   = '/dev/ttyACM0'     # ❌ ไม่ใช้อีก
PIXHAWK_BAUD   = 115200
ARDUINO_BAUD   = 9600
RADIO_PORT     = '/dev/ttyUSB0'     # ✅ Telemetry Radio
RADIO_BAUD     = 57600              # ✅ Radio baud rate
FUSION_INTERVAL = 2.0
USE_SIM        = True
USE_PIXHAWK    = False              # ✅ ปิด Pixhawk
USE_RPi_TELEMETRY = True            # ✅ เปิด RPi telemetry
```

---

#### **2.3 เพิ่ม telemetry object ที่ global**

**เพิ่มหลังจาก `socketio = SocketIO(...)`:**

```python
# ✅ สร้าง telemetry object ที่ global (ใช้ได้จากทุก thread)
telemetry = TelemetrySender(RADIO_PORT, RADIO_BAUD)
```

---

#### **2.4 เพิ่มสถานะ 'radio_ok' ใน state dictionary**

**ก่อน:**
```python
state = {
    'lat': 0.0, 'lon': 0.0, 'depth': 0.0,
    'pixhawk_ok': False,
    'ph': 0.0, 'do': 0.0, 'temp': 0.0,
    'arduino_ok': False,
    'ts': '',
}
```

**หลัง:**
```python
state = {
    'lat': 0.0, 'lon': 0.0, 'depth': 0.0,
    'pixhawk_ok': False,
    'ph': 0.0, 'do': 0.0, 'temp': 0.0,
    'arduino_ok': False,
    'radio_ok': False,              # ✅ เพิ่ม
    'ts': '',
}
```

---

#### **2.5 ❌ ลบ mavlink_thread() ทั้งฟังก์ชัน**

**ลบออกทั้งหมด:**
```python
# ❌ DELETE THIS ENTIRE FUNCTION (50+ lines):
# def mavlink_thread():
#     global state
#     while True:
#         master = None
#         try:
#             ... (ทั้งหมด)
```

**เพราะ:**
- ไม่ต้องอ่านข้อมูลจาก Pixhawk อีก
- GPS มาจาก Arduino (ซึ่งได้มาจาก Garmin NMEA)
- Depth ก็มาจาก Arduino เช่นกัน

---

#### **2.6 แก้ arduino_thread() ให้อ่าน lat/lon ด้วย**

**เปลี่ยนส่วน data parsing:**

**ก่อน:**
```python
data = json.loads(line)
with lock:
    state['ph']   = round(float(data.get('ph',   state['ph'])),   2)
    state['do']   = round(float(data.get('do',   state['do'])),   2)
    state['temp'] = round(float(data.get('temp', state['temp'])), 2)
```

**หลัง:**
```python
data = json.loads(line)
with lock:
    state['depth'] = round(float(data.get('depth', state['depth'])), 2)
    state['temp'] = round(float(data.get('temp', state['temp'])), 2)
    state['ph'] = round(float(data.get('ph', state['ph'])), 2)
    state['do'] = round(float(data.get('do', state['do'])), 2)
    # ✅ เพิ่ม: อ่าน GPS จาก Arduino (Garmin NMEA)
    state['lat'] = float(data.get('lat', state['lat']))
    state['lon'] = float(data.get('lon', state['lon']))
```

---

#### **2.7 แก้ fusion_thread() ให้ส่ง telemetry**

**ก่อน:**
```python
def fusion_thread():
    while True:
        time.sleep(FUSION_INTERVAL)
        with lock:
            snap = dict(state)
            snap['ts'] = datetime.utcnow().strftime('%H:%M:%S')
            state['ts'] = snap['ts']

        try:
            db.insert_record(...)
        except Exception as e:
            print(f"[FUSION] DB error: {e}")

        socketio.emit('sensor_update', snap)
```

**หลัง:**
```python
def fusion_thread():
    while True:
        time.sleep(FUSION_INTERVAL)
        with lock:
            snap = dict(state)
            snap['ts'] = datetime.utcnow().strftime('%H:%M:%S')
            state['ts'] = snap['ts']

        # บันทึก SQLite
        try:
            db.insert_record(
                snap['lat'], snap['lon'], snap['depth'],
                snap['ph'],  snap['do'],  snap['temp']
            )
        except Exception as e:
            print(f"[FUSION] DB error: {e}")

        # ✅ ส่ง Telemetry ไปยัง Mission Planner ผ่าน radio
        try:
            telemetry.send_all_telemetry(
                snap['lat'], snap['lon'], snap['depth'],
                snap['ph'], snap['do'], snap['temp']
            )
            with lock:
                state['radio_ok'] = telemetry.connected
        except Exception as e:
            print(f"[FUSION] Telemetry error: {e}")
            with lock:
                state['radio_ok'] = False

        # Emit ไปยัง Dashboard (เหมือนเดิม)
        socketio.emit('sensor_update', snap)
```

**อธิบาย:**
- `telemetry.send_all_telemetry()` ส่งข้อมูลทั้งหมดไปยัง Radio
- `state['radio_ok']` ติดตาม status การเชื่อมต่อ
- ส่งไปยัง Dashboard เหมือนเดิม

---

#### **2.8 แก้ sim_thread() ให้ส่ง telemetry ด้วย**

**เพิ่มส่วนนี้ในลูป:**

```python
def sim_thread():
    t = 0
    while True:
        t += 1
        with lock:
            # ... (update state)
            state['radio_ok'] = True  # ✅ ให้ SIM mode ประกอบ radio

        socketio.emit('sensor_update', dict(state))
        try:
            db.insert_record(...)
            
            # ✅ ส่ง telemetry ด้วยในเซ SIM
            if telemetry.connected:
                telemetry.send_all_telemetry(
                    state['lat'], state['lon'], state['depth'],
                    state['ph'], state['do'], state['temp']
                )
        except Exception as e:
            print(f"[SIM] error: {e}")
        time.sleep(FUSION_INTERVAL)
```

---

#### **2.9 แก้ if __name__ == '__main__':**

**ก่อน:**
```python
if __name__ == '__main__':
    db.init_db()

    if USE_SIM:
        print("[APP] *** SIMULATION MODE ***")
        threading.Thread(target=sim_thread, daemon=True).start()
    else:
        threading.Thread(target=mavlink_thread, daemon=True).start()
        threading.Thread(target=arduino_thread, daemon=True).start()
        threading.Thread(target=fusion_thread,  daemon=True).start()

    print("[APP] Server: http://0.0.0.0:5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
```

**หลัง:**
```python
if __name__ == '__main__':
    db.init_db()

    # ✅ ตรวจสอบ telemetry radio ก่อนเริ่ม
    if not telemetry.connected and USE_RPi_TELEMETRY:
        print("[MAIN] ⚠️ WARNING: Telemetry radio not connected!")
        print("[MAIN] Check /dev/ttyUSB0 and baud rate 57600")

    if USE_SIM:
        print("[APP] *** SIMULATION MODE ***")
        threading.Thread(target=sim_thread, daemon=True).start()
    else:
        # ❌ ไม่เปิด mavlink_thread() อีก
        threading.Thread(target=arduino_thread, daemon=True).start()
        threading.Thread(target=fusion_thread,  daemon=True).start()

    print("[APP] Server: http://0.0.0.0:5000")
    print("[APP] Telemetry radio:", "🔗 Connected" if telemetry.connected else "❌ Not connected")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
```

---

## 📝 Arduino ต้องแก้ไขอะไร?

Arduino sketch (`sensor_node_improved.ino`) ต้องใช้ **เดิม** แต่เปลี่ยน `sendToRPi()` ให้ส่ง GPS ด้วย:

```cpp
void sendToRPi() {
    StaticJsonDocument<300> doc;
    
    // ✅ เพิ่ม GPS ที่มาจาก Garmin
    doc["lat"]      = garminLat;      // Garmin GPS
    doc["lon"]      = garminLon;
    doc["depth"]    = round(waterDepth * 100) / 100.0;
    doc["temp"]     = round(waterTemp * 10) / 10.0;
    doc["ph"]       = round(waterPH * 100) / 100.0;
    doc["do"]       = round(waterDO * 100) / 100.0;
    
    serializeJson(doc, Serial);
    Serial.println();
}
```

---

## 🔌 Hardware Setup Checklist

- [ ] RPi USB → Telemetry Radio (57600 baud)
- [ ] Arduino USB → RPi USB (9600 baud)
- [ ] Garmin NMEA → Arduino Serial2 (4800 baud)
- [ ] pH sensor → Arduino A0
- [ ] DO sensor → Arduino (ลูกลอย analog)
- [ ] DS18B20 → Arduino pin 6
- [ ] **❌ ไม่ต่อ Pixhawk USB** (ไม่ต้องใช้)

---

## 🧪 วิธีทดสอบ

### 1. ทดสอบ Telemetry Radio
```bash
# ตรวจสอบ port
ls /dev/ttyUSB*

# ควรเห็น /dev/ttyUSB0
```

### 2. เปิด USE_SIM = True ก่อน
```python
USE_SIM = True
USE_RPi_TELEMETRY = True
```

```bash
python3 app.py
```

**ควรเห็น:**
```
[TELEMETRY] Connected to radio at /dev/ttyUSB0 (57600 baud)
[TELEMETRY] 📍 GPS: 13.7563, 100.5018 | Depth: 2.00m
[TELEMETRY] 📊 pH: 7.00
[TELEMETRY] 📊 DO: 6.50
[TELEMETRY] 📊 Temp: 28.00
[TELEMETRY] ♥ Heartbeat sent
```

### 3. เปิด Mission Planner
```
Add connection → COM port (Ground Radio USB)
Baud: 57600
```

**ควรเห็น:**
- Map: GPS position updates ทุก 2 วินาที
- Data: pH, DO, Temp values
- Status: "System 1" heartbeat

---

## ⚠️ Troubleshooting

| ปัญหา | สาเหตุ | แก้ไข |
|------|-------|-----|
| `[TELEMETRY] ERROR connecting` | Port ผิด หรือ baud ผิด | ตรวจสอบ `/dev/ttyUSB*` และ 57600 |
| Mission Planner ไม่เห็นข้อมูล | Heartbeat ไม่ส่ง | ตรวจสอบ `send_heartbeat()` ถูก call |
| GPS ไม่ update | Arduino ไม่ส่ง lat/lon | เพิ่ม lat/lon ใน JSON จาก Arduino |
| Radio disconnect ประจำ | Cable USB ไม่ดี | ลอง USB cable อื่น |

---

## 📊 Data Flow Chart

```
Arduino (sensors)
  ├─ Garmin (NMEA 0183) → UART2
  ├─ pH sensor (A0)
  ├─ DO sensor (Analog)
  └─ DS18B20 (pin 6)
       ↓ JSON serial
      RPi
       ├─ Read JSON
       ├─ Filter + Fuse
       ├─ Save SQLite
       ├─ Send Telemetry ──→ Telemetry Radio (USB)
       │                           ↓ RF link
       │                      Ground Radio
       │                           ↓
       │                      Mission Planner (PC)
       │
       └─ Dashboard (WebSocket)
            ↓
         Web Browser
```

---

