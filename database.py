"""
database.py — SQLite management for ASV
"""
import sqlite3
import csv
import os
from datetime import datetime

DB_PATH = 'logs/asv_data.db'
CSV_DIR = 'logs'

def get_connection():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(CSV_DIR, exist_ok=True)
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS asv_logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                lat       REAL    DEFAULT 0.0,
                lon       REAL    DEFAULT 0.0,
                depth     REAL    DEFAULT 0.0,
                ph        REAL    DEFAULT 0.0,
                do        REAL    DEFAULT 0.0,
                temp      REAL    DEFAULT 0.0
            )
        """)
        conn.commit()
    print("[DB] Database initialized")

def insert_record(lat, lon, depth, ph, do_val, temp):
    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO asv_logs (timestamp, lat, lon, depth, ph, do, temp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ts, lat, lon, depth, ph, do_val, temp))
        conn.commit()

def get_recent(limit=50):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM asv_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]

def export_csv():
    os.makedirs(CSV_DIR, exist_ok=True)
    filename = f"asv_log_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    filepath = os.path.join(CSV_DIR, filename)
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM asv_logs ORDER BY id ASC").fetchall()
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['id','timestamp','lat','lon','depth','ph','do','temp'])
        for r in rows:
            writer.writerow([r['id'],r['timestamp'],r['lat'],r['lon'],
                             r['depth'],r['ph'],r['do'],r['temp']])
    print(f"[DB] Exported {len(rows)} records -> {filepath}")
    return filepath
