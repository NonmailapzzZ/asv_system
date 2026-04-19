"""
camera.py — MJPEG streaming from webcam
"""
import cv2
import threading
import time

class CameraManager:
    def __init__(self):
        self._lock    = threading.Lock()
        self._cap     = None
        self._frame   = None
        self._running = False
        self._channel = 0
        self._thread  = None

    def list_devices(self):
        found = []
        for i in range(6):
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                found.append(i)
                cap.release()
        return found

    def start(self, channel=0):
        self.stop()
        self._channel = channel
        self._running = True
        self._thread  = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        print(f"[CAM] Started on channel {channel}")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            if self._cap:
                self._cap.release()
                self._cap = None
            self._frame = None

    def _capture_loop(self):
        cap = cv2.VideoCapture(self._channel)
        if not cap.isOpened():
            print(f"[CAM] Cannot open channel {self._channel}")
            self._running = False
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 20)

        with self._lock:
            self._cap = cap

        while self._running:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.1)
                continue
            with self._lock:
                self._frame = frame
            time.sleep(0.04)

        cap.release()
        with self._lock:
            self._cap   = None
            self._frame = None

    def get_jpeg(self):
        with self._lock:
            if self._frame is None:
                return None
            ok, buf = cv2.imencode('.jpg', self._frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, 70])
            return buf.tobytes() if ok else None

    @property
    def is_running(self):
        return self._running and self._frame is not None

camera_manager = CameraManager()
