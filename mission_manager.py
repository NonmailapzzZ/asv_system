"""
mission_manager.py — .waypoints upload to Pixhawk
"""
import time
from pymavlink import mavutil

def parse_waypoints_file(filepath):
    waypoints = []
    with open(filepath, 'r') as f:
        lines = f.readlines()

    if not lines or not lines[0].strip().startswith('QGC WPL'):
        raise ValueError("Not ArduPilot .waypoints format")

    for line in lines[1:]:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) < 12:
            continue
        waypoints.append({
            'seq':          int(parts[0]),
            'current':      int(parts[1]),
            'frame':        int(parts[2]),
            'command':      int(parts[3]),
            'p1':           float(parts[4]),
            'p2':           float(parts[5]),
            'p3':           float(parts[6]),
            'p4':           float(parts[7]),
            'lat':          float(parts[8]),
            'lon':          float(parts[9]),
            'alt':          float(parts[10]),
            'autocontinue': int(parts[11]),
        })
    print(f"[MISSION] Parsed {len(waypoints)} waypoints")
    return waypoints

def upload_mission(master, waypoints, timeout=10):
    count = len(waypoints)
    master.waypoint_count_send(count)
    print(f"[MISSION] Sending {count} waypoints...")

    acked = 0
    deadline = time.time() + timeout

    while acked < count:
        if time.time() > deadline:
            raise TimeoutError(f"Timeout at waypoint {acked}")

        msg = master.recv_match(
            type=['MISSION_REQUEST', 'MISSION_REQUEST_INT'],
            blocking=True, timeout=3
        )
        if not msg:
            continue

        seq = msg.seq
        wp  = waypoints[seq]

        master.mav.mission_item_send(
            master.target_system,
            master.target_component,
            wp['seq'],
            wp['frame'],
            wp['command'],
            wp['current'],
            wp['autocontinue'],
            wp['p1'], wp['p2'], wp['p3'], wp['p4'],
            wp['lat'], wp['lon'], wp['alt']
        )
        acked = seq + 1
        print(f"[MISSION] Sent waypoint {seq+1}/{count}")

    ack = master.recv_match(type='MISSION_ACK', blocking=True, timeout=5)
    if not ack or ack.type != 0:
        raise RuntimeError(f"Upload failed, ACK type={ack.type if ack else 'none'}")

    print("[MISSION] Upload complete. Verifying...")
    master.mav.mission_request_list_send(
        master.target_system, master.target_component
    )
    count_msg = master.recv_match(type='MISSION_COUNT', blocking=True, timeout=5)
    if not count_msg:
        raise RuntimeError("No MISSION_COUNT response")
    if count_msg.count != count:
        raise RuntimeError(f"Mismatch! Sent {count} but Pixhawk has {count_msg.count}")

    print(f"[MISSION] Verified OK — {count} waypoints")
    return True

def start_mission(master):
    master.set_mode('AUTO')
    print("[MISSION] AUTO mode set")
