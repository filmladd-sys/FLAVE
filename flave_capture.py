# flave_capture.py
# FLAVE Capture: standalone screen recorder shipped with FLAVE (Windows).
# Run: python flave_capture.py
#
# One small window: output folder, FPS, and a Start / Stop button (enabled
# only when the folder exists and FPS is a positive number). Start launches
# one FFmpeg ddagrab (Desktop Duplication API) process per monitor at that
# monitor's native pixel size, video only (no audio). Frames stay on the
# GPU (D3D11) from capture into NVIDIA NVENC
# (h264_nvenc, fastest preset p1, low-latency tune), one file each:
#   Monitor<N>_<W>x<H>_YYYYMMDD_HHMMSS.mp4
# Stop sends "q" to every FFmpeg so each MP4 is finalized, then kills any
# that do not exit in time. The last folder and FPS are kept in
# flave_capture.json next to this script.
#
# Needs Windows 8+, an NVIDIA GPU and an FFmpeg build with ddagrab (5.1+)
# and h264_nvenc; if either is missing FFmpeg exits and the title bar says
# it failed. ddagrab only sees the outputs of the first GPU adapter.
# Kept light so FLAVE can run in the foreground: FFmpeg runs at below
# normal priority with no console window, its output goes nowhere, and the
# window polls the processes once a second only while recording.
# FFmpeg must be on PATH (same as FLAVE). ASCII only.

import ctypes
import json
import os
import subprocess
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QDoubleValidator, QGuiApplication
from PySide6.QtWidgets import (QApplication, QFormLayout, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "flave_capture.json")
DEFAULT_FPS = "30"
STOP_WAIT_S = 5.0          # per process, after "q", before kill
POLL_MS = 1000             # process check while recording

# Windows process flags (0 elsewhere so the module still imports)
BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="ascii") as f:
            data = json.load(f)
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(folder, fps):
    try:
        with open(SETTINGS_PATH, "w", encoding="ascii") as f:
            json.dump({"folder": folder, "fps": fps}, f)
    except Exception:
        pass


def parse_fps(text):
    # Positive float or None
    try:
        v = float((text or "").strip())
    except ValueError:
        return None
    return v if v > 0 else None


def win32_monitors():
    # [(x, y, w, h)] in physical desktop pixels (fallback for monitors()),
    # primary first; [] when not on Windows or the call fails
    if sys.platform != "win32":
        return []
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try:
        # Per-monitor aware so rects are not DPI-scaled (Qt usually set it)
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    out = []
    proc_t = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _cb(hmon, hdc, rect, lparam):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            primary = bool(info.dwFlags & 1)
            out.append((not primary, r.left, r.top,
                        r.right - r.left, r.bottom - r.top))
        return 1
    try:
        user32.EnumDisplayMonitors(None, None, proc_t(_cb), 0)
    except Exception:
        return []
    out.sort()
    return [m[1:] for m in out]


def qt_monitors():
    # Fallback: Qt screens scaled by device pixel ratio, primary first
    screens = QGuiApplication.screens()
    prim = QGuiApplication.primaryScreen()
    screens = sorted(screens, key=lambda s: s is not prim)
    out = []
    for s in screens:
        g = s.geometry()
        r = s.devicePixelRatio()
        out.append((int(round(g.x() * r)), int(round(g.y() * r)),
                    int(round(g.width() * r)), int(round(g.height() * r))))
    return out


def dxgi_outputs():
    # [(output_idx, w, h)] for the outputs of DXGI adapter 0, in the order
    # ddagrab's output_idx uses; [] when not on Windows or DXGI fails.
    # Plain COM through vtable slots: IDXGIFactory1::EnumAdapters1 = 12,
    # IDXGIAdapter::EnumOutputs = 7, IDXGIOutput::GetDesc = 7, Release = 2.
    if sys.platform != "win32":
        return []
    from ctypes import wintypes
    win32_monitors()           # sets per-monitor DPI awareness first

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16),
                    ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

    class OUTPUT_DESC(ctypes.Structure):
        _fields_ = [("DeviceName", ctypes.c_wchar * 32),
                    ("DesktopCoordinates", wintypes.RECT),
                    ("AttachedToDesktop", wintypes.BOOL),
                    ("Rotation", ctypes.c_int), ("Monitor", wintypes.HMONITOR)]

    def call(obj, slot, *args):
        # args: ctypes.c_uint values or byref() pointers (passed as void *)
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        types = [ctypes.c_uint if isinstance(a, ctypes.c_uint) else ctypes.c_void_p
                 for a in args]
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(vtbl[slot])
        return fn(obj, *args)

    # IID_IDXGIFactory1 {770aae78-f26f-4dba-a829-253c83d1b387}
    iid = GUID(0x770aae78, 0xf26f, 0x4dba,
               (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
    out = []
    factory = ctypes.c_void_p()
    adapter = ctypes.c_void_p()
    try:
        dxgi = ctypes.windll.dxgi
        if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return []
        if call(factory, 12, ctypes.c_uint(0), ctypes.byref(adapter)) != 0:
            return []
        i = 0
        while True:
            output = ctypes.c_void_p()
            if call(adapter, 7, ctypes.c_uint(i), ctypes.byref(output)) != 0:
                break              # DXGI_ERROR_NOT_FOUND: no more outputs
            desc = OUTPUT_DESC()
            if call(output, 7, ctypes.byref(desc)) == 0 and desc.AttachedToDesktop:
                r = desc.DesktopCoordinates
                out.append((i, r.right - r.left, r.bottom - r.top))
            call(output, 2)
            i += 1
    except Exception:
        return []
    finally:
        if adapter:
            call(adapter, 2)
        if factory:
            call(factory, 2)
    return out


def monitors():
    # [(ddagrab output_idx, w, h)]; without DXGI, a guess in Win32 order
    return dxgi_outputs() or [
        (i, w, h) for i, (_x, _y, w, h) in enumerate(win32_monitors() or qt_monitors())]


def capture_cmd(output_idx, fps, out_path):
    # ddagrab of one whole output (native size) -> h264_nvenc (constant
    # quality 23, no bitrate cap). The D3D11 frames go straight into NVENC,
    # so there is no CPU-side scale or pixel format filter.
    src = "ddagrab=output_idx=%d:framerate=%g:draw_mouse=1" % (output_idx, fps)
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
            "-f", "lavfi", "-i", src, "-an",
            "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
            "-rc", "vbr", "-cq", "23", "-b:v", "0",
            "-y", out_path]


class CaptureWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FLAVE Capture")
        self._procs = []
        s = load_settings()
        self.folder_edit = QLineEdit(str(s.get("folder", "")))
        self.fps_edit = QLineEdit(str(s.get("fps", DEFAULT_FPS)))
        self.fps_edit.setValidator(QDoubleValidator(0.0, 1000.0, 3, self))
        self.button = QPushButton("Start")
        form = QFormLayout()
        form.addRow("Output folder:", self.folder_edit)
        form.addRow("FPS:", self.fps_edit)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.button)
        self.folder_edit.textChanged.connect(self._update_button)
        self.fps_edit.textChanged.connect(self._update_button)
        self.button.clicked.connect(self._toggle)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self.resize(420, 0)
        self._update_button()

    def _inputs_ok(self):
        return (os.path.isdir(self.folder_edit.text().strip())
                and parse_fps(self.fps_edit.text()) is not None)

    def _update_button(self):
        recording = bool(self._procs)
        self.button.setText("Stop" if recording else "Start")
        self.button.setEnabled(recording or self._inputs_ok())
        self.folder_edit.setEnabled(not recording)
        self.fps_edit.setEnabled(not recording)

    def _toggle(self):
        if self._procs:
            self._stop()
        elif self._inputs_ok():
            self._start()
        self._update_button()

    def _start(self):
        folder = self.folder_edit.text().strip()
        fps = parse_fps(self.fps_edit.text())
        save_settings(folder, self.fps_edit.text().strip())
        stamp = time.strftime("%Y%m%d_%H%M%S")
        for idx, w, h in monitors():
            name = "Monitor%d_%dx%d_%s.mp4" % (idx + 1, w, h, stamp)
            try:
                p = subprocess.Popen(
                    capture_cmd(idx, fps, os.path.join(folder, name)),
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=BELOW_NORMAL | NO_WINDOW)
            except OSError as e:
                self.setWindowTitle("FLAVE Capture - FFmpeg failed: %s" % e)
                self._stop()
                return
            self._procs.append(p)
        if self._procs:
            self.setWindowTitle("FLAVE Capture - recording %d monitor(s)"
                                % len(self._procs))
            self._timer.start()

    def _stop(self):
        self._timer.stop()
        for p in self._procs:
            try:
                p.stdin.write(b"q")
                p.stdin.flush()
                p.stdin.close()
            except Exception:
                pass
        for p in self._procs:
            try:
                p.wait(STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                p.kill()
        self._procs = []
        if "failed" not in self.windowTitle():
            self.setWindowTitle("FLAVE Capture")

    def _poll(self):
        # A capture that died (bad FPS, monitor change, disk full) stops all
        if any(p.poll() is not None for p in self._procs):
            self._stop()
            self.setWindowTitle("FLAVE Capture - FFmpeg stopped (failed)")
            self._update_button()

    def closeEvent(self, event):
        if self._procs:
            self._stop()
        save_settings(self.folder_edit.text().strip(),
                      self.fps_edit.text().strip())
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    w = CaptureWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
