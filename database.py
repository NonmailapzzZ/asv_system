"""
database.py — จัดการ SQLite สำหรับ ASV Data Fusion System
ตาราง: asv_logs (id, timestamp, lat, lon, depth, ph, do, temp)
"""

import sqlite3
import csv
import os
from datetime import datetime

DB_PATH  = os.path.join(os.path.dirname(__file__), 'logs', 'asv_data.db')
CSV_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'asv_export.csv')


def init_db():
    """สร้างตารางถ้ายังไม่มี"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS asv_logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                lat       REAL    DEFAULT 0.0,
                lon       REAL    DEFAULT 0.0,
                depth     REAL    DEFAULT 0.0,
                ph        REAL    DEFAULT 0.0,
                do_val    REAL    DEFAULT 0.0,
                temp      REAL    DEFAULT 0.0
            )
        """)
        conn.commit()
    print(f"[DB] Initialized: {DB_PATH}")


def insert_record(lat, lon, depth, ph, do_val, temp):
    """บันทึกข้อมูล fused record 1 แถว"""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO asv_logs (timestamp, lat, lon, depth, ph, do_val, temp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ts, lat, lon, depth, ph, do_val, temp))
            conn.commit()
    except Exception as e:
        print(f"[DB] Insert error: {e}")


def get_recent(limit=100):
    """ดึงข้อมูลล่าสุด N แถว (สำหรับ chart)"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM asv_logs ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in reversed(rows)]
    except Exception as e:
        print(f"[DB] Query error: {e}")
        return []


def export_csv():
    """Export ทุกแถวเป็น CSV แล้วคืน path"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM asv_logs ORDER BY id").fetchall()
        with open(CSV_PATH, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'timestamp', 'lat', 'lon', 'depth', 'ph', 'do', 'temp'])
            for r in rows:
                writer.writerow([r['id'], r['timestamp'], r['lat'], r['lon'],
                                  r['depth'], r['ph'], r['do_val'], r['temp']])
        print(f"[DB] Exported {len(rows)} rows → {CSV_PATH}")
        return CSV_PATH
    except Exception as e:
        print(f"[DB] Export error: {e}")
        return None


def get_stats():
    """สถิติรวมสำหรับแสดงบน dashboard"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("""
                SELECT COUNT(*) as total,
                       MIN(timestamp) as first,
                       MAX(timestamp) as last
                FROM asv_logs
            """).fetchone()
            return {'total': row[0], 'first': row[1], 'last': row[2]}
    except Exception:
        return {'total': 0, 'first': None, 'last': None}
