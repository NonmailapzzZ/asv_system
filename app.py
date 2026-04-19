"""
app.py — ASV System Main Flask Application
"""

import os, time, json, threading, serial, serial.tools.list_ports, random, math
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_socketio import SocketIO
from pymavlink import mavutil

import database as db
import mission_manager as mm
from camera import camera_manager

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
PIXHAWK_PORT   = '/dev/ttyACM0'
PIXHAWK_BAUD   = 115200
ARDUINO_BAUD   = 9600
FUSION_INTERVAL = 2.0
USE_SIM        = True
# ---------------------------------------------------------------------------

# ชี้ให้ Flask รู้ว่า template อยู่ folder dashboard และ static files ก็อยู่ที่นั่นเหมือนกัน
app = Flask(__name__, 
            template_folder=os.path.join(os.path.dirname(__file__), 'dashboard'),
            static_folder=os.path.join(os.path.dirname(__file__), 'dashboard'))
app.config['SECRET_KEY'] = 'asv-secret-2024'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

lock = threading.Lock()
state = {
    'lat': 0.0, 'lon': 0.0, 'depth': 0.0,
    'pixhawk_ok': False,
    'ph': 0.0, 'do': 0.0, 'temp': 0.0,
    'arduino_ok': False,
    'ts': '',
}

# ---------------------------------------------------------------------------
# THREAD A — MAVLink
# ---------------------------------------------------------------------------
def mavlink_thread():
    global state
    while True:
        master = None
        try:
            print(f"[MAV] Connecting to Pixhawk on {PIXHAWK_PORT}...")
            master = mavutil.mavlink_connection(PIXHAWK_PORT, baud=PIXHAWK_BAUD)
            master.wait_heartbeat(timeout=15)
            print(f"[MAV] Heartbeat! sys={master.target_system}")

            master.mav.request_data_stream_send(
                master.target_system, master.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_ALL, 4, 1
            )

            with lock:
                state['pixhawk_ok'] = True

            while True:
                msg = master.recv_match(
                    type=['GLOBAL_POSITION_INT', 'DISTANCE_SENSOR', 'VFR_HUD'],
                    blocking=True, timeout=3
                )
                if not msg:
                    continue

                with lock:
                    if msg.get_type() == 'GLOBAL_POSITION_INT':
                        state['lat'] = msg.lat / 1e7
                        state['lon'] = msg.lon / 1e7

                    elif msg.get_type() == 'DISTANCE_SENSOR':
                        state['depth'] = round(msg.current_distance / 100.0, 2)

                    elif msg.get_type() == 'VFR_HUD':
                        if state['depth'] == 0.0:
                            state['depth'] = round(abs(msg.alt), 2)

        except Exception as e:
            print(f"[MAV] Error: {e} — retry in 5s")
            with lock:
                state['pixhawk_ok'] = False
            time.sleep(5)

# ---------------------------------------------------------------------------
# THREAD B — Arduino
# ---------------------------------------------------------------------------
def find_arduino_port():
    for p in serial.tools.list_ports.comports():
        if 'Arduino' in (p.description or '') or 'CH340' in (p.description or '') \
                or 'ttyUSB' in (p.device or ''):
            return p.device
    return None

def arduino_thread():
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
            print(f"[ARD] Connected to {port}")
            with lock:
                state['arduino_ok'] = True

            while True:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    with lock:
                        state['ph']   = round(float(data.get('ph',   state['ph'])),   2)
                        state['do']   = round(float(data.get('do',   state['do'])),   2)
                        state['temp'] = round(float(data.get('temp', state['temp'])), 2)
                except (json.JSONDecodeError, ValueError):
                    pass

        except Exception as e:
            print(f"[ARD] Error: {e} — retry in 5s")
            with lock:
                state['arduino_ok'] = False
            time.sleep(5)

# ---------------------------------------------------------------------------
# THREAD C — Data Fusion
# ---------------------------------------------------------------------------
def fusion_thread():
    while True:
        time.sleep(FUSION_INTERVAL)
        with lock:
            snap = dict(state)
            snap['ts'] = datetime.utcnow().strftime('%H:%M:%S')
            state['ts'] = snap['ts']

        try:
            db.insert_record(
                snap['lat'], snap['lon'], snap['depth'],
                snap['ph'],  snap['do'],  snap['temp']
            )
        except Exception as e:
            print(f"[FUSION] DB error: {e}")

        socketio.emit('sensor_update', snap)

# ---------------------------------------------------------------------------
# SIM MODE
# ---------------------------------------------------------------------------
def sim_thread():
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
            state['pixhawk_ok'] = True
            state['arduino_ok'] = True
            state['ts']        = datetime.utcnow().strftime('%H:%M:%S')

        socketio.emit('sensor_update', dict(state))
        try:
            db.insert_record(
                state['lat'], state['lon'], state['depth'],
                state['ph'],  state['do'],  state['temp']
            )
        except Exception as e:
            print(f"[SIM] DB error: {e}")
        time.sleep(FUSION_INTERVAL)

# ---------------------------------------------------------------------------
# FLASK ROUTES
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/history')
def api_history():
    return jsonify(db.get_recent(50))

@app.route('/api/upload_mission', methods=['POST'])
def upload_mission():
    if 'file' not in request.files:
        return jsonify({'ok': False, 'msg': 'ไม่มีไฟล์'}), 400

    f = request.files['file']
    if not f.filename.endswith('.waypoints'):
        return jsonify({'ok': False, 'msg': 'ต้องเป็นไฟล์ .waypoints'}), 400

    path = f'uploads/{f.filename}'
    os.makedirs('uploads', exist_ok=True)
    f.save(path)

    try:
        waypoints = mm.parse_waypoints_file(path)
        master    = mavutil.mavlink_connection(PIXHAWK_PORT, baud=PIXHAWK_BAUD)
        master.wait_heartbeat(timeout=10)
        mm.upload_mission(master, waypoints)
        return jsonify({'ok': True, 'msg': f'Upload สำเร็จ {len(waypoints)} waypoints'})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

@app.route('/api/start_mission', methods=['POST'])
def start_mission():
    try:
        master = mavutil.mavlink_connection(PIXHAWK_PORT, baud=PIXHAWK_BAUD)
        master.wait_heartbeat(timeout=10)
        mm.start_mission(master)
        return jsonify({'ok': True, 'msg': 'Mission started'})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

@app.route('/api/download_csv')
def download_csv():
    try:
        path = db.export_csv()
        return send_file(path, as_attachment=True,
                         download_name=os.path.basename(path))
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

# ---------------------------------------------------------------------------
# CAMERA ROUTES
# ---------------------------------------------------------------------------
@app.route('/api/camera/devices')
def camera_devices():
    devices = camera_manager.list_devices()
    return jsonify({'devices': devices, 
                    'active': camera_manager._channel if camera_manager.is_running else None})

@app.route('/api/camera/start/<int:channel>', methods=['POST'])
def camera_start(channel):
    try:
        camera_manager.start(channel)
        time.sleep(0.5)
        if camera_manager.is_running:
            return jsonify({'ok': True, 'msg': f'Camera {channel} started'})
        else:
            return jsonify({'ok': False, 'msg': f'Cannot open camera {channel}'}), 400
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

@app.route('/api/camera/stop', methods=['POST'])
def camera_stop():
    camera_manager.stop()
    return jsonify({'ok': True})

@app.route('/api/camera/stream')
def camera_stream():
    def generate():
        while True:
            jpeg = camera_manager.get_jpeg()
            if jpeg:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg + b'\r\n')
            else:
                time.sleep(0.1)
    return Response(generate(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@socketio.on('connect')
def on_connect():
    with lock:
        snap = dict(state)
    socketio.emit('sensor_update', snap)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
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
