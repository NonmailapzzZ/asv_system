"""
database.py - Database operations for ASV system

Modified to support:
1. Timestamp-based queries (date range)
2. Custom CSV export with timestamp naming
3. Better date filtering
"""

import sqlite3
import csv
import os
from datetime import datetime, timedelta, timezone
import time

CSV_MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
CSV_MAX_AGE_DAYS = 21

TH_TZ = timezone(timedelta(hours=7))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# ตั้งค่า LOG_DIR ให้ไปที่โฟลเดอร์ logs ในโปรเจกต์
LOG_DIR = os.path.join(BASE_DIR, 'logs')
DB_PATH = os.path.join(LOG_DIR, 'asv_data.db')


def init_db():
    """Initialize database with asv_logs table"""
    os.makedirs(LOG_DIR, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS asv_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session_id INTEGER,
            lat REAL,
            lon REAL,
            depth REAL,
            ph REAL,
            do_val REAL,
            temp REAL
        )
    ''')

    cursor.execute('PRAGMA table_info(asv_logs)')
    columns = [row[1] for row in cursor.fetchall()]
    if 'session_id' not in columns:
        cursor.execute('ALTER TABLE asv_logs ADD COLUMN session_id INTEGER')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS operation_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            label TEXT,
            source TEXT
        )
    ''')
    
    conn.commit()
    conn.close()
    cleanup_csv_logs()

def cleanup_csv_logs(max_total_bytes=CSV_MAX_TOTAL_BYTES, max_age_days=CSV_MAX_AGE_DAYS, exclude_paths=None):
    if not os.path.isdir(LOG_DIR):
        return {'deleted': [], 'total_bytes': 0}

    excluded = {
        os.path.abspath(path)
        for path in (exclude_paths or [])
        if path
    }
    
    # แก้ไข: ใช้ Timestamp (วินาที) แทน Datetime Object
    now_ts = time.time()
    max_age_seconds = max_age_days * 24 * 60 * 60
    deleted = []

    def csv_files():
        files = []
        for name in os.listdir(LOG_DIR):
            path = os.path.join(LOG_DIR, name)
            abs_path = os.path.abspath(path)
            if abs_path in excluded:
                continue
            if not os.path.isfile(path) or not name.lower().endswith('.csv'):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            files.append({
                'path': path,
                'name': name,
                'size': stat.st_size,
                'mtime': stat.st_mtime, # เก็บเป็น float วินาที
            })
        return files

    def remove_csv(path, name):
        try:
            os.remove(path)
            deleted.append(name)
            return True
        except OSError:
            return False

    # ลบไฟล์ตามอายุ (ตัวเลขลบตัวเลข)
    for item in csv_files():
        if now_ts - item['mtime'] > max_age_seconds:
            remove_csv(item['path'], item['name'])

    # จัดการขนาดไฟล์รวม
    files = sorted(csv_files(), key=lambda item: item['mtime'])
    total_bytes = sum(item['size'] for item in files)

    for item in files:
        if total_bytes <= max_total_bytes:
            break
        if remove_csv(item['path'], item['name']):
            total_bytes -= item['size']

    return {'deleted': deleted, 'total_bytes': total_bytes}

def insert_record(lat, lon, depth, ph, do_val, temp, session_id=None):
    # ป้องกัน Errno 2: สร้างโฟลเดอร์ logs ก่อนเสมอ
    os.makedirs(LOG_DIR, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # บันทึกเป็นเวลาไทย UTC+7
    timestamp = datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute('''
        INSERT INTO asv_logs 
        (timestamp, session_id, lat, lon, depth, ph, do_val, temp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (timestamp, session_id, lat, lon, depth, ph, do_val, temp))
    
    conn.commit()
    conn.close()

def start_session(label=None, source='manual'):
    os.makedirs(LOG_DIR, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ใช้เวลาไทย UTC+7
    now_th = datetime.now(TH_TZ)
    started_at = now_th.strftime('%Y-%m-%d %H:%M:%S')
    
    if not label:
        label = f"Operation {now_th.strftime('%d-%m-%y %H:%M:%S')}"

    cursor.execute('''
        INSERT INTO operation_sessions (started_at, label, source)
        VALUES (?, ?, ?)
    ''', (started_at, label, source))

    session_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return {
        'id': session_id,
        'started_at': started_at,
        'label': label,
        'source': source,
    }

def list_sessions():
    """List operation sessions with record counts for the export picker."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('''
        SELECT
            s.id,
            s.started_at,
            s.label,
            s.source,
            COUNT(l.id) AS record_count,
            MIN(l.timestamp) AS first_record,
            MAX(l.timestamp) AS last_record
        FROM operation_sessions s
        LEFT JOIN asv_logs l ON l.session_id = s.id
        GROUP BY s.id
        ORDER BY s.started_at DESC
    ''')

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def get_recent(limit=50):
    """Get last N records for dashboard"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, timestamp, lat, lon, depth, ph, do_val, temp
        FROM asv_logs
        ORDER BY id DESC
        LIMIT ?
    ''', (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in reversed(rows)]

def get_by_date_range(start_date, end_date):
    """
    Get records between two dates
    
    Args:
        start_date: datetime object or 'YYYY-MM-DD' string
        end_date: datetime object or 'YYYY-MM-DD' string
    
    Returns:
        List of records
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    if isinstance(start_date, datetime):
        start_date = start_date.strftime('%Y-%m-%d')
    if isinstance(end_date, datetime):
        end_date = end_date.strftime('%Y-%m-%d')
    
    cursor.execute('''
        SELECT id, timestamp, lat, lon, depth, ph, do_val, temp
        FROM asv_logs
        WHERE DATE(timestamp) BETWEEN ? AND ?
        ORDER BY timestamp ASC
    ''', (start_date, end_date))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]

def get_by_time_range(start_datetime, end_datetime):
    """
    Get records between two datetimes (with time component)
    
    Args:
        start_datetime: datetime object or 'YYYY-MM-DD HH:MM:SS' string
        end_datetime: datetime object or 'YYYY-MM-DD HH:MM:SS' string
    
    Returns:
        List of records
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    if isinstance(start_datetime, datetime):
        start_datetime = start_datetime.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(end_datetime, datetime):
        end_datetime = end_datetime.strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute('''
        SELECT id, timestamp, lat, lon, depth, ph, do_val, temp
        FROM asv_logs
        WHERE timestamp BETWEEN ? AND ?
        ORDER BY timestamp ASC
    ''', (start_datetime, end_datetime))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]

def export_csv(filename=None, start_date=None, end_date=None):
    """
    Export records to CSV with optional date filtering
    
    Args:
        filename: Custom filename (default: auto-generated with timestamp)
        start_date: Start date filter (optional)
        end_date: End date filter (optional)
    
    Returns:
        Path to exported CSV file
    """
    if filename is None:
        now = datetime.now(TH_TZ)
        filename = f"asv_log_{now.strftime('%d-%m-%y_%H%M%S')}.csv"
    
    filepath = os.path.join(LOG_DIR, filename)
    
    if start_date and end_date:
        records = get_by_date_range(start_date, end_date)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT id, timestamp, session_id, lat, lon, depth, ph, do_val, temp FROM asv_logs ORDER BY timestamp ASC')
        records = [dict(row) for row in cursor.fetchall()]
        conn.close()
    
    if not records:
        raise ValueError("No records found to export")
    
    with open(filepath, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['id', 'timestamp', 'session_id', 'lat', 'lon', 'depth', 'ph', 'do_val', 'temp'])
        writer.writeheader()
        writer.writerows(records)

    cleanup_csv_logs(exclude_paths=[filepath])
    
    return filepath

def export_csv_custom(start_datetime, end_datetime, filename=None):
    """
    Export records between specific times with custom filename
    
    Args:
        start_datetime: Start datetime ('YYYY-MM-DD HH:MM:SS' or datetime object)
        end_datetime: End datetime ('YYYY-MM-DD HH:MM:SS' or datetime object)
        filename: Custom filename (default: auto-generated)
    
    Returns:
        Path to exported CSV file
    """
    if filename is None:
        if isinstance(start_datetime, str):
            start_str = start_datetime.replace(' ', '_').replace(':', '')
        else:
            start_str = start_datetime.strftime('%d-%m-%y_%H%M%S')
        
        if isinstance(end_datetime, str):
            end_str = end_datetime.replace(' ', '_').replace(':', '')
        else:
            end_str = end_datetime.strftime('%d-%m-%y_%H%M%S')
        
        filename = f"asv_log_{start_str}_to_{end_str}.csv"
    
    filepath = os.path.join(LOG_DIR, filename)
    
    records = get_by_time_range(start_datetime, end_datetime)
    
    if not records:
        raise ValueError(f"No records found between {start_datetime} and {end_datetime}")
    
    with open(filepath, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['id', 'timestamp', 'lat', 'lon', 'depth', 'ph', 'do_val', 'temp'])
        writer.writeheader()
        writer.writerows(records)

    cleanup_csv_logs(exclude_paths=[filepath])
    
    return filepath

def export_session(session_id, filename=None):
    """Export records for a selected operation session."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('SELECT id, started_at, label FROM operation_sessions WHERE id = ?', (session_id,))
    session = cursor.fetchone()
    if not session:
        conn.close()
        raise ValueError("Operation session not found")

    cursor.execute('''
        SELECT id, timestamp, session_id, lat, lon, depth, ph, do_val, temp
        FROM asv_logs
        WHERE session_id = ?
        ORDER BY timestamp ASC
    ''', (session_id,))
    records = [dict(row) for row in cursor.fetchall()]
    conn.close()

    if not records:
        raise ValueError("No records found for selected operation")

    if filename is None:
        started = session['started_at'].replace('-', '').replace(' ', '_').replace(':', '')
        filename = f"asv_operation_{session_id}_{started}.csv"

    filepath = os.path.join(LOG_DIR, filename)
    with open(filepath, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['id', 'timestamp', 'session_id', 'lat', 'lon', 'depth', 'ph', 'do_val', 'temp'])
        writer.writeheader()
        writer.writerows(records)

    cleanup_csv_logs(exclude_paths=[filepath])

    return filepath

def get_date_range():
    """
    Get earliest and latest timestamp in database
    
    Returns:
        dict: {'earliest': datetime, 'latest': datetime}
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('SELECT MIN(timestamp) as earliest, MAX(timestamp) as latest FROM asv_logs')
    result = cursor.fetchone()
    conn.close()
    
    return {
        'earliest': result[0],
        'latest': result[1]
    }

def delete_records_before(timestamp):
    """Delete records older than specified timestamp (cleanup function)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('DELETE FROM asv_logs WHERE timestamp < ?', (timestamp,))
    
    deleted_count = cursor.rowcount
    conn.commit()
    conn.close()
    
    return deleted_count
