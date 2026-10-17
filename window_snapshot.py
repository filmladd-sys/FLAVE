# window_snapshot.py
# BUILD 114: a camera button over every top-level window's title bar.
# Windows title bars are native non-client area, so a child widget cannot
# draw there. Each target gets a small frameless Qt.Tool helper window,
# owned by the target (transient parent), placed over the title bar just
# left of the min / max / close buttons. It follows the target through
# the target QWindow's geometry / visibility / state signals.
# Installed once with install_window_snapshots(app); an application event
# filter attaches a helper on the first Show of each normal top-level
# window (QMainWindow / plain QWidget windows and QWindow subclasses such
# as the OpenGL Sequence Player). Dialogs, popups, menus, tooltips, tool
# windows and the helpers themselves are skipped.
# Click: hide the helper, grab the whole frame from the screen, ask for a
# PNG path, save. Cancel does nothing.
# ASCII only.

import os
import re
import sys

from PySide6.QtCore import QObject, QEvent, QRect, QRectF, QPointF, Qt, QTimer
from PySide6.QtGui import (
    QColor, QGuiApplication, QPainter, QPixmap, QWindow,
)
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QWidget


CAPTION_BUTTON_W = 46       # one Windows 10/11 caption button (logical px)
CAPTION_BUTTONS = 3         # minimize, maximize, close
CAPTION_GAP = 4             # space between the camera and minimize
BUTTON_W = 30
BUTTON_H = 22
MIN_TITLE_H = 16            # below this: no usable title band (no frame info)
CLIENT_INSET = 4            # fallback: inside the client area, top-right
PROP_ID = "_ladd_snapshot_id"
PROP_SKIP = "_ladd_snapshot_skip"

_filter = None              # the one application event filter
_helpers = {}               # id -> SnapshotButton
_next_id = [1]
_last_dir = [None]


# ---- pure helpers (testable offscreen)

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = set(["CON", "PRN", "AUX", "NUL"] +
                ["COM%d" % i for i in range(1, 10)] +
                ["LPT%d" % i for i in range(1, 10)])


def sanitize_filename(title, default="window", max_len=120):
    # Window title -> safe Windows file name stem (no extension)
    s = _BAD_CHARS.sub("_", title or "")
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    s = re.sub(r"_+", "_", s).strip(" _")
    if not s:
        s = default
    if s.split(".")[0].upper() in _RESERVED:
        s = s + "_"
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    return s


def default_png_name(title):
    return sanitize_filename(title) + ".png"


def title_button_rect(frame, client, w=BUTTON_W, h=BUTTON_H, avail_top=None):
    # frame / client: QRect in global logical px (frameGeometry, geometry).
    # Returns (QRect, in_title_bar). The title band is frame.top() ..
    # client.top(); the button sits right-aligned against the caption
    # buttons (CAPTION_BUTTONS x CAPTION_BUTTON_W + CAPTION_GAP from the
    # client right edge), vertically centred in the band. With no usable
    # band (no frame info / frameless) it goes inside the client area at
    # the top-right instead.
    top = frame.top()
    if avail_top is not None and top < avail_top:
        top = avail_top                    # maximized: off-screen border
    band = client.top() - top
    right = client.x() + client.width()
    if band >= MIN_TITLE_H:
        h = min(h, band - 4)
        x = right - CAPTION_BUTTONS * CAPTION_BUTTON_W - CAPTION_GAP - w
        y = top + (band - h) // 2
        return QRect(x, y, w, h), True
    x = right - CLIENT_INSET - w
    y = client.top() + CLIENT_INSET
    return QRect(x, y, w, h), False


def draw_camera(painter, rect, color):
    # Simple camera: rounded body, lens circle, small bump on top. The lens
    # is punched out (CompositionMode_Clear) with a dot in the middle, so
    # draw it onto a transparent pixmap (camera_pixmap) first.
    r = QRectF(rect)
    s = min(r.width(), r.height() * 1.3)
    bw = s * 0.80
    bh = bw * 0.62
    cx = r.center().x()
    cy = r.center().y() + bh * 0.08
    body = QRectF(cx - bw / 2, cy - bh / 2, bw, bh)
    bump = QRectF(cx - bw * 0.18, body.top() - bh * 0.22, bw * 0.36, bh * 0.26)
    lens_r = bh * 0.30
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(body, bh * 0.14, bh * 0.14)
    painter.drawRoundedRect(bump, bh * 0.06, bh * 0.06)
    painter.setCompositionMode(QPainter.CompositionMode_Clear)
    painter.setBrush(QColor(0, 0, 0))
    painter.drawEllipse(QPointF(cx, cy), lens_r + bh * 0.07, lens_r + bh * 0.07)
    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(cx, cy), lens_r * 0.62, lens_r * 0.62)
    painter.restore()


def camera_pixmap(size=22, color=None, dpr=1.0, width=None):
    # Transparent pixmap with the camera
    w = width or size
    pm = QPixmap(max(1, int(round(w * dpr))), max(1, int(round(size * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    draw_camera(p, QRect(0, 0, w, size), color or QColor(150, 150, 150))
    p.end()
    return pm


def is_target_widget(w):
    # Normal top-level QWidget windows only
    if not isinstance(w, QWidget) or not w.isWindow():
        return False
    if isinstance(w, (QDialog, SnapshotButton)):
        return False
    if w.property(PROP_SKIP):
        return False
    return w.windowType() == Qt.Window


def is_target_window(win):
    # Bare QWindow subclasses (e.g. the GL Sequence Player). The QWindow
    # behind a QWidget window (QWidgetWindow) is handled via the widget.
    if not isinstance(win, QWindow):
        return False
    if win.metaObject().className() == "QWidgetWindow":
        return False
    if win.property(PROP_SKIP) or win.transientParent() is not None:
        return False
    if win.parent() is not None:
        return False
    return win.type() == Qt.Window


# ---- capture

def _dwm_insets(hwnd):
    # Windows: invisible resize borders (physical px) between GetWindowRect
    # and the DWM visible frame, as (left, top, right, bottom); else None
    if sys.platform != "win32" or not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        wr = wintypes.RECT()
        fr = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(wr)):
            return None
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), 9, ctypes.byref(fr), ctypes.sizeof(fr)) != 0:
            return None                    # 9 = DWMWA_EXTENDED_FRAME_BOUNDS
        ins = (fr.left - wr.left, fr.top - wr.top,
               wr.right - fr.right, wr.bottom - fr.bottom)
        if min(ins) < 0 or max(ins) > 64:
            return None
        return ins
    except Exception:
        return None


def visible_frame_rect(win):
    # Whole window including the native frame, global logical px, minus
    # the invisible Windows 10/11 resize borders when DWM reports them
    fg = win.frameGeometry()
    ins = _dwm_insets(int(win.winId()))
    if ins:
        dpr = win.devicePixelRatio() or 1.0
        l, t, r, b = [int(round(v / dpr)) for v in ins]
        fg = fg.adjusted(l, t, -r, -b)
    return fg


def grab_window(win, widget=None):
    # Screen grab of the frame rect (includes title bar; works for the GL
    # player since it reads the composited screen). Fallbacks: the
    # window's own client area (widget.grab() / grabWindow(winId)).
    rect = visible_frame_rect(win)
    screen = QGuiApplication.screenAt(rect.center()) or win.screen()
    pm = QPixmap()
    if screen is not None and rect.width() > 0 and rect.height() > 0:
        sg = screen.geometry()
        try:
            pm = screen.grabWindow(0, rect.x() - sg.x(), rect.y() - sg.y(),
                                   rect.width(), rect.height())
        except Exception:
            pm = QPixmap()
    if pm.isNull() or pm.width() <= 1:
        if widget is not None:
            pm = widget.grab()
        elif screen is not None:
            try:
                pm = screen.grabWindow(int(win.winId()))
            except Exception:
                pm = QPixmap()
    return pm


def save_snapshot(win, widget=None):
    # Grab, ask for a path (default '<title>.png'), save. Returns the path
    # or None (cancel / failure).
    pm = grab_window(win, widget)
    if pm.isNull():
        # print("SNAPSHOT: grab failed")
        return None
    start = _last_dir[0] or os.path.expanduser("~")
    default = os.path.join(start, default_png_name(win.title()))
    path, _sel = QFileDialog.getSaveFileName(
        widget, "Save Window Screenshot", default, "PNG Images (*.png)")
    if not path:
        return None
    if not path.lower().endswith(".png"):
        path += ".png"
    _last_dir[0] = os.path.dirname(path)
    if not pm.save(path, "PNG"):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(widget, "Screenshot", "Could not save:\n%s" % path)
        return None
    return path


# ---- the helper button window

class SnapshotButton(QWidget):
    # Frameless Qt.Tool window owned by the target; never takes focus.

    def __init__(self, target_win, target_widget=None):
        flags = (Qt.Tool | Qt.FramelessWindowHint |
                 Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        super().__init__(target_widget, flags)
        self.setProperty(PROP_SKIP, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip("Save a screenshot of this window")
        self.setCursor(Qt.ArrowCursor)
        self.target = target_win
        self.target_widget = target_widget
        self.in_title = False
        self._hover = False
        self._down = False
        self._busy = False
        self.resize(BUTTON_W, BUTTON_H)
        self.winId()                               # native handle now
        h = self.windowHandle()
        if h is not None and target_widget is None:
            h.setTransientParent(target_win)       # owned by the QWindow
        t = target_win
        for sig in (t.xChanged, t.yChanged, t.widthChanged, t.heightChanged):
            sig.connect(self._queue_place)
        t.visibleChanged.connect(self._queue_place)
        t.windowStateChanged.connect(self._queue_place)
        self._place_timer = QTimer(self)
        self._place_timer.setSingleShot(True)
        self._place_timer.setInterval(0)
        self._place_timer.timeout.connect(self.place)

    # placement

    def _queue_place(self, *args):
        if not self._place_timer.isActive():
            self._place_timer.start()

    def target_usable(self):
        t = self.target
        try:
            if not t.isVisible():
                return False
            st = t.windowStates()
        except RuntimeError:
            return False                           # C++ object gone
        if st & Qt.WindowMinimized or st & Qt.WindowFullScreen:
            return False
        if self.target_widget is not None and self.target_widget.isMinimized():
            return False
        return True

    def place(self):
        if self._busy:
            return
        if not self.target_usable():
            self.hide()
            return
        t = self.target
        avail = None
        try:
            if t.windowStates() & Qt.WindowMaximized and t.screen() is not None:
                avail = t.screen().availableGeometry().top()
        except Exception:
            avail = None
        r, self.in_title = title_button_rect(t.frameGeometry(), t.geometry(),
                                             avail_top=avail)
        if self.geometry() != r:
            self.setGeometry(r)
        if not self.isVisible():
            self.show()
            self.raise_()

    # painting / mouse

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect()
        # alpha >= 1 everywhere so clicks are not passed through
        bg = QColor(128, 128, 128, 90 if self._down else (55 if self._hover else 1))
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(r), 4, 4)
        ir = r.adjusted(4, 2, -4, -2)
        pm = camera_pixmap(ir.height(), QColor(150, 150, 150),
                           self.devicePixelRatioF(), ir.width())
        p.drawPixmap(ir.topLeft(), pm)
        p.end()

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        self._down = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._down = True
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._down:
            self._down = False
            self.update()
            if self.rect().contains(event.position().toPoint()):
                self.snap()

    def snap(self):
        # Hide first so the camera is not in the shot; grab after the
        # compositor has redrawn the title bar
        if self._busy:
            return
        self._busy = True
        self._hover = False
        self.hide()
        QTimer.singleShot(150, self._do_snap)

    def _do_snap(self):
        try:
            save_snapshot(self.target, self.target_widget)
        except Exception as e:
            # print("SNAPSHOT failed: %s" % e)
            pass
        self._busy = False
        self.place()


# ---- attach / filter

def attach(target_win, target_widget=None):
    # Idempotent: one helper per target window
    if target_win is None:
        return None
    holder = target_widget if target_widget is not None else target_win
    hid = holder.property(PROP_ID)
    if hid and hid in _helpers:
        _helpers[hid]._queue_place()
        return _helpers[hid]
    hid = _next_id[0]
    _next_id[0] += 1
    holder.setProperty(PROP_ID, hid)
    btn = SnapshotButton(target_win, target_widget)
    _helpers[hid] = btn

    def _gone(*args, _hid=hid):
        b = _helpers.pop(_hid, None)
        if b is not None and target_widget is None:
            try:
                b.deleteLater()                    # unparented helper
            except RuntimeError:
                pass
    holder.destroyed.connect(_gone)
    btn._queue_place()
    return btn


class _SnapshotFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() != QEvent.Show:
            return False
        try:
            if isinstance(obj, QWidget):
                if is_target_widget(obj):
                    attach(obj.windowHandle(), obj)
            elif is_target_window(obj):
                attach(obj, None)
        except Exception as e:
            # print("SNAPSHOT attach failed: %s" % e)
            pass
        return False


def install_window_snapshots(app=None):
    # Call once, right after QApplication is created
    global _filter
    app = app or QApplication.instance()
    if app is None or _filter is not None:
        return _filter
    _filter = _SnapshotFilter(app)
    app.installEventFilter(_filter)
    return _filter
