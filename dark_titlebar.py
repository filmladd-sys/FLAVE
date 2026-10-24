# dark_titlebar.py
# Immersive dark title bar on Windows in dark mode.
# DwmSetWindowAttribute(hwnd, 20 DWMWA_USE_IMMERSIVE_DARK_MODE, BOOL) on
# every top-level window's HWND; if 20 fails (Windows 10 before 20H1) it
# is retried once with 19, the older value of the same attribute. No
# Win11-only attributes. Applied on each window's Show (an application
# event filter installed next to the window_snapshot one) and re-applied
# to all open windows by set_dark_titlebars(), which apply_dark_mode()
# in global_settings.py calls whenever the theme is applied or changed.
# Non-Windows or any failure: silently ignored, never raises.
# ASCII only.

import sys

from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtGui import QGuiApplication, QWindow
from PySide6.QtWidgets import QApplication, QWidget

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19     # Windows 10 1809 .. 1909
SWP_REDRAW_FRAME = 0x0020 | 0x0002 | 0x0001 | 0x0004 | 0x0010
# SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE

SKIP_PROPS = ("_ladd_snapshot_skip",)      # the snapshot helper
TITLE_TYPES = (Qt.Window, Qt.Dialog)       # window types with a title bar

_state = {"dark": None}                    # last applied theme (None: unknown)
_filter = None
_api_cache = []


def _is_windows():
    return sys.platform == "win32"


def _api():
    # (DwmSetWindowAttribute, SetWindowPos) or None. Tests replace this.
    if _api_cache:
        return _api_cache[0]
    api = None
    try:
        if _is_windows():
            import ctypes
            from ctypes import wintypes
            dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
            dwm.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD]
            dwm.restype = ctypes.c_long            # HRESULT
            swp = ctypes.windll.user32.SetWindowPos
            swp.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            wintypes.UINT]
            swp.restype = wintypes.BOOL

            def set_attr(hwnd, attr, on):
                val = wintypes.BOOL(1 if on else 0)
                return int(dwm(wintypes.HWND(hwnd), attr, ctypes.byref(val),
                               ctypes.sizeof(val)))

            def redraw(hwnd):
                return swp(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                           SWP_REDRAW_FRAME)
            api = (set_attr, redraw)
    except Exception:
        api = None
    _api_cache.append(api)
    return api


def set_hwnd_dark(hwnd, dark, redraw=False):
    # True if an attribute call succeeded. Never raises.
    try:
        if not _is_windows() or not hwnd:
            return False
        api = _api()
        if api is None:
            return False
        set_attr, do_redraw = api
        ok = set_attr(int(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE, bool(dark)) == 0
        if not ok:
            ok = set_attr(int(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
                          bool(dark)) == 0
        if ok and redraw:
            do_redraw(int(hwnd))
        return ok
    except Exception:
        return False


# ---- which windows

def _skipped(obj):
    for p in SKIP_PROPS:
        if obj.property(p):
            return True
    return False


def is_title_widget(w):
    # Top-level QWidget windows with a title bar: main windows, plain
    # windows, dialogs and message boxes. Not menus / popups / tooltips /
    # tool windows / off-screen (native file dialog shells) / helpers.
    if not isinstance(w, QWidget) or not w.isWindow():
        return False
    if _skipped(w) or w.testAttribute(Qt.WA_DontShowOnScreen):
        return False
    if w.windowFlags() & Qt.FramelessWindowHint:
        return False
    return w.windowType() in TITLE_TYPES


def is_title_window(win):
    # Bare QWindow subclasses (the GL Sequence Player); the QWindow behind
    # a QWidget window is handled through the widget
    if not isinstance(win, QWindow):
        return False
    if win.metaObject().className() == "QWidgetWindow":
        return False
    if _skipped(win) or win.parent() is not None:
        return False
    if win.flags() & Qt.FramelessWindowHint:
        return False
    return win.type() in TITLE_TYPES


def _hwnd_of(obj, create):
    # Native handle; for QWidgets only when one exists already unless
    # create (a Show is about to create it anyway)
    if isinstance(obj, QWidget):
        if not create and obj.windowHandle() is None:
            return 0
        return int(obj.winId())
    return int(obj.winId())


def apply_to(obj, dark=None, redraw=False):
    try:
        if dark is None:
            dark = _state["dark"]
        if dark is None or not _is_windows():
            return False
        if isinstance(obj, QWidget):
            if not is_title_widget(obj):
                return False
        elif not is_title_window(obj):
            return False
        return set_hwnd_dark(_hwnd_of(obj, True), dark, redraw)
    except Exception:
        return False


def title_targets():
    # Open top-level windows that already have a native handle
    out = []
    try:
        for w in QApplication.topLevelWidgets():
            if is_title_widget(w) and w.windowHandle() is not None:
                out.append(w)
        for win in QGuiApplication.topLevelWindows():
            if is_title_window(win):
                out.append(win)
    except Exception:
        pass
    return out


def set_dark_titlebars(dark):
    # Theme applied / changed: remember it for windows shown later, and
    # re-apply to every open one (visible ones get a frame redraw).
    # Returns the number of windows updated (0 off Windows).
    try:
        _state["dark"] = bool(dark)
        if not _is_windows():
            return 0
        n = 0
        for obj in title_targets():
            if apply_to(obj, bool(dark), redraw=obj.isVisible()):
                n += 1
        return n
    except Exception:
        return 0


class _DarkTitleFilter(QObject):
    def eventFilter(self, obj, event):
        # Show arrives before the native window is mapped, so the caption
        # is right on first paint (no redraw needed)
        try:
            if event.type() == QEvent.Show and _state["dark"] is not None:
                if isinstance(obj, (QWidget, QWindow)):
                    apply_to(obj)
        except Exception:
            pass
        return False


def install_dark_titlebars(app=None):
    # Call once, right after QApplication is created. No-op off Windows.
    global _filter
    try:
        if not _is_windows():
            return None
        app = app or QApplication.instance()
        if app is None or _filter is not None:
            return _filter
        _filter = _DarkTitleFilter(app)
        app.installEventFilter(_filter)
        return _filter
    except Exception:
        return None
