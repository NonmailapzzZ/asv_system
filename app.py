"""
app.py — สมองหลัก ASV Data Fusion System
Thread A: MAVLink (Pixhawk) → GPS + Depth
Thread B: Serial  (Arduino) → pH + DO + Temp
Data Fusion → SQLite ทุก 2 วินาที + WebSocket broadcast
"""

import os
import time
import json
import threading
import serial
import serial.tools.list_ports
from flask import Flask, render_template, send_file, jsonify, request
from flask_socketio import SocketIO
from pymavlink import mavutil

import database
import mission_manager

# ---------------------------------------------------------------------------
# การตั้งค่า — แก้ให้ตรงกับเครื่อง
# ---------------------------------------------------------------------------
PIXHAWK_PORT   = '/dev/ttyACM0'    # Pixhawk USB
PIXHAWK_BAUD   = 115200
ARDUINO_PORT   = '/dev/ttyUSB0'    # Arduino Mega USB
ARDUINO_BAUD   = 9600
FLASK_PORT     = 5000
FUSION_INTERVAL = 2.0              # วินาที ระหว่างบันทึก DB

# เปลี่ยนเป็น True เพื่อจำลองข้อมูลโดยไม่ต้องต่อ hardware
SIM_MAVLINK  = False
SIM_ARDUINO  = False
# ---------------------------------------------------------------------------

app     = Flask(__name__)
app.config['SECRET_KEY'] = 'asv-secret-2024'
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ---- Global fused state ----
fused = {
    'lat': 0.0, 'lon': 0.0, 'depth': 0.0,
    'ph': 0.0, 'do': 0.0, 'temp': 0.0,
    'ts': '--',
    'mav_ok': False, 'ard_ok': False,
}
fused_lock = threading.Lock()

# MAVLink connection (shared กับ mission_manager)
mav_master = None


# ---------------------------------------------------------------------------
# THREAD A — MAVLink (Pixhawk)
# ---------------------------------------------------------------------------
def thread_mavlink():
    global mav_master, fused

    import random  # สำหรับ simulation

    if SIM_MAVLINK:
        print("[MAV] Simulation mode")
        while True:
            with fused_lock:
                fused['lat']   = round(13.7563 + random.uniform(-0.001, 0.001), 6)
                fused['lon']   = round(100.5018 + random.uniform(-0.001, 0.001), 6)
                fused['depth'] = round(random.uniform(1.0, 8.0), 2)
                fused['mav_ok'] = True
            time.sleep(1)
        return

    while True:
        try:
            print(f"[MAV] Connecting {PIXHAWK_PORT} @ {PIXHAWK_BAUD}...")
            mav_master = mavutil.mavlink_connection(PIXHAWK_PORT, baud=PIXHAWK_BAUD)
            mav_master.wait_heartbeat(timeout=15)
            print(f"[MAV] Heartbeat! Sys={mav_master.target_system}")

            while True:
                msg = mav_master.recv_match(
                    type=['GLOBAL_POSITION_INT', 'DISTANCE_SENSOR', 'VFR_HUD'],
                    blocking=True, timeout=3
                )
                if msg is None:
                    continue

                t = msg.get_type()
                with fused_lock:
                    fused['mav_ok'] = True
                    if t == 'GLOBAL_POSITION_INT':
                        # MAVLink ส่ง lat/lon เป็น int × 1e7
                        fused['lat'] = msg.lat / 1e7
                        fused['lon'] = msg.lon / 1e7
                    elif t == 'DISTANCE_SENSOR':
                        # Garmin ส่ง depth มาใน cm → แปลงเป็น m
                        fused['depth'] = round(msg.current_distance / 100.0, 2)
                    elif t == 'VFR_HUD':
                        # fallback ถ้า DISTANCE_SENSOR ไม่มี
                        if fused['depth'] == 0.0:
                            fused['depth'] = round(abs(msg.alt), 2)

        except Exception as e:
            print(f"[MAV] Error: {e} — retry in 5s")
            with fused_lock:
                fused['mav_ok'] = False
            time.sleep(5)


# ---------------------------------------------------------------------------
# THREAD B — Arduino Serial (pH, DO, Temp)
# ---------------------------------------------------------------------------
def thread_arduino():
    global fused

    import random  # สำหรับ simulation

    if SIM_ARDUINO:
        print("[ARD] Simulation mode")
        while True:
            with fused_lock:
                fused['ph']   = round(random.uniform(6.5, 8.5), 2)
                fused['do']   = round(random.uniform(5.0, 9.0), 2)
                fused['temp'] = round(random.uniform(25.0, 33.0), 2)
                fused['ard_ok'] = True
            time.sleep(1)
        return

    while True:
        try:
            print(f"[ARD] Connecting {ARDUINO_PORT} @ {ARDUINO_BAUD}...")
            ser = serial.Serial(ARDUINO_PORT, ARDUINO_BAUD, timeout=3)
            print("[ARD] Arduino connected")

            while True:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    with fused_lock:
                        fused['ph']     = round(float(data.get('ph',   fused['ph'])),   2)
                        fused['do']     = round(float(data.get('do',   fused['do'])),   2)
                        fused['temp']   = round(float(data.get('temp', fused['temp'])), 2)
                        fused['ard_ok'] = True
                except json.JSONDecodeError:
                    pass  # บรรทัดที่ยังไม่สมบูรณ์ — รอบรรทัดถัดไป

        except serial.SerialException as e:
            print(f"[ARD] Serial error: {e} — retry in 5s")
            with fused_lock:
                fused['ard_ok'] = False
            time.sleep(5)
        except Exception as e:
            print(f"[ARD] Unexpected error: {e}")
            time.sleep(5)


# ---------------------------------------------------------------------------
# THREAD C — Data Fusion (บันทึก DB + Broadcast SocketIO)
# ---------------------------------------------------------------------------
def thread_fusion():
    while True:
        time.sleep(FUSION_INTERVAL)
        with fused_lock:
            snap = fused.copy()
            snap['ts'] = time.strftime('%H:%M:%S')
            fused['ts'] = snap['ts']

        # บันทึก DB
        database.insert_record(
            snap['lat'], snap['lon'], snap['depth'],
            snap['ph'],  snap['do'], snap['temp']
        )

        # broadcast ไปยัง browser ทุก client
        socketio.emit('sensor_update', snap)


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    with fused_lock:
        return jsonify(fused.copy())


@app.route('/api/history')
def api_history():
    limit = int(request.args.get('limit', 50))
    return jsonify(database.get_recent(limit))


@app.route('/api/stats')
def api_stats():
    return jsonify(database.get_stats())


@app.route('/api/mission/status')
def api_mission_status():
    return jsonify(mission_manager.mission_status)


@app.route('/api/mission/upload', methods=['POST'])
def api_mission_upload():
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'ไม่มีไฟล์'}), 400
    f = request.files['file']
    if not f.filename.endswith('.waypoints'):
        return jsonify({'ok': False, 'error': 'ต้องเป็นไฟล์ .waypoints'}), 400

    path = os.path.join(app.config['UPLOAD_FOLDER'], f.filename)
    f.save(path)

    try:
        waypoints = mission_manager.parse_waypoints_file(path)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    # upload ใน background thread เพื่อไม่บล็อก Flask
    def do_upload():
        if mav_master:
            mission_manager.upload_mission(mav_master, waypoints)
            socketio.emit('mission_update', mission_manager.mission_status)
        else:
            mission_manager.mission_status.update({
                'state': 'error', 'message': 'Pixhawk ไม่ได้เชื่อมต่อ'
            })
            socketio.emit('mission_update', mission_manager.mission_status)

    threading.Thread(target=do_upload, daemon=True).start()
    return jsonify({'ok': True, 'waypoints': len(waypoints)})


@app.route('/api/mission/start', methods=['POST'])
def api_mission_start():
    if mav_master is None:
        return jsonify({'ok': False, 'error': 'Pixhawk ไม่ได้เชื่อมต่อ'}), 400
    ok = mission_manager.arm_and_start(mav_master)
    return jsonify({'ok': ok})


@app.route('/download/csv')
def download_csv():
    path = database.export_csv()
    if path and os.path.exists(path):
        return send_file(path, as_attachment=True, download_name='asv_log.csv')
    return jsonify({'error': 'ไม่สามารถ export ได้'}), 500


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    database.init_db()

    threading.Thread(target=thread_mavlink, daemon=True).start()
    threading.Thread(target=thread_arduino, daemon=True).start()
    threading.Thread(target=thread_fusion,  daemon=True).start()

    print(f"[APP] ASV Dashboard: http://0.0.0.0:{FLASK_PORT}")
    socketio.run(app, host='0.0.0.0', port=FLASK_PORT, debug=False)
