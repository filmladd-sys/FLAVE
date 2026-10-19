# sequence_timeline_rubber.py
# BUILD 148: rubber-band selection on the Sequence Timeline track rows.
# Right-press in empty clip-area space (no clip body / edge, key box or
# marker under the mouse) arms it. Moving past the drag threshold draws a
# white-bordered box (PlayheadOverlay paints tl._rubber_rect) that follows
# the mouse; the release selects every clip (video, audio, image, caption,
# keyframe) whose painted bounds intersect the box, replacing the
# selection. A release without that drag opens the same right-click menu as
# before (tl._track_context_menu at the press position).
# Qt sends the mouse context-menu event at the press (X11 / macOS) or right
# after the release (Windows); while armed, and after that release until
# the next mouse press (at most EAT_MENU_SEC), the viewport filter swallows
# it so the menu never opens during a drag and opens once (from here)
# after a plain right-click. ASCII only.

import time

from PySide6.QtCore import Qt, QEvent, QRect, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication

from sequence_timeline_ruler import CLIPS_COL


def _empty_space(tl, pos):
    # True when a right-click here would open the track / empty-space menu
    # (not a clip, clip edge, key box or marker) inside the Clips area
    if tl._x_in_clips(pos) < 0:
        return False
    if tl._hit_key(pos) is not None or tl._hit_marker(pos) is not None:
        return False
    if tl._clip_at(pos) is not None or tl._clip_edge_at(pos) is not None:
        return False
    return True


def _box(tl):
    rb = tl._rubber
    o, p = rb["origin"], rb["pos"]
    return QRect(min(o.x(), p.x()), min(o.y(), p.y()),
                 abs(p.x() - o.x()) + 1, abs(p.y() - o.y()) + 1)


def clips_in_rect(tl, rect):
    # Clips whose painted rect (row cell height, same x / width as the clip
    # delegate) intersects rect, top row first, then by start frame
    table = tl.table
    left = table.columnViewportPosition(CLIPS_COL)
    ppf = tl.pixels_per_frame
    out = []
    for r in range(table.rowCount()):
        if table.isRowHidden(r):
            continue
        item = table.item(r, 0)
        if item is None:
            continue
        top = table.rowViewportPosition(r)
        h = table.rowHeight(r)
        if top > rect.bottom() or top + h - 1 < rect.top():
            continue
        for c in tl.clips_by_track.get(item.data(Qt.UserRole), []):
            x = int(left + (c[2] - tl.view_start_frame) * ppf)
            w = max(1, int(c[3] * ppf))
            if QRect(x, top, w, h).intersects(rect):
                out.append((r, int(c[2]), c))
    out.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in out]


def select_rect(tl, rect):
    # Replace the selection with the clips in rect (no link partners added;
    # an empty box clears the selection, like a click on empty space)
    clips = clips_in_rect(tl, rect)
    tl._selection_order = [c[0] for c in clips]
    tl._selected_clip_ids = set(tl._selection_order)
    tl._selection_solo = False
    tl.table.viewport().update()
    tl._update_edit_actions()
    return clips


EAT_MENU_SEC = 1.0


def _eating(tl):
    until = getattr(tl, "_rubber_eat_menu", False)
    if not until:
        return False
    if time.monotonic() > float(until):
        tl._rubber_eat_menu = False
        return False
    return True


def rubber_event(tl, obj, event, was_slice=False):
    # Viewport events. True = consumed, None = not ours (normal handling)
    etype = event.type()
    rb = getattr(tl, "_rubber", None)
    if etype == QEvent.ContextMenu:
        if ((rb is not None or _eating(tl))
                and event.reason() == QContextMenuEvent.Mouse):
            tl._rubber_eat_menu = False     # one menu event per gesture
            return True
        return None
    if etype in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
        tl._rubber_eat_menu = False         # a new click: nothing to eat
    if etype == QEvent.MouseButtonPress and event.button() == Qt.RightButton:
        pos = event.position().toPoint()
        tl._rubber = None
        tl._rubber_rect = None
        # Slice mode's right-press (exit + menu) and clip / key / marker
        # menus stay as they were
        if was_slice or not _empty_space(tl, pos):
            return None
        tl._rubber = {"origin": pos, "pos": pos, "active": False}
        return None     # the item view still sees the press, as before
    if rb is None:
        return None
    if etype == QEvent.MouseMove:
        if not (event.buttons() & Qt.RightButton):
            return None
        rb["pos"] = event.position().toPoint()
        if not rb["active"]:
            d = rb["pos"] - rb["origin"]
            if abs(d.x()) + abs(d.y()) < max(1, QApplication.startDragDistance()):
                return True
            rb["active"] = True
        tl._rubber_rect = _box(tl)
        tl.playhead_overlay.update()
        return True
    if etype == QEvent.MouseButtonRelease and event.button() == Qt.RightButton:
        rb["pos"] = event.position().toPoint()
        tl._rubber = None
        tl._rubber_rect = None
        tl._rubber_eat_menu = time.monotonic() + EAT_MENU_SEC
        if rb["active"]:
            o, p = rb["origin"], rb["pos"]
            rect = QRect(min(o.x(), p.x()), min(o.y(), p.y()),
                         abs(p.x() - o.x()) + 1, abs(p.y() - o.y()) + 1)
            clips = select_rect(tl, rect)
            tl.playhead_overlay.update()
            try:
                tl.status_bar.showMessage("Selected %d clip%s" % (
                    len(clips), "" if len(clips) == 1 else "s"), 2000)
            except Exception:
                pass
        else:
            origin = rb["origin"]
            QTimer.singleShot(0, lambda: tl._track_context_menu(origin))
        return None
    return None


def paint_rubber(tl, painter):
    # White 1 px border, no fill (PlayheadOverlay, on top of the clips)
    rect = getattr(tl, "_rubber_rect", None)
    if rect is None:
        return
    from PySide6.QtGui import QPen, QColor
    painter.setPen(QPen(QColor(255, 255, 255), 1))
    painter.setBrush(Qt.NoBrush)
    painter.drawRect(rect.adjusted(0, 0, -1, -1))
