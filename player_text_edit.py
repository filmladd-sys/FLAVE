# On-player interaction with WYSIWYG text clips in the Sequence
# Player (a QWindow; sequence_player.py forwards its mouse events here).
#
# Target: the one selected text clip (text_style_window.selected_text_clip_id)
# when the player is paused and the clip is a layer of the frame under the
# playhead (SequencePlayer._layer_items on the timeline's preview snapshot,
# so a disabled track / clip or a solo elsewhere gives no target).
#
# Coordinates: the picture is drawn in the player's letterboxed rect scaled
# by the Playback Size percent and centered (same math as
# SequencePlayer._present); picture_rect gives it in window (logical) px and
# window_to_seq / seq_to_window map through it. The layer's keyframes
# (scale, position X / Y plus move, rotate) map a point p of the text image
# to the frame as blend_layer does: C + pd + s * R(p - C), C the sequence
# center (layer_xform / to_display / to_source).
#
# Hover: a dashed outline of the target's text box (text_clip.text_box_rect,
# through the keyframe map, bounding rect) in a frameless, translucent,
# input-transparent tool window over the player, and a move cursor inside
# the box. Press inside the box and drag: the outline follows the mouse;
# release writes the clip's base x / y (one undo step "Move text"), clamped
# so the base center stays in the frame, and schedules the preview refresh
# that marks the clip's frames stale and redraws the shown frame. With move
# / position keyframes the drag changes the base x / y and the keys still
# add on top. The picture itself is not redrawn during the drag.
#
# Double-click inside the box: playback stops and a frameless text editor
# window covers the box (same font family, size scaled by the displayed
# size, color, centered lines, Enter = new line, grows with the text, dark
# translucent background that dims the burned-in text under it). Esc,
# Ctrl+Enter or focus leaving the editor (a click outside) commits: a
# changed, non-blank text is written as one undo step (text_style_window
# apply_text_field) and the style window shows it. The editor accepts every
# shortcut override, so no player / timeline / application shortcut fires
# while it has focus (Ctrl+Z is the editor's own undo).

import math

from PySide6.QtCore import Qt, QPoint, QRect, QEvent, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QFontMetrics, QTextOption
from PySide6.QtWidgets import QWidget, QTextEdit


def picture_rect(win_w, win_h, seq_w, seq_h, pct):
    # (left, top, width, height) of the displayed picture in window px:
    # the sequence fitted into the window (letterbox / pillarbox) at pct
    # percent, centered
    win_w, win_h = max(1, int(win_w)), max(1, int(win_h))
    seq_aspect = float(seq_w) / float(max(1, seq_h))
    win_aspect = float(win_w) / float(win_h)
    if win_aspect > seq_aspect:
        vw, vh = seq_aspect / win_aspect, 1.0
    else:
        vw, vh = 1.0, win_aspect / seq_aspect
    f = max(1, min(100, int(pct))) / 100.0
    w, h = vw * f * win_w, vh * f * win_h
    return ((win_w - w) / 2.0, (win_h - h) / 2.0, w, h)


def window_to_seq(x, y, rect, seq_w, seq_h):
    # Window px -> sequence px (floats; outside the picture maps outside)
    left, top, w, h = rect
    return ((x - left) * seq_w / w, (y - top) * seq_h / h)


def seq_to_window(x, y, rect, seq_w, seq_h):
    # Sequence px -> window px (floats)
    left, top, w, h = rect
    return (left + x * w / float(seq_w), top + y * h / float(seq_h))


def layer_xform(item):
    # (scale factor, pos_dx, pos_dy, cos, sin) of a layer item from
    # SequencePlayer._layer_items; same defaults as _blend
    def num(v, d):
        try:
            return float(v) if v is not None else d
        except (TypeError, ValueError):
            return d
    s = num(item[3], 100.0) / 100.0
    if s <= 0.0:
        s = 1.0
    rot = math.radians(num(item[14] if len(item) > 14 else None, 0.0))
    return (s, num(item[4], 0.0), num(item[5], 0.0), math.cos(rot), math.sin(rot))


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0)


def to_display(x, y, xf, seq_w, seq_h):
    # Text image point -> frame point (blend_layer's forward map)
    s, pdx, pdy, c, sn = xf
    cx, cy = seq_w / 2.0, seq_h / 2.0
    u, v = (x - cx) * s, (y - cy) * s
    return (cx + pdx + c * u - sn * v, cy + pdy + sn * u + c * v)


def to_source(x, y, xf, seq_w, seq_h):
    # Frame point -> text image point (inverse of to_display)
    s, pdx, pdy, c, sn = xf
    cx, cy = seq_w / 2.0, seq_h / 2.0
    dx, dy = x - cx - pdx, y - cy - pdy
    return (cx + (c * dx + sn * dy) / s, cy + (-sn * dx + c * dy) / s)


def delta_to_source(dx, dy, xf):
    # A frame-space move -> the base x / y change that follows it
    s, _pdx, _pdy, c, sn = xf
    return ((c * dx + sn * dy) / s, (-sn * dx + c * dy) / s)


def clamp_center(x, y, seq_w, seq_h):
    # Base box center kept inside the frame
    return (max(0, min(int(seq_w) - 1, int(round(x)))),
            max(0, min(int(seq_h) - 1, int(round(y)))))


def style_box(style, seq_w, seq_h, x=None, y=None):
    # The style's text box (QRect, sequence px, text image space); x / y
    # override the stored center
    from text_clip import text_box_rect
    if x is None:
        x = style.get("x")
        x = int(seq_w) // 2 if x is None else x
    if y is None:
        y = style.get("y")
        y = int(seq_h) // 2 if y is None else y
    return text_box_rect(style.get("text") or "", style.get("font_family"),
                         int(style.get("font_size") or 1), x, y)


def display_box(box, xf, seq_w, seq_h):
    # Bounding rect (floats l, t, r, b; frame px) of a text box after the
    # keyframe map
    pts = [to_display(px, py, xf, seq_w, seq_h) for px, py in (
        (box.left(), box.top()), (box.left() + box.width(), box.top()),
        (box.left(), box.top() + box.height()),
        (box.left() + box.width(), box.top() + box.height()))]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def is_playing(tl):
    return bool(getattr(tl, "_playing", False) or getattr(tl, "_play_filling", False)
                or getattr(tl, "_play_mixing", False))


def _tool_flags():
    return Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint


class _Outline(QWidget):
    # Dashed box outline over the player; never takes the mouse or focus
    PAD = 2

    def __init__(self):
        super().__init__(None, _tool_flags() | Qt.WindowTransparentForInput
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.dragging = False

    def place(self, global_rect):
        self.setGeometry(global_rect.adjusted(-self.PAD, -self.PAD,
                                              self.PAD, self.PAD))

    def paintEvent(self, event):
        p = QPainter(self)
        r = self.rect().adjusted(1, 1, -2, -2)
        p.setPen(QPen(QColor(0, 0, 0, 200), 1, Qt.SolidLine))
        p.drawRect(r)
        pen = QPen(QColor(255, 220, 0) if self.dragging else QColor(255, 255, 255),
                   1, Qt.DashLine)
        p.setPen(pen)
        p.drawRect(r)
        p.end()


class _Editor(QTextEdit):
    # Frameless plain-text editor window; Esc / Ctrl+Enter / focus out
    # call on_commit(editor) (a late call for a closed editor is ignored)
    def __init__(self, on_commit):
        super().__init__(None)
        self.setWindowFlags(_tool_flags())
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.NoWrap)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(0)
        opt = QTextOption(self.document().defaultTextOption())
        opt.setAlignment(Qt.AlignHCenter)
        self.document().setDefaultTextOption(opt)
        self._on_commit = on_commit

    def event(self, event):
        # Every key is the editor's: no shortcut (player, timeline, app
        # Undo / Redo) fires while it has focus
        if event.type() == QEvent.ShortcutOverride:
            event.accept()
            return True
        if event.type() == QEvent.WindowDeactivate:
            QTimer.singleShot(0, lambda: self._on_commit(self))
        return super().event(event)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape or (key in (Qt.Key_Return, Qt.Key_Enter)
                                    and event.modifiers() & Qt.ControlModifier):
            event.accept()
            self._on_commit(self)
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        # The editor's own context menu keeps editing
        if event.reason() != Qt.PopupFocusReason:
            QTimer.singleShot(0, lambda: self._on_commit(self))


class PlayerTextEdit(object):
    # One per SequencePlayer (made on first mouse event)
    DRAG_START_PX = 3       # window px before a press becomes a drag
    EDIT_PAD = 4            # editor padding around the text, window px

    def __init__(self, player):
        self.player = player
        self._outline = None
        self._editor = None
        self._edit_clip = None
        self._edit_old = None
        self._drag = None       # dict while a press on the box is held
        self._cache_key = None
        self._cache_val = None
        self._gen = 0           # bumped by our own writes (target cache)

    # ---- target

    def _snapshot(self):
        tl = self.player.timeline
        snap = getattr(tl, "_preview_snapshot", None)
        if snap is None:
            from sequence_player import timeline_snapshot
            snap = timeline_snapshot(tl)
        return snap

    def target(self):
        # (clip_id, style, xform) of the interactive text clip, or None
        tl = self.player.timeline
        if self.player.closed or is_playing(tl):
            return None
        from text_style_window import selected_text_clip_id
        clip_id = selected_text_clip_id(tl)
        if clip_id is None:
            return None
        frame = int(tl.current_frame)
        snap = self._snapshot()
        key = (clip_id, frame, id(snap), self._gen)
        if key == self._cache_key:
            return self._cache_val
        val = None
        item = None
        try:
            for it in self.player._layer_items(frame, snap):
                if it[0] == "text" and it[6][0] == clip_id:
                    item = it
        except Exception as e:
            print(f"Player text target failed: {e}")
        if item is not None:
            from text_clip import read_text_style
            style = read_text_style(tl._project_db(), clip_id)
            if style is not None and (style.get("text") or "").strip():
                val = (clip_id, style, layer_xform(item))
        self._cache_key, self._cache_val = key, val
        return val

    def _rect(self):
        p = self.player
        return picture_rect(p.width(), p.height(), p.seq_w, p.seq_h,
                            getattr(p, "_playback_pct", 100))

    def hit(self, pos, tgt=None):
        # True when window point pos is inside the target's text box
        tgt = tgt or self.target()
        if tgt is None:
            return False
        p = self.player
        sx, sy = window_to_seq(pos.x(), pos.y(), self._rect(), p.seq_w, p.seq_h)
        ux, uy = to_source(sx, sy, tgt[2], p.seq_w, p.seq_h)
        box = style_box(tgt[1], p.seq_w, p.seq_h)
        return (box.left() <= ux <= box.left() + box.width()
                and box.top() <= uy <= box.top() + box.height())

    def window_box(self, tgt, x=None, y=None):
        # The target's displayed box in window px (QRect)
        p = self.player
        l, t, r, b = display_box(style_box(tgt[1], p.seq_w, p.seq_h, x, y),
                                 tgt[2], p.seq_w, p.seq_h)
        rect = self._rect()
        wl, wt = seq_to_window(l, t, rect, p.seq_w, p.seq_h)
        wr, wb = seq_to_window(r, b, rect, p.seq_w, p.seq_h)
        return QRect(int(math.floor(wl)), int(math.floor(wt)),
                     max(1, int(math.ceil(wr - wl))), max(1, int(math.ceil(wb - wt))))

    def _global(self, rect):
        return QRect(self.player.mapToGlobal(rect.topLeft()), rect.size())

    # ---- outline

    def _show_outline(self, window_rect, dragging=False):
        if self._outline is None:
            self._outline = _Outline()
            self._outline.winId()
            handle = self._outline.windowHandle()
            if handle is not None:
                handle.setTransientParent(self.player)
        self._outline.dragging = dragging
        self._outline.place(self._global(window_rect))
        if not self._outline.isVisible():
            self._outline.show()
        self._outline.update()

    def hide_outline(self):
        if self._outline is not None and self._outline.isVisible():
            self._outline.hide()

    def outline_visible(self):
        return self._outline is not None and self._outline.isVisible()

    # ---- mouse (window px, QPointF / QPoint)

    def hover(self, pos):
        if self._drag is not None or self._editor is not None:
            return
        tgt = self.target()
        if tgt is None:
            self.hide_outline()
            self.player.unsetCursor()
            return
        self._show_outline(self.window_box(tgt))
        if self.hit(pos, tgt):
            self.player.setCursor(Qt.SizeAllCursor)
        else:
            self.player.unsetCursor()

    def press(self, pos):
        # True: the press is on the target's box (a drag may follow)
        if self._editor is not None:
            self.commit_edit()
            return False
        tgt = self.target()
        if tgt is None or not self.hit(pos, tgt):
            return False
        p = self.player
        style = tgt[1]
        x0 = style.get("x")
        y0 = style.get("y")
        self._drag = {"tgt": tgt, "start": (pos.x(), pos.y()), "moved": False,
                      "x0": int(p.seq_w) // 2 if x0 is None else int(x0),
                      "y0": int(p.seq_h) // 2 if y0 is None else int(y0),
                      "xy": None}
        return True

    def _drag_xy(self, pos):
        d = self._drag
        p = self.player
        rect = self._rect()
        dx = (pos.x() - d["start"][0]) * p.seq_w / rect[2]
        dy = (pos.y() - d["start"][1]) * p.seq_h / rect[3]
        bx, by = delta_to_source(dx, dy, d["tgt"][2])
        return clamp_center(d["x0"] + bx, d["y0"] + by, p.seq_w, p.seq_h)

    def move(self, pos):
        # True while a press on the box is held
        d = self._drag
        if d is None:
            self.hover(pos)
            return False
        if not d["moved"]:
            if (abs(pos.x() - d["start"][0]) < self.DRAG_START_PX
                    and abs(pos.y() - d["start"][1]) < self.DRAG_START_PX):
                return True
            d["moved"] = True
        d["xy"] = self._drag_xy(pos)
        self._show_outline(self.window_box(d["tgt"], d["xy"][0], d["xy"][1]), True)
        return True

    def release(self, pos):
        d = self._drag
        if d is None:
            return False
        if d["moved"]:
            x, y = self._drag_xy(pos)
            if (x, y) != (d["x0"], d["y0"]):
                self.write_xy(d["tgt"][0], x, y)
            self._show_outline(self.window_box(d["tgt"], x, y))
        self._drag = None
        return True

    def double_click(self, pos):
        # Stops playback, then opens the editor on the target's box
        tl = self.player.timeline
        if is_playing(tl):
            tl._stop_play()
        self._drag = None
        tgt = self.target()
        if tgt is None or not self.hit(pos, tgt):
            return False
        self.open_editor(tgt)
        return True

    def leave(self):
        if self._drag is None:
            self.hide_outline()

    def geometry_changed(self):
        # Player moved / resized: the outline returns on the next hover
        self.hide_outline()

    def close(self):
        self.commit_edit()
        self._drag = None
        if self._outline is not None:
            self._outline.hide()
            self._outline.deleteLater()
            self._outline = None

    # ---- writes

    def write_xy(self, clip_id, x, y):
        # Base x / y as one undo step, then the preview refresh
        tl = self.player.timeline
        project_db = tl._project_db()
        if not project_db:
            return False
        import sqlite3
        from undo import push_undo
        from text_clip import write_text_field
        from text_style_window import schedule_text_preview
        push_undo(project_db, "Move text")
        try:
            conn = sqlite3.connect(project_db)
            try:
                cur = conn.cursor()
                write_text_field(cur, clip_id, "x", int(x))
                write_text_field(cur, clip_id, "y", int(y))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            tl.status_bar.showMessage(f"Could not move text clip: {e}", 3000)
            return False
        self._gen += 1
        self._sync_window(clip_id)
        schedule_text_preview(tl)
        return True

    def _sync_window(self, clip_id):
        win = getattr(self.player.timeline, "_text_style_window", None)
        if win is not None and getattr(win, "clip_id", None) == clip_id:
            win._undo_field = None      # the next style edit is its own step
            win.set_clip(clip_id)

    # ---- editor

    def editing(self):
        return self._editor is not None

    def open_editor(self, tgt):
        from text_clip import text_font, default_font_family, DEFAULT_COLOR
        self.hide_outline()
        self.player.unsetCursor()
        p = self.player
        clip_id, style = tgt[0], tgt[1]
        rect = self._rect()
        ratio = rect[2] / float(p.seq_w) * tgt[2][0]
        size = max(1, int(round(int(style.get("font_size") or 1) * ratio)))
        font = text_font(style.get("font_family") or default_font_family(), size)
        color = QColor(style.get("color") or DEFAULT_COLOR)
        if not color.isValid():
            color = QColor(DEFAULT_COLOR)
        ed = _Editor(self.commit_edit)
        ed.setFont(font)
        ed.setStyleSheet(
            "QTextEdit { background-color: rgba(0, 0, 0, 200); color: %s;"
            " border: 1px dashed rgb(255, 255, 255); }" % color.name())
        ed.setPlainText(style.get("text") or "")
        ed.selectAll()
        self._editor = ed
        self._edit_clip = clip_id
        self._edit_old = style.get("text") or ""
        wb = self.window_box(tgt)
        self._edit_center = (wb.center().x(), wb.center().y())
        self._edit_min = (wb.width(), wb.height())
        ed.textChanged.connect(self._fit_editor)
        self._fit_editor()
        ed.winId()
        handle = ed.windowHandle()
        if handle is not None:
            handle.setTransientParent(p)
        ed.show()
        ed.raise_()
        ed.activateWindow()
        ed.setFocus(Qt.OtherFocusReason)

    def _fit_editor(self):
        # Size to the text (at least the burned-in box), kept centered on
        # the box center
        ed = self._editor
        if ed is None:
            return
        fm = QFontMetrics(ed.font())
        text = ed.toPlainText()
        r = fm.boundingRect(QRect(0, 0, 100000, 100000),
                            int(Qt.AlignHCenter | Qt.AlignTop), text or " ")
        lines = max(1, text.count("\n") + 1)
        pad = self.EDIT_PAD
        w = max(self._edit_min[0], r.width() + fm.averageCharWidth() * 2) + 2 * pad
        h = max(self._edit_min[1], r.height(), lines * fm.lineSpacing()) + 2 * pad
        cx, cy = self._edit_center
        local = QRect(int(cx - w / 2.0), int(cy - h / 2.0), int(w), int(h))
        ed.setGeometry(self._global(local))

    def commit_edit(self, editor=None):
        # Close the editor; a changed, non-blank text is one undo step.
        # editor: the calling editor; a deferred call from an editor that
        # is already closed does nothing
        ed = self._editor
        if ed is None or (editor is not None and editor is not ed):
            return False
        self._editor = None
        text = ed.toPlainText()
        clip_id, old = self._edit_clip, self._edit_old
        self._edit_clip = self._edit_old = None
        try:
            ed.textChanged.disconnect(self._fit_editor)
        except Exception:
            pass
        ed.hide()
        ed.deleteLater()
        if text == old or not text.strip():
            return False
        from text_style_window import apply_text_field
        ok = apply_text_field(self.player.timeline, clip_id, "text", text, push=True)
        if ok:
            self._gen += 1
            self._sync_window(clip_id)
        return ok
