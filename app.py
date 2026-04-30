"""
app_uart_version.py — ASV System Flask Application (UART Version)

📊 Data Flow:
   Arduino (UART) → RPi (app.py) → SQLite → Dashboard (WebSocket)
   
🔧 Key Features:
   - อ่านค่า sensor จาก Arduino ผ่าน UART (/dev/ttyS0)
   - Fusion data จาก UART + (optional) Pixhawk
   - Save ลง SQLite database
   - Stream ไป Dashboard real-time ผ่าน WebSocket
   - Export CSV
   - Mission upload/start
   - Camera streaming

⚙️ ติดตั้ง:
   pip install flask flask-socketio pymavlink pyserial eventlet opencv-python-headless

🚀 รัน:
   python3 app_uart_version.py
"""

import os, time, json, threading, logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_socketio import SocketIO

import database as db
import mission_manager as mm
from camera import camera_manager
from uart_reader import ArduinoUARTReader

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ===== CONFIG =====
UART_PORT       = '/dev/ttyS0'      # RPi UART port (GPIO 14/15)
UART_BAUD       = 9600              # Arduino baud rate
FUSION_INTERVAL = 2.0               # วินาที - บันทึก + emit ผ่าน WebSocket
USE_SIM         = False             # ปิด simulation mode, เปิด UART จริง

# ===== FLASK SETUP =====
app = Flask(__name__, 
            template_folder=os.path.join(os.path.dirname(__file__), 'dashboard'),
            static_folder=os.path.join(os.path.dirname(__file__), 'dashboard'))
app.config['SECRET_KEY'] = 'asv-secret-2024'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# ===== SHARED STATE (Thread-safe) =====
lock = threading.Lock()
state = {
    # GPS from Arduino (Garmin)
    'lat': 0.0, 'lon': 0.0,
    
    # Sensors from Arduino
    'depth': 0.0,
    'temp': 0.0,
    'ph': 0.0,
    'do': 0.0,
    
    # Status
    'uart_ok': False,           # ✅ UART connection status
    'pixhawk_ok': False,        # (optional) Pixhawk status
    'ts': '',                   # timestamp
}

# ===== UART READER OBJECT =====
uart_reader = None

# ===================================================
# UART THREAD — อ่านข้อมูล Arduino
# ===================================================

def uart_thread():
    """
    🔄 Thread A: UART Reader
    
    ทำงาน:
    1. เชื่อมต่อกับ Arduino ผ่าน UART (/dev/ttyS0)
    2. อ่านค่า JSON จาก Arduino ทีละ 1 line
    3. Parse JSON: {"lat": ..., "lon": ..., "depth": ..., "temp": ..., "ph": ..., "do": ...}
    4. Update state dictionary
    
    ⚠️ อ่านทีละ line, ไม่กำหนดช่วงเวลา (Arduino ส่งทุก 1 วินาที)
    """
    global uart_reader, state
    
    while True:
        try:
            log.info("[UART] Initializing reader on /dev/ttyS0...")
            uart_reader = ArduinoUARTReader(UART_PORT, UART_BAUD)
            
            if not uart_reader.connected:
                log.error("[UART] Failed to connect")
                with lock:
                    state['uart_ok'] = False
                time.sleep(5)
                continue
            
            log.info("[UART] ✅ Connected to Arduino")
            with lock:
                state['uart_ok'] = True
            
            # ✅ อ่านข้อมูล continuously
            for sensor_data in uart_reader.read_sensors_continuous(interval=0.1):
                if sensor_data:
                    # Update state ด้วย sensor data จาก Arduino
                    with lock:
                        state['lat']   = sensor_data.get('lat',   state['lat'])
                        state['lon']   = sensor_data.get('lon',   state['lon'])
                        state['depth'] = sensor_data.get('depth', state['depth'])
                        state['temp']  = sensor_data.get('temp',  state['temp'])
                        state['ph']    = sensor_data.get('ph',    state['ph'])
                        state['do']    = sensor_data.get('do',    state['do'])
                        state['uart_ok'] = True
                else:
                    with lock:
                        state['uart_ok'] = False
        
        except Exception as e:
            log.error(f"[UART] Error: {e}")
            with lock:
                state['uart_ok'] = False
            time.sleep(5)

# ===================================================
# DATA FUSION THREAD — บันทึก + Emit
# ===================================================

def fusion_thread():
    """
    🔀 Thread B: Data Fusion & Broadcasting
    
    ทำงาน:
    1. อ่าน state ล่าสุด (มีการอัปเดตจาก uart_thread)
    2. บันทึก SQLite database
    3. Emit ไปยัง Dashboard ผ่าน WebSocket
    
    ⏱️ Interval: 2 วินาที (FUSION_INTERVAL)
    """
    while True:
        time.sleep(FUSION_INTERVAL)
        
        # ✅ Copy state snapshot
        with lock:
            snap = dict(state)
            snap['ts'] = datetime.utcnow().strftime('%H:%M:%S')
            state['ts'] = snap['ts']
        
        # ✅ บันทึก SQLite
        try:
            db.insert_record(
                snap['lat'], snap['lon'], snap['depth'],
                snap['ph'],  snap['do'],  snap['temp']
            )
            log.info(f"[DB] ✓ Logged: depth={snap['depth']}m, temp={snap['temp']}°C, pH={snap['ph']}")
        except Exception as e:
            log.error(f"[DB] Error: {e}")
        
        # ✅ Emit ไปยัง Dashboard (WebSocket)
        try:
            socketio.emit('sensor_update', snap)
            log.debug(f"[WS] ✓ Emitted to {snap.get('ts', 'unknown')}")
        except Exception as e:
            log.error(f"[WS] Error: {e}")

# ===================================================
# SIMULATION THREAD (optional)
# ===================================================

def sim_thread():
    """
    🎮 Simulation Mode (ถ้า USE_SIM=True)
    
    ส่ง fake data ทีละ FUSION_INTERVAL
    """
    import math
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
            state['uart_ok']   = True
            state['ts']        = datetime.utcnow().strftime('%H:%M:%S')

        socketio.emit('sensor_update', dict(state))
        try:
            db.insert_record(
                state['lat'], state['lon'], state['depth'],
                state['ph'],  state['do'],  state['temp']
            )
        except Exception as e:
            log.error(f"[SIM] DB error: {e}")
        time.sleep(FUSION_INTERVAL)

# ===================================================
# FLASK ROUTES
# ===================================================

@app.route('/')
def index():
    """📊 Dashboard home page"""
    return render_template('index.html')

@app.route('/api/history')
def api_history():
    """📈 ดึง 50 record ล่าสุด สำหรับ load chart"""
    return jsonify(db.get_recent(50))

@app.route('/api/upload_mission', methods=['POST'])
def upload_mission():
    """🗺️ Upload .waypoints ไปยัง Pixhawk (ถ้าต่อ)"""
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
        # ⚠️ ต้องมี Pixhawk ต่อถึงจะใช้ได้
        from pymavlink import mavutil
        master = mavutil.mavlink_connection('/dev/ttyACM0', baud=115200)
        master.wait_heartbeat(timeout=10)
        mm.upload_mission(master, waypoints)
        return jsonify({'ok': True, 'msg': f'Upload สำเร็จ {len(waypoints)} waypoints'})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

@app.route('/api/start_mission', methods=['POST'])
def start_mission():
    """🚀 สั่ง AUTO mode (ถ้าต่อ Pixhawk)"""
    try:
        from pymavlink import mavutil
        master = mavutil.mavlink_connection('/dev/ttyACM0', baud=115200)
        master.wait_heartbeat(timeout=10)
        mm.start_mission(master)
        return jsonify({'ok': True, 'msg': 'Mission started'})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

@app.route('/api/download_csv')
def download_csv():
    """💾 Export CSV log"""
    try:
        path = db.export_csv()
        return send_file(path, as_attachment=True,
                         download_name=os.path.basename(path))
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

# ===================================================
# CAMERA ROUTES
# ===================================================

@app.route('/api/camera/devices')
def camera_devices():
    """📹 หา webcam ที่ต่ออยู่"""
    devices = camera_manager.list_devices()
    return jsonify({'devices': devices, 
                    'active': camera_manager._channel if camera_manager.is_running else None})

@app.route('/api/camera/start/<int:channel>', methods=['POST'])
def camera_start(channel):
    """📹 เปิดกล้อง channel"""
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
    """📹 ปิดกล้อง"""
    camera_manager.stop()
    return jsonify({'ok': True})

@app.route('/api/camera/stream')
def camera_stream():
    """📹 MJPEG stream"""
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

@app.route('/api/camera/error')
def camera_error():
    """📹 ดึง error message"""
    error = camera_manager.get_error()
    return jsonify({'error': error})

# ===================================================
# WEBSOCKET EVENTS
# ===================================================

@socketio.on('connect')
def on_connect():
    """🔗 Browser เชื่อมต่อ"""
    with lock:
        snap = dict(state)
    socketio.emit('sensor_update', snap)
    log.info("[WS] Client connected")

# ===================================================
# MAIN
# ===================================================

if __name__ == '__main__':
    # ✅ Initialize database
    db.init_db()
    log.info("[MAIN] Database initialized")
    
    # ✅ Start threads
    if USE_SIM:
        log.warning("[MAIN] *** SIMULATION MODE ***")
        threading.Thread(target=sim_thread, daemon=True).start()
    else:
        log.info("[MAIN] *** UART MODE ***")
        threading.Thread(target=uart_thread, daemon=True).start()
        threading.Thread(target=fusion_thread, daemon=True).start()
    
    log.info("[MAIN] Starting Flask server...")
    log.info("[MAIN] 🌐 Dashboard: http://0.0.0.0:5000")
    log.info(f"[MAIN] 🔌 UART: {UART_PORT} @ {UART_BAUD} baud")
    log.info(f"[MAIN] 🗄️  Database: logs/asv_data.db")
    
    # ✅ Start Flask with SocketIO
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
