"""
app_final.py - ASV System with Smart Sampling
Modified to handle missing Pixhawk gracefully

Flow:
1. Try to connect to Pixhawk
2. If Pixhawk not available, disable Delay detection
3. Manual sampling still works via API
4. System continues normally without Pixhawk
"""

import os, time, json, threading, logging, math
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_socketio import SocketIO

import database as db
import mission_manager as mm
from camera import camera_manager
from uart_reader import ArduinoUARTReader
from sampling_manager import SamplingManager

# Try to import Pixhawk reader, but don't fail if it's not available
try:
    from pixhawk_reader import PixhawkReader
    PIXHAWK_AVAILABLE = True
except ImportError:
    PIXHAWK_AVAILABLE = False
    PixhawkReader = None

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Config
UART_PORT       = '/dev/ttyS0'
UART_BAUD       = 9600
PIXHAWK_PORT    = '/dev/ttyACM0'
PIXHAWK_BAUD    = 115200
FUSION_INTERVAL = 2.0
USE_SIM         = False
USE_PIXHAWK     = True

# Flask setup
app = Flask(__name__, 
            template_folder=os.path.join(os.path.dirname(__file__), 'dashboard'),
            static_folder=os.path.join(os.path.dirname(__file__), 'dashboard'))
app.config['SECRET_KEY'] = 'asv-secret-2024'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# Global state
lock = threading.Lock()
state = {
    'lat': 0.0, 'lon': 0.0,
    'depth': 0.0,
    'temp': 0.0, 'ph': 0.0, 'do': 0.0,
    'uart_ok': False,
    'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0,
    'battery_voltage': 0.0,
    'battery_remaining': 0.0,
    'pixhawk_ok': False,
    'sampling_active': False,
    'sampling_progress': 0,
    'sampling_samples': 0,
    'sampling_remaining': 0,
    'ts': '',
}

# Global objects
sampler = SamplingManager()
pixhawk = None
uart_reader = None
pixhawk_available = False

# ===========================================================
# UART THREAD
# ===========================================================

def uart_thread():
    """
    Read sensor data from Arduino via UART
    Add to sampler buffer (don't write to SQL here)
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
            
            for sensor_data in uart_reader.read_sensors_continuous(interval=0.1):
                if sensor_data:
                    with lock:
                        state['lat']   = sensor_data.get('lat',   state['lat'])
                        state['lon']   = sensor_data.get('lon',   state['lon'])
                        state['depth'] = sensor_data.get('depth', state['depth'])
                        state['temp']  = sensor_data.get('temp',  state['temp'])
                        state['ph']    = sensor_data.get('ph',    state['ph'])
                        state['do']    = sensor_data.get('do',    state['do'])
                        state['uart_ok'] = True
                    
                    sampler.add_sample(sensor_data)
                    log.debug(f"[UART] Lat={sensor_data['lat']:.4f}, Lon={sensor_data['lon']:.4f}")
                else:
                    with lock:
                        state['uart_ok'] = False
        
        except Exception as e:
            log.error(f"[UART] Error: {e}")
            with lock:
                state['uart_ok'] = False
            time.sleep(5)

# ===========================================================
# PIXHAWK THREAD (with error handling)
# ===========================================================

def pixhawk_thread():
    """
    Read Pixhawk data and detect Delay commands
    Gracefully handles missing Pixhawk connection
    """
    global pixhawk, state, pixhawk_available
    
    delay_active = False
    retry_count = 0
    max_retries = 3
    
    while True:
        try:
            # Check if Pixhawk module is available
            if not PIXHAWK_AVAILABLE or not PixhawkReader:
                log.warning("[PIXHAWK] Module not available, skipping...")
                with lock:
                    state['pixhawk_ok'] = False
                time.sleep(10)
                continue
            
            if not pixhawk or not pixhawk.connected:
                if retry_count >= max_retries:
                    log.error("[PIXHAWK] Failed to connect after 3 attempts, disabling Pixhawk")
                    pixhawk_available = False
                    with lock:
                        state['pixhawk_ok'] = False
                    time.sleep(10)
                    continue
                
                log.info(f"[PIXHAWK] Attempt {retry_count + 1}/{max_retries} to connect...")
                try:
                    pixhawk = PixhawkReader(PIXHAWK_PORT, PIXHAWK_BAUD)
                    if pixhawk.connected:
                        retry_count = 0
                        pixhawk_available = True
                        log.info("[PIXHAWK] Connected!")
                    else:
                        retry_count += 1
                        with lock:
                            state['pixhawk_ok'] = False
                except Exception as e:
                    retry_count += 1
                    log.warning(f"[PIXHAWK] Connection attempt failed: {e}")
                    with lock:
                        state['pixhawk_ok'] = False
                
                time.sleep(5)
                continue
            
            # Read Pixhawk data
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
            
            # Detect Delay command
            try:
                msg = pixhawk.master.recv_match(
                    type=['COMMAND_LONG', 'COMMAND_ACK'],
                    blocking=False,
                    timeout=0.1
                )
                
                if msg:
                    msg_type = msg.get_type()
                    
                    if msg_type == 'COMMAND_LONG':
                        if msg.command == 184:
                            duration = int(msg.param2)
                            log.info(f"[PIXHAWK] Delay detected: {duration}s")
                            sampler.start_sampling(duration)
                            delay_active = True
                            with lock:
                                state['sampling_active'] = True
                    
                    elif msg_type == 'COMMAND_ACK':
                        if msg.command == 184 and delay_active:
                            log.info("[PIXHAWK] Delay ended")
                            result = sampler.stop_sampling()
                            log.info(f"[PIXHAWK] Collected {result['count']} samples")
                            
                            avg = sampler.get_average_sample()
                            if avg:
                                try:
                                    db.insert_record(
                                        avg['lat'], avg['lon'], avg['depth'],
                                        avg['ph'], avg['do'], avg['temp']
                                    )
                                    log.info(f"[DB] Saved: depth={avg['depth']:.2f}m, temp={avg['temp']:.1f}C, pH={avg['ph']:.2f}")
                                    sampler.clear_samples()
                                except Exception as e:
                                    log.error(f"[DB] Save error: {e}")
                            
                            delay_active = False
                            with lock:
                                state['sampling_active'] = False
            
            except Exception as e:
                log.debug(f"[PIXHAWK] Message processing error: {e}")
            
            # Update sampling status
            status = sampler.get_status()
            with lock:
                state['sampling_active'] = status['active']
                state['sampling_progress'] = status['progress']
                state['sampling_samples'] = status['sample_count']
                state['sampling_remaining'] = status['remaining']
            
            time.sleep(0.5)
        
        except Exception as e:
            log.error(f"[PIXHAWK] Unexpected error: {e}")
            with lock:
                state['pixhawk_ok'] = False
            retry_count += 1
            time.sleep(5)

# ===========================================================
# FUSION THREAD
# ===========================================================

def fusion_thread():
    """
    Broadcast sensor data to Dashboard
    Do NOT write to SQL here
    """
    while True:
        time.sleep(FUSION_INTERVAL)
        
        with lock:
            snap = dict(state)
            snap['ts'] = datetime.utcnow().strftime('%H:%M:%S')
            state['ts'] = snap['ts']
        
        try:
            socketio.emit('sensor_update', snap)
            log.debug(f"[WS] Emitted: {snap.get('ts', 'unknown')}")
        except Exception as e:
            log.error(f"[WS] Error: {e}")

# ===========================================================
# SIMULATION THREAD
# ===========================================================

def sim_thread():
    """
    Simulation mode for testing without hardware
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

# ===========================================================
# FLASK ROUTES
# ===========================================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/history')
def api_history():
    return jsonify(db.get_recent(50))

@app.route('/api/download_csv')
def download_csv():
    try:
        path = db.export_csv()
        return send_file(path, as_attachment=True,
                         download_name=os.path.basename(path))
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

# Sampling routes
@app.route('/api/sampling/status')
def sampling_status():
    status = sampler.get_status()
    return jsonify(status)

@app.route('/api/sampling/start/<int:seconds>', methods=['POST'])
def sampling_start(seconds):
    log.info(f"[API] Manual sampling start: {seconds}s")
    sampler.start_sampling(seconds)
    with lock:
        state['sampling_active'] = True
    return jsonify({'ok': True, 'msg': f'Sampling started for {seconds}s'})

@app.route('/api/sampling/stop', methods=['POST'])
def sampling_stop():
    log.info("[API] Manual sampling stop")
    result = sampler.stop_sampling()
    
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
    samples = sampler.get_all_samples()
    avg = sampler.get_average_sample()
    
    return jsonify({
        'average': avg,
        'samples': samples,
        'count': len(samples)
    })

# Camera routes
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

# Websocket
@socketio.on('connect')
def on_connect():
    with lock:
        snap = dict(state)
    socketio.emit('sensor_update', snap)
    log.info("[WS] Client connected")

# ===========================================================
# MAIN
# ===========================================================

if __name__ == '__main__':
    db.init_db()
    log.info("[MAIN] Database initialized")
    
    if USE_SIM:
        log.warning("[MAIN] SIMULATION MODE")
        threading.Thread(target=sim_thread, daemon=True).start()
    else:
        log.info("[MAIN] UART + SAMPLING MODE")
        threading.Thread(target=uart_thread, daemon=True).start()
        
        if USE_PIXHAWK and PIXHAWK_AVAILABLE:
            log.info(f"[MAIN] Pixhawk support enabled, connecting to {PIXHAWK_PORT}...")
            threading.Thread(target=pixhawk_thread, daemon=True).start()
        else:
            if not PIXHAWK_AVAILABLE:
                log.warning("[MAIN] Pixhawk module not available, manual sampling only")
            else:
                log.info("[MAIN] Pixhawk disabled, manual sampling only")
        
        threading.Thread(target=fusion_thread, daemon=True).start()
    
    log.info("[MAIN] Starting Flask server...")
    log.info("[MAIN] Dashboard: http://0.0.0.0:5000")
    log.info(f"[MAIN] UART: {UART_PORT} @ {UART_BAUD} baud")
    log.info(f"[MAIN] Pixhawk: {PIXHAWK_PORT if (USE_PIXHAWK and PIXHAWK_AVAILABLE) else 'DISABLED'}")
    log.info(f"[MAIN] Database: logs/asv_data.db")
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
