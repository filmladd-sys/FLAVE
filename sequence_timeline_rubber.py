# sequence_timeline_rubber.py
# BUILD 148: rubber-band selection on the Sequence Timeline track rows.
# BUILD 149: on the LEFT button. A left-press in empty clip-area space (no
# clip body / edge, key box or marker under the mouse; never in slice
# mode) arms it. Moving past the drag threshold draws a white-bordered box
# (PlayheadOverlay paints tl._rubber_rect) that follows the mouse; the
# release selects every clip (video, audio, image, caption, keyframe)
# whose painted bounds intersect the box, replacing the selection. A plain
# left-click (no drag past the threshold) is unchanged. The BUILD 148
# right-button arm, its menu-on-release and the context-menu swallowing
# were removed: right-click opens the menu through Qt as before BUILD 148,
# and a right-drag draws nothing. ASCII only.

from PySide6.QtCore import Qt, QEvent, QRect
from PySide6.QtWidgets import QApplication

from sequence_timeline_ruler import CLIPS_COL


def _empty_space(tl, pos):
    # Empty track space inside the Clips area: not a clip, clip edge, key
    # box or marker (BUILD 149: those keep their own left-press drags)
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


def rubber_event(tl, obj, event, was_slice=False):
    # BUILD 149: left button. Viewport events. True = consumed, None = not
    # ours (the normal handling runs). The press is never consumed, so a
    # plain left-click on empty space does what it did before (clear the
    # selection, the item view's press); only a drag past the threshold
    # becomes a box. Right-click is not handled here at all (the track /
    # empty-space menu opens through Qt as before BUILD 148).
    etype = event.type()
    rb = getattr(tl, "_rubber", None)
    if etype == QEvent.MouseButtonPress:
        tl._rubber = None
        if tl._rubber_rect is not None:
            tl._rubber_rect = None
            tl.playhead_overlay.update()
        if event.button() != Qt.LeftButton:
            return None
        pos = event.position().toPoint()
        # Slice mode, clips (move / trim drag), key boxes and markers keep
        # their own left-press handling
        if was_slice or getattr(tl, "_slice_mode", False) or not _empty_space(tl, pos):
            return None
        tl._rubber = {"origin": pos, "pos": pos, "active": False}
        return None
    if rb is None:
        return None
    if etype == QEvent.MouseMove:
        if not (event.buttons() & Qt.LeftButton):
            tl._rubber = None
            return None
        rb["pos"] = event.position().toPoint()
        if not rb["active"]:
            d = rb["pos"] - rb["origin"]
            if abs(d.x()) + abs(d.y()) < max(1, QApplication.startDragDistance()):
                return None     # still a plain click (hover handling runs)
            rb["active"] = True
        tl._rubber_rect = _box(tl)
        tl.playhead_overlay.update()
        return True
    if etype == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
        rb["pos"] = event.position().toPoint()
        tl._rubber = None
        tl._rubber_rect = None
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
