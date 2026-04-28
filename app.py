"""
app_telemetry_version.py — แก้ไข app.py เพื่อใช้ RPi Telemetry แทน Pixhawk

เปลี่ยนแปลงหลัก:
1. ลบ mavlink_thread() (Pixhawk connection) ออก
2. เพิ่ม telemetry_sender module
3. แก้ fusion_thread() ให้ส่ง telemetry ไปยัง radio
4. เก็บการรับข้อมูล GPS/Depth จาก Arduino + Garmin

ตัวอย่างการแก้ไข:
"""

# ===== ที่ HEAD ของ app.py แก้ไขให้เป็น: =====

import os, time, json, threading, serial, serial.tools.list_ports, random, math
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_socketio import SocketIO
# ❌ ลบ: from pymavlink import mavutil

import database as db
import mission_manager as mm
from camera import camera_manager
from telemetry_sender import TelemetrySender  # ✅ เพิ่มบรรทัดนี้

# ---------------------------------------------------------------------------
# CONFIG — แก้ไขส่วนนี้
# ---------------------------------------------------------------------------
PIXHAWK_PORT   = '/dev/ttyACM0'     # ❌ ไม่ต้องใช้ Pixhawk อีก
PIXHAWK_BAUD   = 115200
ARDUINO_BAUD   = 9600
RADIO_PORT     = '/dev/ttyUSB0'     # ✅ เพิ่มบรรทัดนี้ — Telemetry Radio port
RADIO_BAUD     = 57600              # ✅ เพิ่มบรรทัดนี้ — Telemetry Radio baud
FUSION_INTERVAL = 2.0
USE_SIM        = True
USE_PIXHAWK    = False              # ✅ เปลี่ยนเป็น False (ไม่ใช้ Pixhawk)
USE_RPi_TELEMETRY = True            # ✅ เพิ่มบรรทัดนี้ — เปิด RPi telemetry
# ---------------------------------------------------------------------------

app = Flask(__name__, 
            template_folder=os.path.join(os.path.dirname(__file__), 'dashboard'),
            static_folder=os.path.join(os.path.dirname(__file__), 'dashboard'))
app.config['SECRET_KEY'] = 'asv-secret-2024'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

lock = threading.Lock()
state = {
    'lat': 0.0, 'lon': 0.0, 'depth': 0.0,
    'pixhawk_ok': False,            # ❌ ไม่ใช้แล้ว
    'ph': 0.0, 'do': 0.0, 'temp': 0.0,
    'arduino_ok': False,
    'radio_ok': False,              # ✅ เพิ่มเพื่อบ่งชี้สถานะ radio
    'ts': '',
}

# ✅ สร้าง telemetry object ที่ global
telemetry = TelemetrySender(RADIO_PORT, RADIO_BAUD)

# ---------------------------------------------------------------------------
# ❌ ลบ mavlink_thread() ทั้งฟังก์ชัน (เพราะไม่ใช้ Pixhawk อีก)
# ---------------------------------------------------------------------------
# OLD CODE (ลบออก):
# def mavlink_thread():
#     ... (ทั้งฟังก์ชัน 50+ lines)

# REPLACE WITH: Arduino reads GPS + Depth via Garmin NMEA
# (Arduino already handles this in sensor_node_improved.ino)

# ---------------------------------------------------------------------------
# THREAD B — Arduino (เหมือนเดิม แต่ Arduino จะส่ง depth มาด้วย)
# ---------------------------------------------------------------------------

def find_arduino_port():
    """หาปลั้ก Arduino"""
    for p in serial.tools.list_ports.comports():
        if 'Arduino' in (p.description or '') or 'CH340' in (p.description or '') \
                or 'ttyUSB' in (p.device or ''):
            return p.device
    return None

def arduino_thread():
    """
    อ่านข้อมูลจาก Arduino (pH, DO, Temp, Depth)
    
    อธิบาย:
    - Arduino ส่งมา JSON: {"depth":1.25, "ph":7.4, "do":6.2, "temp":28.5}
    - RPi อ่านข้อมูลนี้เก็บลง state
    - ใช้ GPS จาก Arduino เพราะ Arduino มี Garmin NMEA parser
    """
    global state
    while True:
        port = find_arduino_port()
        if not port:
            print("[ARD] Arduino not found — retry in 5s")
            with lock:
                state['arduino_ok'] = False
            time.sleep(5)
            continue

        try:
            print(f"[ARD] Connecting to Arduino on {port}...")
            ser = serial.Serial(port, ARDUINO_BAUD, timeout=2)
            time.sleep(2)
            print(f"[ARD] Connected!")
            with lock:
                state['arduino_ok'] = True

            while True:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue
                try:
                    # Arduino ส่ง: {"depth":1.25,"temp":28.5,"ph":7.4,"do":6.2,"lat":13.7563,"lon":100.5}
                    data = json.loads(line)
                    with lock:
                        state['depth'] = round(float(data.get('depth', state['depth'])), 2)
                        state['temp'] = round(float(data.get('temp', state['temp'])), 2)
                        state['ph'] = round(float(data.get('ph', state['ph'])), 2)
                        state['do'] = round(float(data.get('do', state['do'])), 2)
                        # ✅ อ่าน GPS จาก Arduino (ถ้า Arduino มี Garmin)
                        state['lat'] = float(data.get('lat', state['lat']))
                        state['lon'] = float(data.get('lon', state['lon']))
                except (json.JSONDecodeError, ValueError):
                    pass

        except Exception as e:
            print(f"[ARD] Error: {e} — retry in 5s")
            with lock:
                state['arduino_ok'] = False
            time.sleep(5)

# ---------------------------------------------------------------------------
# THREAD C — Data Fusion (แก้ไขให้ส่ง telemetry)
# ---------------------------------------------------------------------------

def fusion_thread():
    """
    ✅ แก้ไข: แทนการส่งข้อมูลไปยัง Pixhawk
    ตอนนี้ส่งไปยัง Telemetry Radio แทน
    
    Flow:
    1. อ่าน state (lat, lon, depth, pH, DO, temp)
    2. บันทึก SQLite
    3. ส่ง Telemetry ไปยัง Radio (ผ่าน telemetry object)
    4. Emit ไปยัง Dashboard
    """
    while True:
        time.sleep(FUSION_INTERVAL)
        
        with lock:
            snap = dict(state)
            snap['ts'] = datetime.utcnow().strftime('%H:%M:%S')
            state['ts'] = snap['ts']

        # บันทึก SQLite (เหมือนเดิม)
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

        # Emit ไปยัง Dashboard
        socketio.emit('sensor_update', snap)

# ---------------------------------------------------------------------------
# SIM MODE (เหมือนเดิม)
# ---------------------------------------------------------------------------

def sim_thread():
    """Simulation mode — ใช้ทดสอบไม่ต้องต่อ hardware"""
    t = 0
    while True:
        t += 1
        with lock:
            state['lat']       = 13.7563 + (t * 0.0001)
            state['lon']       = 100.5018 + (t * 0.0001)
            state['depth']     = round(2.0 + math.sin(t * 0.1) * 1.5, 2)
            state['ph']        = round(7.0 + math.sin(t * 0.05) * 0.4, 2)
            state['do']        = round(6.5 + math.sin(t * 0.08) * 1.0, 2)
            state['temp']      = round(28.0 + math.sin(t * 0.03) * 2.0, 2)
            state['arduino_ok'] = True
            state['radio_ok'] = True
            state['ts']        = datetime.utcnow().strftime('%H:%M:%S')

        socketio.emit('sensor_update', dict(state))
        try:
            db.insert_record(
                state['lat'], state['lon'], state['depth'],
                state['ph'],  state['do'],  state['temp']
            )
            # ✅ ส่ง telemetry ด้วยในเซ SIM
            if telemetry.connected:
                telemetry.send_all_telemetry(
                    state['lat'], state['lon'], state['depth'],
                    state['ph'], state['do'], state['temp']
                )
        except Exception as e:
            print(f"[SIM] DB error: {e}")
        time.sleep(FUSION_INTERVAL)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    db.init_db()

    # ✅ เปิด telemetry sender ก่อนเริ่มเธรด
    if not telemetry.connected and USE_RPi_TELEMETRY:
        print("[MAIN] ⚠️ WARNING: Telemetry radio not connected!")
        print("[MAIN] Check /dev/ttyUSB0 and baud rate 57600")

    if USE_SIM:
        print("[APP] *** SIMULATION MODE ***")
        threading.Thread(target=sim_thread, daemon=True).start()
    else:
        # ❌ ไม่เปิด mavlink_thread()
        threading.Thread(target=arduino_thread, daemon=True).start()
        threading.Thread(target=fusion_thread,  daemon=True).start()

    print("[APP] Server: http://0.0.0.0:5000")
    print("[APP] Telemetry radio:", "🔗 Connected" if telemetry.connected else "❌ Not connected")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
