"""
mission_manager.py — อ่านไฟล์ .waypoints และ upload ให้ Pixhawk
รองรับ ArduPilot QGroundControl waypoint format
"""

import time
from pymavlink import mavutil, mavwp

# ---- state ที่แชร์กับ app.py ----
mission_status = {
    'state':    'idle',   # idle | uploading | verifying | ready | error
    'message':  '',
    'total':    0,
    'uploaded': 0,
}


def parse_waypoints_file(filepath):
    """
    อ่านไฟล์ .waypoints (ArduPilot format)
    format ต่อบรรทัด:
      index current frame command param1..4 lat lon alt autocontinue
    คืนค่า list of dict หรือ raise Exception ถ้า format ผิด
    """
    waypoints = []
    try:
        with open(filepath, 'r') as f:
            lines = f.readlines()

        if not lines or not lines[0].strip().startswith('QGC WPL'):
            raise ValueError("ไฟล์ไม่ใช่ QGC WPL format")

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
                'param1':       float(parts[4]),
                'param2':       float(parts[5]),
                'param3':       float(parts[6]),
                'param4':       float(parts[7]),
                'lat':          float(parts[8]),
                'lon':          float(parts[9]),
                'alt':          float(parts[10]),
                'autocontinue': int(parts[11]),
            })
        print(f"[MISSION] Parsed {len(waypoints)} waypoints")
        return waypoints

    except Exception as e:
        raise Exception(f"Parse error: {e}")


def upload_mission(master, waypoints):
    """
    Upload mission ไปยัง Pixhawk พร้อม verify
    master = mavutil connection object จาก app.py
    """
    global mission_status

    if not waypoints:
        mission_status.update({'state': 'error', 'message': 'ไม่มี waypoint'})
        return False

    total = len(waypoints)
    mission_status.update({'state': 'uploading', 'total': total, 'uploaded': 0, 'message': 'กำลัง upload...'})

    try:
        # ---- 1. ส่งจำนวน waypoint ----
        master.mav.mission_count_send(
            master.target_system,
            master.target_component,
            total,
            mavutil.mavlink.MAV_MISSION_TYPE_MISSION
        )

        # ---- 2. ส่งทีละ waypoint เมื่อ Pixhawk ขอ ----
        for _ in range(total + 5):
            msg = master.recv_match(
                type=['MISSION_REQUEST', 'MISSION_REQUEST_INT', 'MISSION_ACK'],
                blocking=True, timeout=5
            )
            if msg is None:
                raise TimeoutError("Pixhawk ไม่ตอบสนอง (timeout)")

            if msg.get_type() == 'MISSION_ACK':
                if msg.type == mavutil.mavlink.MAV_MISSION_ACCEPTED:
                    break
                else:
                    raise Exception(f"Pixhawk ปฏิเสธ mission: code {msg.type}")

            seq = msg.seq
            wp  = waypoints[seq]
            master.mav.mission_item_int_send(
                master.target_system,
                master.target_component,
                wp['seq'],
                wp['frame'],
                wp['command'],
                wp['current'],
                wp['autocontinue'],
                wp['param1'], wp['param2'], wp['param3'], wp['param4'],
                int(wp['lat'] * 1e7),
                int(wp['lon'] * 1e7),
                wp['alt'],
                mavutil.mavlink.MAV_MISSION_TYPE_MISSION
            )
            mission_status['uploaded'] = seq + 1
            mission_status['message'] = f"Upload {seq+1}/{total}"

        # ---- 3. Verify: อ่าน mission กลับมาเช็ค ----
        mission_status.update({'state': 'verifying', 'message': 'กำลัง verify...'})
        verified = _verify_mission(master, waypoints)

        if verified:
            mission_status.update({'state': 'ready', 'message': f'Mission ready — {total} waypoints verified'})
            print(f"[MISSION] Upload + verify สำเร็จ ({total} waypoints)")
            return True
        else:
            mission_status.update({'state': 'error', 'message': 'Verify ไม่ผ่าน — กรุณา upload ใหม่'})
            return False

    except Exception as e:
        msg = str(e)
        mission_status.update({'state': 'error', 'message': msg})
        print(f"[MISSION] Upload error: {msg}")
        return False


def _verify_mission(master, original):
    """
    อ่าน mission กลับจาก Pixhawk แล้วเปรียบเทียบ lat/lon กับต้นฉบับ
    คืน True ถ้าตรงกัน
    """
    try:
        master.mav.mission_request_list_send(
            master.target_system,
            master.target_component,
            mavutil.mavlink.MAV_MISSION_TYPE_MISSION
        )
        count_msg = master.recv_match(type='MISSION_COUNT', blocking=True, timeout=5)
        if not count_msg or count_msg.count != len(original):
            print(f"[MISSION] Verify: count mismatch ({count_msg.count if count_msg else '?'} vs {len(original)})")
            return False

        for i in range(count_msg.count):
            master.mav.mission_request_int_send(
                master.target_system,
                master.target_component,
                i,
                mavutil.mavlink.MAV_MISSION_TYPE_MISSION
            )
            item = master.recv_match(type='MISSION_ITEM_INT', blocking=True, timeout=5)
            if not item:
                return False
            recv_lat = item.x / 1e7
            recv_lon = item.y / 1e7
            orig_lat = original[i]['lat']
            orig_lon = original[i]['lon']
            if abs(recv_lat - orig_lat) > 0.00001 or abs(recv_lon - orig_lon) > 0.00001:
                print(f"[MISSION] Verify fail at seq {i}: got ({recv_lat},{recv_lon}) expected ({orig_lat},{orig_lon})")
                return False

        master.mav.mission_ack_send(
            master.target_system,
            master.target_component,
            mavutil.mavlink.MAV_MISSION_ACCEPTED,
            mavutil.mavlink.MAV_MISSION_TYPE_MISSION
        )
        return True

    except Exception as e:
        print(f"[MISSION] Verify error: {e}")
        return False


def arm_and_start(master):
    """Arm Pixhawk และ set AUTO mode เพื่อเริ่ม mission"""
    try:
        # set mode AUTO
        master.set_mode('AUTO')
        time.sleep(0.5)
        # arm
        master.arducopter_arm()
        print("[MISSION] Armed + AUTO mode set")
        mission_status['message'] = 'Mission started'
        return True
    except Exception as e:
        print(f"[MISSION] Arm error: {e}")
        return False
