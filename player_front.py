# player_front.py
# The selected sequence's Sequence Player stays in front of the other
# FLAVE windows. One player per sequence (open_player_for in
# sequence_player.py: one SequencePlayer per Sequence Timeline window, and
# one timeline window per sequence). The selected sequence is the one
# whose Sequence Timeline or Sequence Player window was activated last,
# or whose row was selected in the Sequence Manager. Whenever another
# FLAVE window is activated (and when a sequence is selected), that
# sequence's player is raised with QWindow.raise_(), which on Windows is
# SetWindowPos(HWND_TOP, SWP_NOACTIVATE): keyboard focus stays where it
# is, the native window and its GL context are untouched, and other
# applications can still cover FLAVE (no stay-on-top flag). The other
# players keep normal stacking. Stacking among FLAVE windows: the
# selected player, then the Find in Captions window(s), then everything
# else: Find is raised first, then the player (all without activation,
# so typing in Find keeps its focus; clicking Find puts the player back
# above it). Not raised: while a modal dialog or popup menu is up, when
# the activated window is another dialog, or when the player is hidden,
# minimized or fullscreen. After a raise, other visible non-modal
# dialogs (progress windows) are raised again so they stay above the
# player. ASCII only.

_STATE = {"sid": None, "installed": False}

# Dialog class kept directly under the player (sequence_timeline_find)
FIND_DIALOG_CLASS = "CaptionFindDialog"


def _is_find(w):
    return type(w).__name__ == FIND_DIALOG_CLASS


def selected_sequence():
    return _STATE["sid"]


def _player_for(sid):
    # The open SequencePlayer of sequence sid, or None
    if sid is None:
        return None
    from PySide6.QtGui import QGuiApplication
    for w in QGuiApplication.topLevelWindows():
        if (type(w).__name__ == "SequencePlayer"
                and getattr(w, "sequence_id", None) == sid
                and not getattr(w, "closed", False)):
            return w
    return None


def _widget_of(window):
    # Top-level QWidget whose native window is window, or None
    from PySide6.QtWidgets import QApplication
    if window is None:
        return None
    for w in QApplication.topLevelWidgets():
        try:
            if w.windowHandle() is window:
                return w
        except RuntimeError:
            continue
    return None


def select_sequence(sid, raise_now=True):
    # sid becomes the selected sequence; its player (if open) comes forward
    if sid is None:
        return
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return
    _STATE["sid"] = sid
    if raise_now:
        _schedule_raise()


def _schedule_raise():
    # Deferred one event-loop turn so the activated window has finished
    # moving to the top before the player goes above it
    from PySide6.QtCore import QTimer
    QTimer.singleShot(0, raise_selected)


def raise_selected():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QWindow
    from PySide6.QtWidgets import QApplication, QDialog
    app = QApplication.instance()
    if app is None or QGuiApplication.applicationState() != Qt.ApplicationActive:
        return
    if QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None:
        return
    player = _player_for(_STATE["sid"])
    if player is None or not player.isVisible():
        return
    if player.visibility() in (QWindow.FullScreen, QWindow.Minimized, QWindow.Hidden):
        return
    focus = QGuiApplication.focusWindow()
    if focus is player:
        return
    fw = _widget_of(focus)
    if isinstance(fw, QDialog) and not _is_find(fw):
        return
    dialogs = []
    for w in QApplication.topLevelWidgets():
        try:
            if isinstance(w, QDialog) and w.isVisible() and not w.isMinimized():
                dialogs.append(w)
        except RuntimeError:
            continue
    for w in dialogs:
        if _is_find(w):
            try:
                w.raise_()
            except RuntimeError:
                pass
    try:
        player.raise_()
    except RuntimeError:
        return
    for w in dialogs:
        if not _is_find(w):
            try:
                w.raise_()
            except RuntimeError:
                pass


def _on_focus_window(window):
    # Activation of a FLAVE window: a timeline or player selects its
    # sequence; any other window just keeps the selected player in front
    if window is None:
        return
    if type(window).__name__ == "SequencePlayer":
        sid = getattr(window, "sequence_id", None)
        if sid is not None:
            _STATE["sid"] = int(sid)
        return
    w = _widget_of(window)
    if w is not None and type(w).__name__ == "SequenceTimeline":
        sid = getattr(w, "sequence_id", None)
        if sid is not None:
            _STATE["sid"] = int(sid)
    _schedule_raise()


def install():
    # Once per process; safe to call from every timeline / manager
    if _STATE["installed"]:
        return
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    app.focusWindowChanged.connect(_on_focus_window)
    _STATE["installed"] = True
