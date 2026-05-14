Changelog (Update)
การเปลี่ยนแปลงในเวอร์ชันนี้มุ่งเน้นไปที่ความฉลาดของระบบการเก็บตัวอย่าง (Smart Sampling) เพื่อลดภาระของผู้ใช้งานและเพิ่มความแม่นยำของข้อมูล โดยการเพิ่มระบบตรวจจับโหมดการบินและระบบหยุดการทำงานอัตโนมัติ
[Latest Update] - Smart Sampling & Automation
Added
    Auto-Sampling Triggers:
        LOITER Mode Detection: เมื่อเปลี่ยนโหมดเป็น LOITER ระบบจะเริ่ม Sampling อัตโนมัติ (ใช้ระยะเวลาเริ่มต้น 30 วินาที) เพื่อเก็บข้อมูล ณ จุดที่เรือหยุดนิ่ง
        MAVLink Delay Command (184): ปรับปรุงการดึงค่าระยะเวลา (param2) จากคำสั่ง Delay ของ Pixhawk หากไม่มีการระบุค่าจะใช้ค่า Default (30 วินาที) แทน
    Sampling Watchdog Thread:
        เพิ่ม sampling_watch_thread ใน app.py เพื่อตรวจสอบเวลาคงเหลือของการ Sampling แบบ Real-time
        ระบบจะเรียกฟังก์ชัน finish_sampling โดยอัตโนมัติเมื่อ Timer เป็น 0 ทำให้สถานะใน Dashboard กลับเป็น Idle และบันทึกข้อมูลลงฐานข้อมูลโดยไม่ต้องกด Stop เอง
    Thread Safety:
        เพิ่ม sampling_finish_lock (threading.Lock) เพื่อป้องกันการเข้าถึงฟังก์ชันบันทึกข้อมูลซ้ำซ้อนในเวลาเดียวกัน (Race Condition)
Changed
    Sampling Logic (app.py):
        รวมศูนย์ฟังก์ชันการหยุด Sampling ไว้ที่ finish_sampling(reason, force) เพื่อให้รองรับทั้งการหยุดจาก Timer, จากคำสั่ง Pixhawk (Delay ended) และจากผู้ใช้งาน (Manual Stop)
        ปรับปรุงฟังก์ชัน update_sampling_state ให้ซิงค์สถานะระหว่าง SamplingManager และ WebSocket ของ Dashboard ตลอดเวลา
    API Endpoints:
        ปรับปรุง /api/sampling/stop ให้เรียกใช้ finish_sampling เพื่อมาตรฐานเดียวกันในการบันทึกข้อมูลและจัดการ Buffer
    Documentation (readthis.md):
        อัปเดตโครงสร้างระบบเป็น 4 Threads หลัก: uart_thread, pixhawk_thread, fusion_thread และ sampling_watch_thread
Fixed
    UI Consistency:
        แก้ไขปัญหาปุ่ม Start/Stop ในหน้า Dashboard ไม่เปลี่ยนสถานะตามความเป็นจริง เมื่อระบบหยุด Sampling อัตโนมัติด้วย Timer
        ปรับปรุงการบันทึกฐานข้อมูลให้บันทึกเพียง 1 Record ต่อ 1 จุดสถานี (เฉลี่ยจากกลุ่มตัวอย่าง) ช่วยลดขนาดฐานข้อมูลลงกว่า 70% และเพิ่มคุณภาพข้อมูล
สรุปกลไกการทำงานใหม่ (Smart Sampling Flow)
    Trigger: Pixhawk สั่ง DELAY หรือเข้าโหมด LOITER -> เริ่มสร้าง operation_session
    Collect: ข้อมูลจากเซนเซอร์ (pH, DO, Temp, Depth) จะถูกเก็บเข้า Buffer (ไม่บันทึกลง DB ทันที)
    Monitor: sampling_watch_thread คอยนับถอยหลังเวลา
    Auto-Save: เมื่อครบเวลา ระบบคำนวณค่าเฉลี่ย (Average) และบันทึกลงฐานข้อมูล 1 Record พร้อมเชื่อมโยง session_id
    Idle: ระบบและ UI กลับสู่สถานะพร้อมทำงานต่อโดยอัตโนมัติ