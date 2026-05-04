"""
app_uart_version.py — ASV System Flask Application (UART Version)

Data Flow:
   Arduino (UART) → RPi (app.py) → SQLite → Dashboard (WebSocket)
   
Key Features:
   - อ่านค่า sensor จาก Arduino ผ่าน UART (/dev/ttyS0)
   - Fusion data จาก UART + (optional) Pixhawk
   - Save ลง SQLite database
   - Stream ไป Dashboard real-time ผ่าน WebSocket
   - Export CSV
   - Mission upload/start
   - Camera streaming

ติดตั้ง:
   pip install flask flask-socketio pymavlink pyserial eventlet opencv-python-headless

รัน:
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
from pixhawk_reader import PixhawkReader
from sampling_manager import SamplingManager


# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ===== CONFIG =====
UART_PORT       = '/dev/ttyS0'      # RPi UART port (GPIO 14/15)
UART_BAUD       = 9600              # Arduino baud rate
FUSION_INTERVAL = 2.0               # วินาที - บันทึก + emit ผ่าน WebSocket
USE_SIM         = False             # ปิด simulation mode, เปิด UART จริง

PIXHAWK_PORT = '/dev/ttyACM0'  # USB
# PIXHAWK_PORT = '/dev/ttyS0'   # UART TELEM1
PIXHAWK_BAUD = 115200
USE_PIXHAWK = True

sampler = SamplingManager()

# GLOBAL
pixhawk = None

if USE_PIXHAWK:
    pixhawk = PixhawkReader(PIXHAWK_PORT, PIXHAWK_BAUD)
    


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
    
    # Pixhawk data
    'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0,
    'battery_voltage': 0.0,
    'battery_remaining': 0.0,
    'pixhawk_ok': False,
    
    # Status
    'uart_ok': False,           #UART connection status
    'pixhawk_ok': False,        # (optional) Pixhawk status
    'ts': '',                   # timestamp
}

# ===== UART READER OBJECT =====
uart_reader = None

# ===================================================
# Detect Pixhawk Delay THREAD
# ===================================================

def pixhawk_thread():
    """
    Monitor Pixhawk MAVLink messages
    Detect DO_SET_SERVO command (Delay equivalent)
    """
    while True:
        msg = pixhawk.master.recv_match(
            type=['COMMAND_LONG', 'COMMAND_ACK'],
            blocking=False
        )
        
        if msg:
            # Detect Delay command
            if msg.get_type() == 'COMMAND_LONG':
                if msg.command == 184:  # MAV_CMD_DO_SET_SERVO
                    duration = msg.param2  # duration in seconds
                    log.info(f"[PIXHAWK] Delay detected: {duration}s")
                    sampler.start_sampling(int(duration))
            
            # Detect Delay end
            elif msg.get_type() == 'COMMAND_ACK':
                if msg.command == 184:  # Delay ended
                    log.info("[PIXHAWK] Delay ended")
                    result = sampler.stop_sampling()
                    
                    # Calculate and save to database
                    avg = sampler.get_average_sample()
                    if avg:
                        db.insert_record(
                            avg['lat'], avg['lon'], avg['depth'],
                            avg['ph'], avg['do'], avg['temp']
                        )
                        log.info(f"[DB] ✓ Saved {avg['sample_count']} samples as 1 record")
                        sampler.clear_samples()


# ===================================================
# UART THREAD — อ่านข้อมูล Arduino
# ===================================================

def uart_thread():
    """
    Thread A: UART Reader
    
    ทำงาน:
    1. เชื่อมต่อกับ Arduino ผ่าน UART (/dev/ttyS0)
    2. อ่านค่า JSON จาก Arduino ทีละ 1 line
    3. Parse JSON: {"lat": ..., "lon": ..., "depth": ..., "temp": ..., "ph": ..., "do": ...}
    4. Update state dictionary
    
    อ่านทีละ line, ไม่กำหนดช่วงเวลา (Arduino ส่งทุก 1 วินาที)
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
            
            log.info("[UART]Connected to Arduino")
            with lock:
                state['uart_ok'] = True
            
            #อ่านข้อมูล continuously
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
    Fusion thread:
    - Merge Arduino + Pixhawk data
    - Only insert to DB if NOT sampling
    """
    while True:
        time.sleep(FUSION_INTERVAL)
        
        with lock:
            snap = dict(state)
            snap['ts'] = datetime.utcnow().strftime('%H:%M:%S')
        
        # ห้ามบันทึก DB ตอน sampling active
        if not sampler.sampling_active:
            db.insert_record(
                snap['lat'], snap['lon'], snap['depth'],
                snap['ph'], snap['do'], snap['temp']
            )
        
        # Emit to dashboard
        socketio.emit('sensor_update', snap)
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
# MAVLINK ROUTES
# ===================================================

@app.route('/')
def index():
    """ Dashboard home page"""
    return render_template('index.html')

@app.route('/api/history')
def api_history():
    """ ดึง 50 record ล่าสุด สำหรับ load chart"""
    return jsonify(db.get_recent(50))

@app.route('/api/pixhawk/set_mode/<mode_name>', methods=['POST'])
def set_mode(mode_name):
    """ เปลี่ยน flight mode"""
    if not pixhawk or not pixhawk.connected:
        return jsonify({'ok': False, 'msg': 'Pixhawk not connected'}), 400
    
    if pixhawk.set_mode(mode_name):
        return jsonify({'ok': True, 'msg': f'Mode changed to {mode_name}'})
    else:
        return jsonify({'ok': False, 'msg': 'Failed to change mode'}), 500

@app.route('/api/pixhawk/arm', methods=['POST'])
def arm():
    """ Arm motors"""
    if not pixhawk or not pixhawk.connected:
        return jsonify({'ok': False, 'msg': 'Pixhawk not connected'}), 400
    
    if pixhawk.arm():
        return jsonify({'ok': True, 'msg': 'Motors armed'})
    else:
        return jsonify({'ok': False, 'msg': 'Failed to arm'}), 500

@app.route('/api/pixhawk/disarm', methods=['POST'])
def disarm():
    """ Disarm motors"""
    if not pixhawk or not pixhawk.connected:
        return jsonify({'ok': False, 'msg': 'Pixhawk not connected'}), 400
    
    if pixhawk.disarm():
        return jsonify({'ok': True, 'msg': 'Motors disarmed'})
    else:
        return jsonify({'ok': False, 'msg': 'Failed to disarm'}), 500
    
    
# ===================================================
# FLASK ROUTES
# ===================================================
    
@app.route('/api/upload_mission', methods=['POST'])
def upload_mission():
    """Upload .waypoints ไปยัง Pixhawk (ถ้าต่อ)"""
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
        # ต้องมี Pixhawk ต่อถึงจะใช้ได้
        from pymavlink import mavutil
        master = mavutil.mavlink_connection('/dev/ttyACM0', baud=115200)
        master.wait_heartbeat(timeout=10)
        mm.upload_mission(master, waypoints)
        return jsonify({'ok': True, 'msg': f'Upload สำเร็จ {len(waypoints)} waypoints'})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

@app.route('/api/start_mission', methods=['POST'])
def start_mission():
    """สั่ง AUTO mode (ถ้าต่อ Pixhawk)"""
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
    """Export CSV log"""
    try:
        path = db.export_csv()
        return send_file(path, as_attachment=True,
                         download_name=os.path.basename(path))
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

# ===================================================
# API ROUTES
# ===================================================

@app.route('/api/sampling/status')
def sampling_status():
    """ดึงสถานะ sampling"""
    status = sampler.get_status()
    return jsonify(status)

@app.route('/api/sampling/start/<int:seconds>', methods=['POST'])
def sampling_start(seconds):
    """Manual start sampling (สำหรับ test)"""
    sampler.start_sampling(seconds)
    return jsonify({'ok': True, 'msg': f'Sampling started for {seconds}s'})

@app.route('/api/sampling/stop', methods=['POST'])
def sampling_stop():
    """Manual stop sampling"""
    result = sampler.stop_sampling()
    
    # Auto-save average to DB
    avg = sampler.get_average_sample()
    if avg:
        db.insert_record(
            avg['lat'], avg['lon'], avg['depth'],
            avg['ph'], avg['do'], avg['temp']
        )
        sampler.clear_samples()
        return jsonify({
            'ok': True,
            'samples_collected': avg['sample_count'],
            'message': f'Saved {avg["sample_count"]} samples as 1 record'
        })
    else:
        return jsonify({'ok': False, 'msg': 'No samples collected'}), 400

@app.route('/api/sampling/details')
def sampling_details():
    """ดึงรายละเอียด samples ทั้งหมด"""
    samples = sampler.get_all_samples()
    avg = sampler.get_average_sample()
    
    return jsonify({
        'average': avg,
        'samples': samples,
        'count': len(samples)
    })

# ===================================================
# CAMERA ROUTES
# ===================================================

@app.route('/api/camera/devices')
def camera_devices():
    """ หา webcam ที่ต่ออยู่"""
    devices = camera_manager.list_devices()
    return jsonify({'devices': devices, 
                    'active': camera_manager._channel if camera_manager.is_running else None})

@app.route('/api/camera/start/<int:channel>', methods=['POST'])
def camera_start(channel):
    """เปิดกล้อง channel"""
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
    """ปิดกล้อง"""
    camera_manager.stop()
    return jsonify({'ok': True})

@app.route('/api/camera/stream')
def camera_stream():
    """MJPEG stream"""
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
    """ดึง error message"""
    error = camera_manager.get_error()
    return jsonify({'error': error})

# ===================================================
# WEBSOCKET EVENTS
# ===================================================

@socketio.on('connect')
def on_connect():
    """Browser เชื่อมต่อ"""
    with lock:
        snap = dict(state)
    socketio.emit('sensor_update', snap)
    log.info("[WS] Client connected")
    
# ===================================================
# Pixhawk Thread
# ===================================================
def pixhawk_thread():
    """
    Thread: อ่านข้อมูล Pixhawk
    
    ทำงาน:
    1. เชื่อมต่อ Pixhawk
    2. อ่าน GPS, Attitude, Battery
    3. Update state
    """
    global pixhawk, state
    
    while True:
        try:
            if not pixhawk or not pixhawk.connected:
                log.info("[PIXHAWK] Reconnecting...")
                pixhawk = PixhawkReader(PIXHAWK_PORT, PIXHAWK_BAUD)
                time.sleep(5)
                continue
            
            # อ่านข้อมูล
            data = pixhawk.read_all()
            
            if data:
                with lock:
                    # Update GPS (ถ้า Pixhawk มี GPS)
                    if 'lat' in data:
                        state['lat'] = data['lat']
                        state['lon'] = data['lon']
                    
                    # Update attitude
                    if 'roll' in data:
                        state['roll'] = data['roll']
                        state['pitch'] = data['pitch']
                        state['yaw'] = data['yaw']
                    
                    # Update battery
                    if 'voltage' in data:
                        state['battery_voltage'] = data['voltage']
                        state['battery_remaining'] = data['remaining']
                    
                    state['pixhawk_ok'] = True
                    log.info(f"[PIXHAWK] ✓ Lat={data.get('lat', 0):.4f}, "
                             f"Lon={data.get('lon', 0):.4f}")
            else:
                with lock:
                    state['pixhawk_ok'] = False
            
            time.sleep(1)
        
        except Exception as e:
            log.error(f"[PIXHAWK] Error: {e}")
            with lock:
                state['pixhawk_ok'] = False
            time.sleep(5)


# ===================================================
# MAIN
# ===================================================

if __name__ == '__main__':
    #Initialize database
    db.init_db()
    log.info("[MAIN] Database initialized")
    
    #Start threads
    if USE_SIM:
        log.warning("[MAIN] *** SIMULATION MODE ***")
        threading.Thread(target=sim_thread, daemon=True).start()
    else:
        log.info("[MAIN] *** UART MODE ***")
        threading.Thread(target=uart_thread, daemon=True).start()
        threading.Thread(target=fusion_thread, daemon=True).start()
        
    if USE_PIXHAWK:
        threading.Thread(target=pixhawk_thread, daemon=True).start()
    
    log.info("[MAIN] Starting Flask server...")
    log.info("[MAIN] Dashboard: http://0.0.0.0:5000")
    log.info(f"[MAIN] UART: {UART_PORT} @ {UART_BAUD} baud")
    log.info(f"[MAIN] Database: logs/asv_data.db")
    
    #Start Flask with SocketIO
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
