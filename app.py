"""
app_final.py - ASV System with Smart Sampling
Modified to handle missing Pixhawk gracefully

Flow:
1. Try to connect to Pixhawk
2. If Pixhawk not available, disable Delay detection
3. Manual sampling still works via API
4. System continues normally without Pixhawk
"""

import os, time, json, threading, logging, math, sqlite3
from datetime import datetime, timedelta    
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
DEFAULT_AUTO_SAMPLING_SECONDS = 30
UART_STALE_SECONDS = 3.0

now_thailand = datetime.utcnow() + timedelta(hours=7)


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
    'garmin_lat': 0.0, 'garmin_lon': 0.0,
    'pixhawk_lat': 0.0, 'pixhawk_lon': 0.0,
    'gps_match': True,
    'gps_error_m': 0.0,
    'gps_status': 'ok',
    'depth': 0.0,
    'temp': 0.0, 'ph': 0.0, 'do': 0.0,
    'uart_ok': False,
    'arduino_ok': False,
    'arduino_status': 'error',
    'arduino_error': 'No UART data',
    'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0,
    'battery_voltage': 0.0,
    'battery_remaining': 0.0,
    'pixhawk_ok': False,
    'pixhawk_mode': 'DISCONNECTED',
    'sampling_active': False,
    'sampling_progress': 0,
    'sampling_samples': 0,
    'sampling_remaining': 0,
    'mission_item': 0,
    'ts': '',
}

# Global objects
sampler = SamplingManager()
pixhawk = None
uart_reader = None
pixhawk_available = False
active_session_id = None
sampling_finish_lock = threading.Lock()

def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate distance in meters between two GPS points."""
    if not all([lat1, lon1, lat2, lon2]): return 0.0
    R = 6371000  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))

def begin_operation_session(label=None, source='manual'):
    """Mark the start of an operation for Mission Planner style export picking."""
    global active_session_id
    session = db.start_session(label=label, source=source)
    active_session_id = session['id']
    log.info(f"[SESSION] Started operation #{active_session_id}: {session['label']}")
    return session

def update_sampling_state(status=None):
    """Mirror SamplingManager status into websocket state."""
    if status is None:
        status = sampler.get_status()
    with lock:
        state['sampling_active'] = status['active']
        state['sampling_progress'] = status['progress']
        state['sampling_samples'] = status['sample_count']
        state['sampling_remaining'] = status['remaining']
    return status

def finish_sampling(reason='timer', force=False):
    """Stop sampling, save the averaged sample, and return the system to Idle."""
    with sampling_finish_lock:
        status = sampler.get_status()
        if not status['active']:
            return {'ok': False, 'msg': 'Sampling is not active'}

        result = sampler.stop_sampling()
        log.info(f"[SAMPLING] Finish reason={reason}, samples={result['count']}")

        avg = sampler.get_average_sample()
        if avg:
            try:
                db.insert_record(
                    avg['lat'], avg['lon'], avg['depth'],
                    avg['ph'], avg['do'], avg['temp'],
                    active_session_id,
                    garmin_lat=state['garmin_lat'],
                    garmin_lon=state['garmin_lon'],
                    pixhawk_lat=state['pixhawk_lat'],
                    pixhawk_lon=state['pixhawk_lon'],
                    gps_error=state['gps_error_m']
                )
                sampler.clear_samples()
                update_sampling_state()
                log.info(f"[DB] Saved average sample: {avg['sample_count']} samples")
                return {
                    'ok': True,
                    'samples_collected': avg['sample_count'],
                    'message': f'Saved {avg["sample_count"]} samples as 1 record'
                }
            except Exception as e:
                update_sampling_state()
                return {'ok': False, 'msg': f'DB error: {e}'}

        update_sampling_state()
        return {'ok': False, 'msg': 'No samples collected'}

def sampling_watch_thread():
    """Auto-stop sampling when its duration has elapsed."""
    while True:
        try:
            status = update_sampling_state()
            if status['active'] and status.get('remaining', 0) <= 0:
                finish_sampling(reason='timer')
        except Exception as e:
            log.error(f"[SAMPLING] Watch error: {e}")
        time.sleep(0.5)

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
            last_valid_rx = 0
            last_warn_rx = 0
            last_warn_error = ''
            
            if not uart_reader.connected:
                log.error("[UART] Failed to connect")
                with lock:
                    state['uart_ok'] = False
                    state['arduino_ok'] = False
                    state['arduino_status'] = 'error'
                    state['arduino_error'] = uart_reader.last_error or 'UART connection failed'
                time.sleep(5)
                continue
            
            log.info("[UART] Connected to Arduino")
            with lock:
                state['uart_ok'] = False
                state['arduino_ok'] = False
                state['arduino_status'] = 'error'
                state['arduino_error'] = 'Waiting for UART data'
            
            while uart_reader.connected:
                sensor_data = uart_reader.read_sensor_data()
                reader_status = uart_reader.last_status
                if sensor_data:
                    last_valid_rx = time.time()
                    with lock:
                        state['garmin_lat'] = sensor_data.get('lat', 0.0)
                        state['garmin_lon'] = sensor_data.get('lon', 0.0)
                        
                        # Use Garmin as primary lat/lon if Pixhawk not available or for sensor consistency
                        state['lat']   = state['garmin_lat']
                        state['lon']   = state['garmin_lon']
                        
                        state['depth'] = sensor_data.get('depth', state['depth'])
                        state['temp']  = sensor_data.get('temp',  state['temp'])
                        state['ph']    = sensor_data.get('ph',    state['ph'])
                        state['do']    = sensor_data.get('do',    state['do'])
                        state['uart_ok'] = True
                        state['arduino_ok'] = True
                        state['arduino_status'] = 'ok'
                        state['arduino_error'] = ''
                    
                    # Inject Garmin GPS into Pixhawk
                    if pixhawk and pixhawk.connected:
                        pixhawk.send_gps_input(state['garmin_lat'], state['garmin_lon'])
                        log.debug(f"[UART->PIX] Sent Garmin GPS: {state['garmin_lat']:.6f}, {state['garmin_lon']:.6f}")
                    
                    sampler.add_sample(sensor_data)
                    log.debug(f"[UART] Lat={sensor_data['lat']:.4f}, Lon={sensor_data['lon']:.4f}")
                elif reader_status == 'warn':
                    last_warn_rx = time.time()
                    last_warn_error = uart_reader.last_error
                    with lock:
                        state['uart_ok'] = True
                        state['arduino_ok'] = False
                        state['arduino_status'] = 'warn'
                        state['arduino_error'] = uart_reader.last_error
                elif reader_status == 'error':
                    with lock:
                        state['uart_ok'] = False
                        state['arduino_ok'] = False
                        state['arduino_status'] = 'error'
                        state['arduino_error'] = uart_reader.last_error
                else:
                    now = time.time()
                    stale = last_valid_rx == 0 or (now - last_valid_rx) > UART_STALE_SECONDS
                    warn_recent = last_warn_rx and (now - last_warn_rx) <= 2.0
                    with lock:
                        state['uart_ok'] = not stale
                        state['arduino_ok'] = not stale and not warn_recent
                        state['arduino_status'] = 'error' if stale else ('warn' if warn_recent else 'ok')
                        state['arduino_error'] = 'No UART data' if stale else (last_warn_error if warn_recent else '')
                time.sleep(0.1)
        
        except Exception as e:
            log.error(f"[UART] Error: {e}")
            with lock:
                state['uart_ok'] = False
                state['arduino_ok'] = False
                state['arduino_status'] = 'error'
                state['arduino_error'] = str(e)
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
    loiter_active = False
    mode_before_delay = 'UNKNOWN'
    retry_count = 0
    max_retries = 3
    
    while True:
        try:
            # Check if Pixhawk module is available
            if not PIXHAWK_AVAILABLE or not PixhawkReader:
                log.warning("[PIXHAWK] Module not available, skipping...")
                with lock:
                    state['pixhawk_ok'] = False
                    state['pixhawk_mode'] = 'UNAVAILABLE'
                time.sleep(10)
                continue
            
            if not pixhawk or not pixhawk.connected:
                if retry_count >= max_retries:
                    log.error("[PIXHAWK] Failed to connect after 3 attempts, disabling Pixhawk")
                    pixhawk_available = False
                    with lock:
                        state['pixhawk_ok'] = False
                        state['pixhawk_mode'] = 'DISCONNECTED'
                    time.sleep(10)
                    continue
                
                log.info(f"[PIXHAWK] Attempt {retry_count + 1}/{max_retries} to connect...")
                try:
                    pixhawk = PixhawkReader(PIXHAWK_PORT, PIXHAWK_BAUD)
                    if pixhawk.connected:
                        retry_count = 0
                        pixhawk_available = True
                        with lock:
                            state['pixhawk_ok'] = True
                            state['pixhawk_mode'] = 'CONNECTED'
                        log.info("[PIXHAWK] Connected!")
                    else:
                        retry_count += 1
                        with lock:
                            state['pixhawk_ok'] = False
                            state['pixhawk_mode'] = 'CONNECTING'
                except Exception as e:
                    retry_count += 1
                    log.warning(f"[PIXHAWK] Connection attempt failed: {e}")
                    with lock:
                        state['pixhawk_ok'] = False
                        state['pixhawk_mode'] = 'CONNECTING'
                
                time.sleep(5)
                continue
            
            # Read Pixhawk data
            data = pixhawk.read_all()
            if data:
                with lock:
                    if 'lat' in data:
                        state['pixhawk_lat'] = data['lat']
                        state['pixhawk_lon'] = data['lon']
                        
                        # Primary display position comes from Pixhawk's EKF/Fused GPS
                        state['lat'] = data['lat']
                        state['lon'] = data['lon']
                        
                        # Status Logic:
                        # Red: Both zero
                        # Blinking Orange: Garmin zero
                        # Yellow: Distance >= 10m
                        # Green: Distance < 10m
                        
                        garmin_no_fix = (state['garmin_lat'] == 0 and state['garmin_lon'] == 0)
                        pixhawk_no_fix = (state['pixhawk_lat'] == 0 and state['pixhawk_lon'] == 0)
                        
                        if garmin_no_fix and pixhawk_no_fix:
                            state['gps_status'] = 'no_fix' # Red
                            state['gps_match'] = False
                        elif garmin_no_fix:
                            state['gps_status'] = 'no_garmin' # Blinking Orange
                            state['gps_match'] = False
                        else:
                            dist = haversine_distance(
                                state['pixhawk_lat'], state['pixhawk_lon'],
                                state['garmin_lat'], state['garmin_lon']
                            )
                            state['gps_error_m'] = round(dist, 2)
                            state['gps_match'] = (dist < 10.0)
                            state['gps_status'] = 'ok' if state['gps_match'] else 'warn' # Green or Yellow
                            
                            if not state['gps_match']:
                                log.warning(f"[GPS] Mismatch detected: {dist:.1f}m difference!")
                    
                    if 'roll' in data:
                        state['roll'] = data['roll']
                        state['pitch'] = data['pitch']
                        state['yaw'] = data['yaw']
                    if 'voltage' in data:
                        state['battery_voltage'] = data['voltage']
                        state['battery_remaining'] = data['remaining']
                    if 'mission_item' in data:
                        if state.get('mission_item') != data['mission_item']:
                            log.info(f"[PIXHAWK] Mission item changed to #{data['mission_item']}")
                            state['mission_item'] = data['mission_item']
                            loiter_active = False # Allow re-trigger if next item is also LOITER
                # Process flight mode and mission status
                if 'mode' in data:
                    mode = data['mode'].upper()
                    
                    # Track mission progression to allow re-triggering on same mode (e.g. consecutive LOITER waypoints)
                    mission_curr = data.get('mission_current', -1)
                    mission_reached = data.get('mission_reached', -1)
                    
                    with lock:
                        if state.get('mission_item') != mission_curr and mission_curr != -1:
                            log.info(f"[PIXHAWK] New mission item: #{mission_curr}")
                            state['mission_item'] = mission_curr
                            # Reset loiter_active so if this new item is also a LOITER, it triggers again
                            loiter_active = False
                        
                        if not delay_active:
                            state['pixhawk_mode'] = mode
                    
                    # 1. Automatic Sampling via LOITER Mode (Flight Mode)
                    if mode == 'LOITER':
                        if not loiter_active and not sampler.get_status()['active']:
                            log.info("[PIXHAWK] LOITER mode detected, starting auto sampling")
                            if active_session_id is None:
                                begin_operation_session(source='pixhawk_loiter')
                            sampler.start_sampling(DEFAULT_AUTO_SAMPLING_SECONDS)
                            loiter_active = True
                            update_sampling_state()
                    else:
                        if loiter_active:
                            log.info(f"[PIXHAWK] Left LOITER mode (now {mode})")
                        loiter_active = False
            
            # 2. Automatic Sampling via Mission Commands (Delay/Loiter Time)
            # These are caught via COMMAND_LONG messages which ArduPilot broadcasts when executing mission items
            try:
                msg = pixhawk.master.recv_match(
                    type=['COMMAND_LONG', 'COMMAND_ACK'],
                    blocking=False,
                    timeout=0.1
                )
                
                if msg:
                    msg_type = msg.get_type()
                    
                    if msg_type == 'COMMAND_LONG':
                        # 93: MAV_CMD_NAV_DELAY
                        # 19: MAV_CMD_NAV_LOITER_TIME
                        # 184: MAV_CMD_DO_REPEAT_SERVO (Common custom trigger)
                        if msg.command in [93, 19, 184]:
                            # param1 for 93/19 is usually time, param2 for 184
                            duration = DEFAULT_AUTO_SAMPLING_SECONDS
                            if msg.command == 184:
                                duration = int(msg.param2) if int(msg.param2) > 0 else duration
                            else:
                                duration = int(msg.param1) if int(msg.param1) > 0 else duration
                                
                            log.info(f"[PIXHAWK] Mission Command {msg.command} detected: {duration}s")
                            
                            if not sampler.get_status()['active']:
                                if active_session_id is None:
                                    begin_operation_session(source=f'pixhawk_cmd_{msg.command}')
                                sampler.start_sampling(duration)
                                delay_active = True
                                with lock:
                                    mode_before_delay = state.get('pixhawk_mode', 'UNKNOWN')
                                    state['pixhawk_mode'] = 'DELAY'
                                update_sampling_state()
                    
                    elif msg_type == 'COMMAND_ACK':
                        if msg.command in [93, 19, 184] and delay_active:
                            log.info(f"[PIXHAWK] Command {msg.command} finished/ACKed")
                            finish_sampling(reason='pixhawk_ack')
                            delay_active = False
                            with lock:
                                state['pixhawk_mode'] = mode_before_delay
            
            except Exception as e:
                log.debug(f"[PIXHAWK] Message processing error: {e}")
            
            update_sampling_state()
            
            time.sleep(0.5)
        
        except Exception as e:
            log.error(f"[PIXHAWK] Unexpected error: {e}")
            with lock:
                state['pixhawk_ok'] = False
                state['pixhawk_mode'] = 'ERROR'
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
            snap['ts'] = now_thailand.strftime('%H:%M:%S')
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
            state['garmin_lat'] = 13.7563 + (t * 0.0001)
            state['garmin_lon'] = 100.5018 + (t * 0.0001)
            
            # Pixhawk GPS with a small random error
            err = math.sin(t * 0.2) * 0.00005
            state['pixhawk_lat'] = state['garmin_lat'] + err
            state['pixhawk_lon'] = state['garmin_lon'] + err
            
            state['lat']       = state['pixhawk_lat']
            state['lon']       = state['pixhawk_lon']
            
            garmin_no_fix = (state['garmin_lat'] == 0 and state['garmin_lon'] == 0)
            pixhawk_no_fix = (state['pixhawk_lat'] == 0 and state['pixhawk_lon'] == 0)
            
            if garmin_no_fix and pixhawk_no_fix:
                state['gps_status'] = 'no_fix'
                state['gps_match'] = False
            elif garmin_no_fix:
                state['gps_status'] = 'no_garmin'
                state['gps_match'] = False
            else:
                dist = haversine_distance(state['pixhawk_lat'], state['pixhawk_lon'], state['garmin_lat'], state['garmin_lon'])
                state['gps_error_m'] = round(dist, 2)
                state['gps_match'] = (dist < 10.0)
                state['gps_status'] = 'ok' if state['gps_match'] else 'warn'

            state['depth']     = round(2.0 + math.sin(t * 0.1) * 1.5, 2)
            state['ph']        = round(7.0 + math.sin(t * 0.05) * 0.4, 2)
            state['do']        = round(6.5 + math.sin(t * 0.08) * 1.0, 2)
            state['temp']      = round(28.0 + math.sin(t * 0.03) * 2.0, 2)
            state['uart_ok']   = True
            state['arduino_ok'] = True
            state['arduino_status'] = 'ok'
            state['arduino_error'] = ''
            state['pixhawk_mode'] = 'SIM'
            state['ts']        = now_thailand.strftime('%H:%M:%S')

        socketio.emit('sensor_update', dict(state))
        time.sleep(FUSION_INTERVAL)

# ===========================================================
# FLASK ROUTES
# ===========================================================

@app.route('/')
def index():
    try:
        return render_template('index.html')
    except Exception as e:
        log.error(f"[TEMPLATE] Error rendering index.html: {e}")
        return f"Error loading dashboard: {e}", 500

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/api/history')
def api_history():
    return jsonify(db.get_recent(50))

@app.route('/api/download_csv')
def download_csv():
    try:
        path = db.export_csv()
        
        # ตั้ง headers สำหรับ ngrok
        response = send_file(path, as_attachment=True,
                            download_name=os.path.basename(path))
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        response.headers['Content-Disposition'] = f'attachment; filename="{os.path.basename(path)}"'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        
        return response
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

@app.route('/api/export/sessions')
def export_sessions():
    try:
        return jsonify({'ok': True, 'sessions': db.list_sessions()})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

@app.route('/api/export/session_download/<int:session_id>')
def export_session_download(session_id):
    try:
        path = db.export_session(session_id)
        return send_file(path, as_attachment=True,
                         download_name=os.path.basename(path))
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500
    
@app.route('/api/export/date_range', methods=['POST'])
def export_date_range():
    """
    Export CSV for specific date range
    
    Request body:
    {
        "start_date": "2024-12-15",
        "end_date": "2024-12-16",
        "filename": "custom_name.csv" (optional)
    }
    """
    try:
        data = request.get_json()
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        filename = data.get('filename')
        
        if not start_date or not end_date:
            return jsonify({'ok': False, 'msg': 'start_date and end_date required'}), 400
        
        path = db.export_csv(filename=filename, start_date=start_date, end_date=end_date)
        
        return jsonify({
            'ok': True,
            'msg': 'Export successful',
            'filename': os.path.basename(path),
            'path': path
        })
    
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500
 
@app.route('/api/export/time_range', methods=['POST'])
def export_time_range():
    """
    Export CSV for specific time range (with hours/minutes)
    
    Request body:
    {
        "start_datetime": "2024-12-15 10:30:00",
        "end_datetime": "2024-12-15 15:45:00",
        "filename": "5-15-24_103000_to_154500.csv" (optional)
    }
    """
    try:
        data = request.get_json()
        start_datetime = data.get('start_datetime')
        end_datetime = data.get('end_datetime')
        filename = data.get('filename')
        
        if not start_datetime or not end_datetime:
            return jsonify({'ok': False, 'msg': 'start_datetime and end_datetime required'}), 400
        
        path = db.export_csv_custom(start_datetime, end_datetime, filename=filename)
        
        return jsonify({
            'ok': True,
            'msg': 'Export successful',
            'filename': os.path.basename(path),
            'path': path
        })
    
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500
 
@app.route('/api/export/date_range_download', methods=['POST'])
def export_date_range_download():
    try:
        data = request.get_json()
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        
        if not start_date or not end_date:
            return jsonify({'ok': False, 'msg': 'start_date and end_date required'}), 400
        
        path = db.export_csv(filename=None, start_date=start_date, end_date=end_date)
        
        response = send_file(path, as_attachment=True,
                            download_name=os.path.basename(path))
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        response.headers['Content-Disposition'] = f'attachment; filename="{os.path.basename(path)}"'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        
        return response
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500
 

@app.route('/api/export/time_range_download', methods=['POST'])
def export_time_range_download():
    try:
        data = request.get_json()
        start_datetime = data.get('start_datetime')
        end_datetime = data.get('end_datetime')
        
        if not start_datetime or not end_datetime:
            return jsonify({'ok': False, 'msg': 'start_datetime and end_datetime required'}), 400
        
        path = db.export_csv_custom(start_datetime, end_datetime)
        
        response = send_file(path, as_attachment=True,
                            download_name=os.path.basename(path))
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        response.headers['Content-Disposition'] = f'attachment; filename="{os.path.basename(path)}"'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        
        return response
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500
 
@app.route('/api/database/info')
def database_info():
    """
    Get database information (date range, record count)
    """
    try:
        conn = sqlite3.connect(db.DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) as count FROM asv_logs')
        count = cursor.fetchone()[0]
        
        date_range = db.get_date_range()
        
        conn.close()
        
        return jsonify({
            'ok': True,
            'total_records': count,
            'earliest_record': date_range['earliest'],
            'latest_record': date_range['latest']
        })
    
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
    if active_session_id is None:
        begin_operation_session(source='manual_sampling')
    sampler.start_sampling(seconds)
    update_sampling_state()
    return jsonify({'ok': True, 'msg': f'Sampling started for {seconds}s'})

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
        return jsonify({'ok': True, 'msg': f'Upload สำเร็จ {len(waypoints)} waypoints'})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

@app.route('/api/start_mission', methods=['POST'])
def start_mission():
    session = begin_operation_session(source='mission')
    return jsonify({
        'ok': True,
        'msg': f'Mission started - operation #{session["id"]}',
        'session': session
    })


@app.route('/api/sampling/stop', methods=['POST'])
def sampling_stop():
    log.info("[API] Manual sampling stop")
    result = finish_sampling(reason='manual_stop', force=True)
    return jsonify(result), (200 if result.get('ok') else 400)

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
    threading.Thread(target=sampling_watch_thread, daemon=True).start()
    
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
