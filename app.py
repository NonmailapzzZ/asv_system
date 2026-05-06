"""
app_sampling_version.py — ASV System with Smart Sampling

Data Flow:
   Arduino (UART) → RPi (app.py) → Buffer (ไม่บันทึก SQL)
   
   Pixhawk สั่ง Delay ↓
   → Start sampling
   → เก็บค่าเฉพาะ 30 วินาที
   → หยุด sampling
   → Calculate average
   → บันทึก 1 record ลง SQL

ติดตั้ง:
   pip install flask flask-socketio pymavlink pyserial eventlet opencv-python-headless

รัน:
   python3 app_sampling_version.py
"""

import os, time, json, threading, logging, math
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
UART_PORT       = '/dev/ttyS0'      # RPi UART port
UART_BAUD       = 9600
PIXHAWK_PORT    = '/dev/ttyACM0'    # Pixhawk USB หรือ /dev/ttyS0 สำหรับ TELEM1
PIXHAWK_BAUD    = 115200            # USB: 115200, UART: 57600
FUSION_INTERVAL = 2.0               # วินาที - emit ผ่าน WebSocket
USE_SIM         = False             # ปิด simulation mode
USE_PIXHAWK     = True              # เปิด Pixhawk connection

# ===== FLASK SETUP =====
app = Flask(__name__, 
            template_folder=os.path.join(os.path.dirname(__file__), 'dashboard'),
            static_folder=os.path.join(os.path.dirname(__file__), 'dashboard'))
app.config['SECRET_KEY'] = 'asv-secret-2024'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# ===== SHARED STATE (Thread-safe) =====
lock = threading.Lock()
state = {
    # Arduino sensors (real-time)
    'lat': 0.0, 'lon': 0.0,
    'depth': 0.0,
    'temp': 0.0, 'ph': 0.0, 'do': 0.0,
    'uart_ok': False,
    
    # Pixhawk (optional)
    'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0,
    'battery_voltage': 0.0,
    'battery_remaining': 0.0,
    'pixhawk_ok': False,
    
    # Sampling status
    'sampling_active': False,
    'sampling_progress': 0,
    'sampling_samples': 0,
    'sampling_remaining': 0,
    
    'ts': '',
}

# ===== GLOBAL OBJECTS =====
sampler = SamplingManager()
pixhawk = None
uart_reader = None

# ===================================================
# UART THREAD — อ่านข้อมูล Arduino
# ===================================================

def uart_thread():
    """
    Thread A: UART Reader
    
    ทำงาน:
    1. เชื่อมต่อ Arduino ผ่าน UART
    2. อ่านค่า sensor ทีละ 1 line
    3. Update state
    4. เพิ่มเข้า sampler buffer (ถ้า sampling active)
    
    สำคัญ: ไม่บันทึก SQL ที่นี่!
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
            
            log.info("[UART] Connected to Arduino")
            with lock:
                state['uart_ok'] = True
            
            # อ่านข้อมูล continuously
            for sensor_data in uart_reader.read_sensors_continuous(interval=0.1):
                if sensor_data:
                    # Update state
                    with lock:
                        state['lat']   = sensor_data.get('lat',   state['lat'])
                        state['lon']   = sensor_data.get('lon',   state['lon'])
                        state['depth'] = sensor_data.get('depth', state['depth'])
                        state['temp']  = sensor_data.get('temp',  state['temp'])
                        state['ph']    = sensor_data.get('ph',    state['ph'])
                        state['do']    = sensor_data.get('do',    state['do'])
                        state['uart_ok'] = True
                    
                    # ส่งเข้า sampler buffer (ถ้า sampling active)
                    sampler.add_sample(sensor_data)
                    log.debug(f"[UART] ✓ Lat={sensor_data['lat']:.4f}, "
                             f"Lon={sensor_data['lon']:.4f}, "
                             f"Depth={sensor_data['depth']:.2f}m")
                else:
                    with lock:
                        state['uart_ok'] = False
        
        except Exception as e:
            log.error(f"[UART] Error: {e}")
            with lock:
                state['uart_ok'] = False
            time.sleep(5)

# ===================================================
# PIXHAWK THREAD — อ่านข้อมูล + Detect Delay
# ===================================================

def pixhawk_thread():
    """
    Thread B: Pixhawk Reader + Delay Detector
    
    ทำงาน:
    1. เชื่อมต่อ Pixhawk
    2. อ่าน GPS, Attitude, Battery
    3. Detect Delay command (DO_SET_SERVO)
    4. Auto start/stop sampling
    
    CRITICAL: ตรวจสอบ DO_SET_SERVO command
    - param2 = duration (วินาที)
    - เมื่อ Delay เริ่ม → sampler.start_sampling(duration)
    - เมื่อ Delay หมด → sampler.stop_sampling() + save to DB
    """
    global pixhawk, state
    
    delay_active = False
    delay_start_time = 0
    delay_duration = 0
    
    while True:
        try:
            if not pixhawk or not pixhawk.connected:
                log.info("[PIXHAWK] Connecting...")
                pixhawk = PixhawkReader(PIXHAWK_PORT, PIXHAWK_BAUD)
                time.sleep(5)
                continue
            
            # อ่านข้อมูล Pixhawk
            data = pixhawk.read_all()
            if data:
                with lock:
                    if 'lat' in data:
                        state['lat'] = data['lat']
                        state['lon'] = data['lon']
                    if 'roll' in data:
                        state['roll'] = data['roll']
                        state['pitch'] = data['pitch']
                        state['yaw'] = data['yaw']
                    if 'voltage' in data:
                        state['battery_voltage'] = data['voltage']
                        state['battery_remaining'] = data['remaining']
                    state['pixhawk_ok'] = True
            
            # DETECT DELAY COMMAND
            # ตรวจสอบ MAVLink messages สำหรับ DO_SET_SERVO (Delay command)
            msg = pixhawk.master.recv_match(
                type=['COMMAND_LONG', 'COMMAND_ACK'],
                blocking=False,
                timeout=0.1
            )
            
            if msg:
                msg_type = msg.get_type()
                
                # === DELAY START ===
                if msg_type == 'COMMAND_LONG':
                    if msg.command == 184:  # MAV_CMD_DO_SET_SERVO
                        duration = int(msg.param2)
                        log.info(f"[PIXHAWK] DELAY DETECTED: {duration}s")
                        log.info(f"[PIXHAWK] Starting sampling...")
                        
                        # Start sampling
                        sampler.start_sampling(duration)
                        delay_active = True
                        delay_start_time = time.time()
                        delay_duration = duration
                        
                        with lock:
                            state['sampling_active'] = True
                
                # === DELAY END ===
                elif msg_type == 'COMMAND_ACK':
                    if msg.command == 184 and delay_active:
                        log.info(f"[PIXHAWK] DELAY ENDED")
                        
                        # Stop sampling
                        result = sampler.stop_sampling()
                        log.info(f"[PIXHAWK] ✓ Collected {result['count']} samples")
                        
                        # Calculate average + save to DB
                        avg = sampler.get_average_sample()
                        if avg:
                            try:
                                db.insert_record(
                                    avg['lat'], avg['lon'], avg['depth'],
                                    avg['ph'], avg['do'], avg['temp']
                                )
                                log.info(f"[DB] Saved: "
                                        f"depth={avg['depth']:.2f}m, "
                                        f"temp={avg['temp']:.1f}°C, "
                                        f"pH={avg['ph']:.2f}, "
                                        f"sample_count={avg['sample_count']}")
                                sampler.clear_samples()
                            except Exception as e:
                                log.error(f"[DB] Save error: {e}")
                        
                        delay_active = False
                        with lock:
                            state['sampling_active'] = False
            
            # UPDATE SAMPLING STATUS
            status = sampler.get_status()
            with lock:
                state['sampling_active'] = status['active']
                state['sampling_progress'] = status['progress']
                state['sampling_samples'] = status['sample_count']
                state['sampling_remaining'] = status['remaining']
            
            time.sleep(0.5)
        
        except Exception as e:
            log.error(f"[PIXHAWK] Error: {e}")
            with lock:
                state['pixhawk_ok'] = False
            time.sleep(5)

# ===================================================
# DATA FUSION THREAD — Emit เท่านั้น (ไม่บันทึก DB)
# ===================================================

def fusion_thread():
    """
    Thread C: Data Broadcasting
    
    ทำงาน:
    1. อ่าน state ล่าสุด
    2. ไม่บันทึก SQL (เก็บใน sampler buffer เท่านั้น)
    3. Emit ไปยัง Dashboard ผ่าน WebSocket
    
    CRITICAL: ห้ามบันทึก DB ที่นี่!
    - SQL จะบันทึกเฉพาะตอน Delay หมด
    """
    while True:
        time.sleep(FUSION_INTERVAL)
        
        # Copy state
        with lock:
            snap = dict(state)
            snap['ts'] = datetime.utcnow().strftime('%H:%M:%S')
            state['ts'] = snap['ts']
        
        # ไม่บันทึก SQL ที่นี่!
        # SQL จะบันทึกเฉพาะเมื่อ Delay หมด ใน pixhawk_thread()
        
        # Emit ไปยัง Dashboard
        try:
            socketio.emit('sensor_update', snap)
            log.debug(f"[WS] ✓ Emitted: {snap.get('ts', 'unknown')}")
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
        time.sleep(FUSION_INTERVAL)

# ===================================================
# FLASK ROUTES
# ===================================================

@app.route('/')
def index():
    """Dashboard home page"""
    return render_template('index.html')

@app.route('/api/history')
def api_history():
    """ ดึง 50 record ล่าสุด"""
    return jsonify(db.get_recent(50))

@app.route('/api/download_csv')
def download_csv():
    """ Export CSV log"""
    try:
        path = db.export_csv()
        return send_file(path, as_attachment=True,
                         download_name=os.path.basename(path))
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

# ===== SAMPLING ROUTES =====

@app.route('/api/sampling/status')
def sampling_status():
    """ดึงสถานะ sampling"""
    status = sampler.get_status()
    return jsonify(status)

@app.route('/api/sampling/start/<int:seconds>', methods=['POST'])
def sampling_start(seconds):
    """Manual start sampling (สำหรับ test)"""
    log.info(f"[API] Manual sampling start: {seconds}s")
    sampler.start_sampling(seconds)
    with lock:
        state['sampling_active'] = True
    return jsonify({'ok': True, 'msg': f'Sampling started for {seconds}s'})

@app.route('/api/sampling/stop', methods=['POST'])
def sampling_stop():
    """Manual stop sampling"""
    log.info("[API] Manual sampling stop")
    result = sampler.stop_sampling()
    
    # Auto-save average to DB
    avg = sampler.get_average_sample()
    if avg:
        try:
            db.insert_record(
                avg['lat'], avg['lon'], avg['depth'],
                avg['ph'], avg['do'], avg['temp']
            )
            log.info(f"[DB] Manual save: {avg['sample_count']} samples")
            sampler.clear_samples()
            
            with lock:
                state['sampling_active'] = False
            
            return jsonify({
                'ok': True,
                'samples_collected': avg['sample_count'],
                'message': f'Saved {avg["sample_count"]} samples as 1 record'
            })
        except Exception as e:
            return jsonify({'ok': False, 'msg': f'DB error: {e}'}), 500
    else:
        return jsonify({'ok': False, 'msg': 'No samples collected'}), 400

@app.route('/api/sampling/details')
def sampling_details():
    """ ดึงรายละเอียด samples ทั้งหมด"""
    samples = sampler.get_all_samples()
    avg = sampler.get_average_sample()
    
    return jsonify({
        'average': avg,
        'samples': samples,
        'count': len(samples)
    })

# ===== CAMERA ROUTES =====

@app.route('/api/camera/devices')
def camera_devices():
    """ หา webcam ที่ต่ออยู่"""
    devices = camera_manager.list_devices()
    return jsonify({'devices': devices, 
                    'active': camera_manager._channel if camera_manager.is_running else None})

@app.route('/api/camera/start/<int:channel>', methods=['POST'])
def camera_start(channel):
    """ เปิดกล้อง channel"""
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
    """ ปิดกล้อง"""
    camera_manager.stop()
    return jsonify({'ok': True})

@app.route('/api/camera/stream')
def camera_stream():
    """ MJPEG stream"""
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

# ===== WEBSOCKET EVENTS =====

@socketio.on('connect')
def on_connect():
    """ Browser เชื่อมต่อ"""
    with lock:
        snap = dict(state)
    socketio.emit('sensor_update', snap)
    log.info("[WS] Client connected")

# ===================================================
# MAIN
# ===================================================

if __name__ == '__main__':
    # Initialize database
    db.init_db()
    log.info("[MAIN] Database initialized")
    
    # Start threads
    if USE_SIM:
        log.warning("[MAIN] *** SIMULATION MODE ***")
        threading.Thread(target=sim_thread, daemon=True).start()
    else:
        log.info("[MAIN] *** UART + PIXHAWK MODE ***")
        threading.Thread(target=uart_thread, daemon=True).start()
        
        if USE_PIXHAWK:
            threading.Thread(target=pixhawk_thread, daemon=True).start()
        
        threading.Thread(target=fusion_thread, daemon=True).start()
    
    log.info("[MAIN] Starting Flask server...")
    log.info("[MAIN] Dashboard: http://0.0.0.0:5000")
    log.info(f"[MAIN] UART: {UART_PORT} @ {UART_BAUD} baud")
    log.info(f"[MAIN] Pixhawk: {PIXHAWK_PORT if USE_PIXHAWK else 'DISABLED'}")
    log.info(f"[MAIN] Database: logs/asv_data.db")
    log.info(f"[MAIN] Sampling: {'ENABLED' if USE_PIXHAWK else 'DISABLED'}")
    
    # Start Flask with SocketIO
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
