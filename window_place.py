# window_place.py
# BUILD 228: where a window opened from Project Manager first appears.
# place_from_project_manager(win, pm, key, saved_keys) runs after the window
# restored its own remembered geometry and before it is shown. The window
# is put just under Project Manager's menu bar (frame top-left at the menu
# bar's bottom-left, overlapping Project Manager's body, clamped into
# Project Manager's screen) when:
#   - it has no remembered geometry (none of saved_keys is set), or
#   - the monitor setup (monitor_signature) differs from the one stored for
#     this window the last time it opened, or
#   - its remembered geometry does not touch any screen's available area.
# Otherwise the remembered geometry stays. The current signature is then
# stored for this window ("<key>_monitors" in global settings), so after a
# monitor change each window is placed once and its memory resumes.
# ASCII only.

from PySide6.QtCore import QRect, QTimer
from PySide6.QtGui import QGuiApplication

from global_settings import _get_global_setting, _set_global_setting


def _screens():
    return list(QGuiApplication.screens())


def monitor_signature(screens=None):
    # One string for the monitor setup: monitor number (Qt's order, from 1)
    # with its width and height in native pixels, e.g.
    # "Monitor1=1920x1080;Monitor2=1080x1920"
    parts = []
    for n, s in enumerate(_screens() if screens is None else screens, 1):
        g = s.geometry()
        dpr = float(s.devicePixelRatio())
        parts.append("Monitor%d=%dx%d" % (n, int(round(g.width() * dpr)),
                                          int(round(g.height() * dpr))))
    return ";".join(parts)


def on_any_screen(rect, screens=None):
    # True when rect touches some screen's available geometry
    for s in (_screens() if screens is None else screens):
        if s.availableGeometry().intersects(rect):
            return True
    return False


def _margins(w):
    # (left, top, right, bottom) frame margins of a shown window, else 0s
    f, g = w.frameGeometry(), w.geometry()
    return (max(0, g.left() - f.left()), max(0, g.top() - f.top()),
            max(0, f.right() - g.right()), max(0, f.bottom() - g.bottom()))


def under_menu_bar_rect(pm, size, margins=None):
    # Client QRect for a window of size (QSize, client area) whose frame
    # top-left sits at Project Manager's menu bar bottom-left, kept inside
    # the available geometry of Project Manager's screen (shrunk if larger).
    # margins: the new window's frame margins; default = Project Manager's
    # own (same window decorations).
    ml, mt, mr, mb = margins if margins is not None else _margins(pm)
    pml, pmt, _r, _b = _margins(pm)
    mbar = pm.menuBar()
    bar_h = mbar.height() if pm.isVisible() else mbar.sizeHint().height()
    x = pm.geometry().left() - pml
    y = pm.geometry().top() + bar_h
    scr = pm.screen() if pm.screen() is not None else QGuiApplication.primaryScreen()
    avail = scr.availableGeometry()
    fw = min(size.width() + ml + mr, avail.width())
    fh = min(size.height() + mt + mb, avail.height())
    x = max(avail.left(), min(x, avail.left() + avail.width() - fw))
    y = max(avail.top(), min(y, avail.top() + avail.height() - fh))
    return QRect(x + ml, y + mt, fw - ml - mr, fh - mt - mb)


def place_from_project_manager(win, pm, key, saved_keys=()):
    # See the header. Returns True when the window was placed.
    sig = monitor_signature()
    sig_key = "%s_monitors" % key
    has_saved = any(_get_global_setting(k, "") for k in saved_keys)
    place = (not has_saved
             or _get_global_setting(sig_key, "") != sig
             or not on_any_screen(win.geometry()))
    _set_global_setting(sig_key, sig)
    if not place:
        return False
    win.setGeometry(under_menu_bar_rect(pm, win.size()))
    if not pm.isVisible():
        # Startup reopen: Project Manager has no frame / menu bar size yet;
        # place again once the event loop runs (Project Manager shown)
        def again():
            try:
                if pm.isVisible() and win.isVisible():
                    win.setGeometry(under_menu_bar_rect(pm, win.size(), _margins(win)))
            except RuntimeError:
                pass
        QTimer.singleShot(0, again)
    return True
