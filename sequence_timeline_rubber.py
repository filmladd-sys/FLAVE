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
# and a right-drag draws nothing.
# BUILD 150: the box also selects the link partners of the clips it
# touches; the selection (yellow borders) follows the box live while
# dragging; dragging into / past the left or right edge scrolls the view
# (scrub edge rule, stops at frame 0 / the sequence end) with the box
# anchored in sequence frames.
# BUILD 336: Shift+left-drag adds the box (and link partners) onto the
# selection instead of replacing it. The kept ids are snapshotted when the
# box is armed, so each new drag while Shift is still held starts a fresh
# box and adds again. Mouse release does not end that. Releasing Shift
# makes the next drag a plain replace. ASCII only.

from PySide6.QtCore import Qt, QEvent, QRect, QTimer
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


def _clips_span(tl):
    # (left x, right x) of the visible Clips area in viewport coordinates
    left = tl.table.columnViewportPosition(CLIPS_COL)
    w = min(tl.table.columnWidth(CLIPS_COL), tl.table.viewport().width() - left)
    return left, left + max(1, w) - 1


def _box(tl):
    # BUILD 150: the anchor is kept in sequence frames (rb["frame"]), so the
    # box keeps its start while the view scrolls; the moving corner is the
    # mouse, held inside the visible Clips area
    rb = tl._rubber
    left, right = _clips_span(tl)
    ox = int(round(left + (rb["frame"] - tl.view_start_frame) * tl.pixels_per_frame))
    oy = rb["origin"].y()
    px = max(left, min(right, rb["pos"].x()))
    py = rb["pos"].y()
    return QRect(min(ox, px), min(oy, py), abs(px - ox) + 1, abs(py - oy) + 1)


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


def clips_with_partners(tl, clips):
    # BUILD 150: the box's clips plus their link-group partners (the same
    # _linked_clips the plain click uses), box order first, no duplicates
    out = []
    seen = set()
    for c in clips:
        for m in [c] + tl._linked_clips(c):
            if m[0] not in seen:
                seen.add(m[0])
                out.append(m)
    return out


def _order_adding(base, hits):
    # BUILD 336: prior selection first, then clips this box hits that are
    # not already in it. base is the snapshot from when the box was armed,
    # so shrinking the box drops only this box's clips.
    order = []
    seen = set()
    for cid in list(base) + list(hits):
        if cid not in seen:
            seen.add(cid)
            order.append(cid)
    return order


def select_rect(tl, rect, live=False, base=None):
    # Replace the selection with the clips in rect and (BUILD 150) their
    # link partners; an empty box clears the selection, like a click on
    # empty space. live=True (during the drag): the selection borders
    # follow the box; the edit actions update on the release.
    # BUILD 336: base is the selection to keep (Shift held when the box
    # was armed). None replaces, the same as a plain drag.
    clips = clips_with_partners(tl, clips_in_rect(tl, rect))
    order = [c[0] for c in clips]
    if base is not None:
        order = _order_adding(base, order)
    if order != tl._selection_order or tl._selection_solo:
        tl._selection_order = order
        tl._selected_clip_ids = set(order)
        tl._selection_solo = False
        tl.table.viewport().update()
    if not live:
        tl._update_edit_actions()
    return clips


# BUILD 150: edge auto-scroll while the box is dragged, with the scrub's
# pan rule (sequence_timeline_ruler.scrub_edge: pans by the mouse travel
# into / past the margin, plus a timer tick while held there; stops at
# frame 0 and at the end of the sequence). RUBBER_EDGE_PX: the margin
# inside the visible Clips edges where panning starts.
RUBBER_EDGE_PX = 8


def _pan(tl, tick=False):
    from sequence_timeline_ruler import scrub_edge
    rb = tl._rubber
    left, right = _clips_span(tl)
    width = right - left + 1
    xc = rb["pos"].x() - left
    last = xc if tick else rb.get("last_x")
    view, _frame, rem = scrub_edge(
        xc, last, width, tl.view_start_frame, tl.pixels_per_frame,
        tl.total_frames(), tl.visible_frames(), rb.get("rem", 0.0), tick,
        margin=RUBBER_EDGE_PX)
    rb["last_x"] = xc
    rb["rem"] = rem
    if view != tl.view_start_frame:
        tl.view_start_frame = view
        rb["panned"] = True
        tl.ruler.viewport().update()
        tl.table.viewport().update()
        try:
            tl._update_status()
        except Exception:
            pass
    m = max(0, min(RUBBER_EDGE_PX, (width - 1) // 3))
    return xc < m or xc > width - m


def _shift_add(mods):
    # Shift marquee adds. Alt+Shift is the single-clip toggle, not this.
    return bool(mods & Qt.ShiftModifier) and not (mods & Qt.AltModifier)


def _add_base(rb):
    # None = replace. A list (maybe empty) = add onto that snapshot.
    if rb is not None and rb.get("add"):
        return list(rb.get("base_order") or [])
    return None


def _update(tl, tick=False):
    # Pan if at an edge, then redraw the box and the live selection
    edge = _pan(tl, tick)
    tl._rubber_rect = _box(tl)
    select_rect(tl, tl._rubber_rect, live=True, base=_add_base(tl._rubber))
    tl.playhead_overlay.update()
    timer = _timer(tl)
    if edge and not timer.isActive():
        timer.start()
    elif not edge and timer.isActive():
        timer.stop()


def _timer(tl):
    t = getattr(tl, "_rubber_timer", None)
    if t is None:
        from sequence_timeline_ruler import SCRUB_TICK_MS
        t = QTimer(tl)
        t.setInterval(SCRUB_TICK_MS)
        t.timeout.connect(lambda: _tick(tl))
        tl._rubber_timer = t
    return t


def _tick(tl):
    rb = getattr(tl, "_rubber", None)
    if rb is None or not rb.get("active"):
        _timer(tl).stop()
        return
    _update(tl, tick=True)


def _finish(tl, rb):
    # Release (or a lost button): stop panning, keep the panned view
    _timer(tl).stop()
    if rb.get("panned"):
        try:
            tl._update_status()
            tl._save_view_state()
        except Exception:
            pass


def rubber_event(tl, obj, event, was_slice=False):
    # BUILD 149: left button. Viewport events. True = consumed, None = not
    # ours (the normal handling runs). The press is not consumed here, so
    # a plain left-click on empty space does what it did before (clear the
    # selection); only a drag past the threshold becomes a box. Right-click
    # is not handled here at all (the track / empty-space menu opens
    # through Qt as before BUILD 148).
    etype = event.type()
    rb = getattr(tl, "_rubber", None)
    if etype == QEvent.MouseButtonPress:
        if rb is not None:
            _finish(tl, rb)
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
        left, _right = _clips_span(tl)
        frame = tl.view_start_frame + (pos.x() - left) / float(tl.pixels_per_frame)
        # BUILD 336: snapshot before the press falls through. A second
        # drag while Shift is still held adds onto this, including clips
        # a previous box in this Shift hold already selected.
        mods = event.modifiers() | QApplication.queryKeyboardModifiers()
        add = _shift_add(mods)
        tl._rubber = {"origin": pos, "pos": pos, "active": False,
                      "frame": frame, "last_x": None, "rem": 0.0,
                      "panned": False, "add": add,
                      "base_order": list(tl._selection_order) if add else None}
        return None
    if rb is None:
        return None
    if etype == QEvent.MouseMove:
        if not (event.buttons() & Qt.LeftButton):
            _finish(tl, rb)
            tl._rubber = None
            if tl._rubber_rect is not None:
                tl._rubber_rect = None
                tl.playhead_overlay.update()
            return None
        rb["pos"] = event.position().toPoint()
        if not rb["active"]:
            d = rb["pos"] - rb["origin"]
            if abs(d.x()) + abs(d.y()) < max(1, QApplication.startDragDistance()):
                return None     # still a plain click (hover handling runs)
            rb["active"] = True
        _update(tl)
        return True
    if etype == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
        rb["pos"] = event.position().toPoint()
        rect = _box(tl) if rb["active"] else None
        # Read the Shift snapshot before _rubber is cleared. The release
        # used to drop it first, so select_rect always replaced and the
        # next drag while Shift was still held could not add.
        base = _add_base(rb)
        _finish(tl, rb)
        tl._rubber = None
        tl._rubber_rect = None
        if rect is not None:
            clips = select_rect(tl, rect, base=base)
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
