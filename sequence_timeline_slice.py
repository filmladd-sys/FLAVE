# sequence_timeline_slice.py
# BUILD 131: slice mode for the Sequence Timeline. C enters, Escape / V /
# right-click exit. A 1 px yellow line shows where the cut lands on the clip
# under the mouse and its link-group partners; left-click cuts through the
# existing slice path (slice_clip, the right-click Slice item). Functions
# take the SequenceTimeline as tl. ASCII only.
# BUILD 132: keyframe clips slice too (slice_clip -> keyframe.
# slice_keyframe_clip_db). Every tl attribute is read with getattr so a
# call before SequenceTimeline.__init__ finishes cannot raise.

from PySide6.QtCore import Qt, QRect, QEvent
from PySide6.QtGui import QColor

SLICE_LINE_COLOR = QColor(255, 255, 0)
SLICE_CURSOR = Qt.CrossCursor


def slice_split_frame(clip, x_in_clips, view_start, ppf):
    # The frame slice_clip cuts at for this clips-column x, or None when
    # slice_clip would do nothing (same math and guards: length < 2, mark
    # outside the clip; inside, never the first frame and the right part
    # keeps at least one frame). BUILD 132: keyframe clips included.
    if clip is None or len(clip) < 4:
        return None
    start, length = clip[2], clip[3]
    if length < 2 or not ppf:
        return None
    mark = view_start + x_in_clips / ppf
    if not (start <= mark < start + length):
        return None
    return max(start + 1, min(start + length - 1, int(mark)))


def slice_members(tl, clip, split):
    # Clips slice_clip would cut at split: the clip and its link partners
    # (only the clip under selection solo), each strictly containing split
    if (getattr(tl, "_selection_solo", False)
            and clip[0] in (getattr(tl, "_selected_clip_ids", None) or ())):
        members = [clip]
    else:
        members = [clip] + tl._linked_clips(clip)
    return [c for c in members if c[2] < split < c[2] + c[3]
            and tl._clip_track_id(c) is not None]


def slice_clip_at(tl, pos):
    # Clip under pos for slicing: the whole clip rect (edges too; trims are
    # off in slice mode), track types with clips incl. keyframe clips
    if getattr(tl, "table", None) is None or getattr(tl, "ruler", None) is None:
        return None
    name_item = tl._clip_image_track_row(pos)
    if name_item is None:
        return None
    x = tl._x_in_clips(pos)
    if x < 0:
        return None
    ppf = getattr(tl, "pixels_per_frame", 0)
    by_track = getattr(tl, "clips_by_track", None) or {}
    for clip in reversed(by_track.get(name_item.data(Qt.UserRole), [])):
        left = (clip[2] - tl.view_start_frame) * ppf
        right = left + clip[3] * ppf
        if left <= x < right:
            return clip
    return None


def _track_row(tl, track_id):
    for r in range(tl.table.rowCount()):
        item = tl.table.item(r, 0)
        if item is not None and item.data(Qt.UserRole) == track_id:
            return r
    return None


def slice_line_rects(tl, members, split):
    # One 1 px rect per member at split, inside that clip's cell rect (its
    # track row, grid line excluded) and the visible Clips column
    from sequence_timeline_ruler import CLIPS_COL
    table = tl.table
    col_left = table.columnViewportPosition(CLIPS_COL)
    col_right = col_left + table.columnWidth(CLIPS_COL) - 1
    x = int(col_left + (split - tl.view_start_frame) * tl.pixels_per_frame)
    if not (col_left <= x <= col_right):
        return []
    grid = 1 if table.showGrid() else 0
    rects = []
    for c in members:
        row = _track_row(tl, tl._clip_track_id(c))
        if row is None or table.isRowHidden(row):
            continue
        y = table.rowViewportPosition(row)
        h = max(1, table.rowHeight(row) - grid)
        rects.append(QRect(x, y, 1, h))
    return rects


def _set_lines(tl, rects):
    old = getattr(tl, "_slice_lines", None) or []
    if old == rects:
        return
    tl._slice_lines = rects
    overlay = getattr(tl, "playhead_overlay", None)
    if overlay is not None:
        for r in old + rects:
            overlay.update(r)


def slice_hover(tl, pos):
    # Recompute the line for the mouse at pos (viewport coordinates)
    if not getattr(tl, "_slice_mode", False):
        return
    if getattr(tl, "sequence_id", None) is None:
        exit_slice_mode(tl)
        return
    clip = slice_clip_at(tl, pos)
    rects = []
    if clip is not None:
        split = slice_split_frame(clip, tl._x_in_clips(pos), tl.view_start_frame,
                                  tl.pixels_per_frame)
        if split is not None:
            rects = slice_line_rects(tl, slice_members(tl, clip, split), split)
    _set_lines(tl, rects)


def slice_click(tl, pos):
    # Left-click: the existing slice path at this x, stay in slice mode and
    # show the line on whatever piece is under the mouse now. True if cut.
    if getattr(tl, "sequence_id", None) is None:
        return False
    clip = slice_clip_at(tl, pos)
    if clip is None:
        return False
    x = tl._x_in_clips(pos)
    if slice_split_frame(clip, x, tl.view_start_frame, tl.pixels_per_frame) is None:
        return False
    tl.slice_clip(clip, x)
    _set_lines(tl, [])
    slice_hover(tl, pos)
    return True


def text_input_focused(tl):
    # True when a text field, spin box or table item editor has focus
    from PySide6.QtWidgets import (QApplication, QLineEdit, QTextEdit,
                                   QPlainTextEdit, QAbstractSpinBox,
                                   QAbstractItemView)
    w = QApplication.focusWidget()
    if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
        return True
    table = getattr(tl, "table", None)
    try:
        if table is not None and table.state() == QAbstractItemView.EditingState:
            return True
    except Exception:
        pass
    return False


def enter_slice_mode(tl):
    # C: only with the timeline focused (window shortcut) and no text input
    if getattr(tl, "_slice_mode", False) or getattr(tl, "sequence_id", None) is None:
        return False
    if getattr(tl, "table", None) is None:
        return False
    # Not in the middle of a clip / key / marker drag (its release would
    # be swallowed by slice mode)
    if (getattr(tl, "_edge_drag", None) is not None
            or getattr(tl, "_key_drag", None) is not None
            or getattr(tl, "_marker_drag", None) is not None):
        return False
    if text_input_focused(tl):
        return False
    tl._slice_mode = True
    tl._slice_lines = []
    tl.table.viewport().setCursor(SLICE_CURSOR)
    return True


def exit_slice_mode(tl):
    if not getattr(tl, "_slice_mode", False):
        return False
    tl._slice_mode = False
    _set_lines(tl, [])
    try:
        tl.table.viewport().unsetCursor()
    except Exception:
        pass
    return True


def slice_key_exit(tl):
    # V (window shortcut) and Escape (the window's keyPressEvent: an item
    # editor or text field consumes its own Escape first, so it still
    # closes the editor). A text input keeps its keys.
    if text_input_focused(tl):
        return False
    return exit_slice_mode(tl)


def slice_event(tl, obj, event):
    # Viewport events in slice mode. True = consumed, False = pass on
    # (the normal handling runs), None = not a slice-mode event
    if not getattr(tl, "_slice_mode", False):
        return None
    try:
        return _slice_event(tl, obj, event)
    except AttributeError as e:
        # BUILD 132: never let a half-built timeline crash the filter
        print(f"Slice mode event skipped: {e}")
        return None


def _slice_event(tl, obj, event):
    etype = event.type()
    if etype == QEvent.MouseButtonPress and event.button() == Qt.RightButton:
        # Exit, then the same event opens the normal right-click menu
        exit_slice_mode(tl)
        return False
    if etype in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick,
                 QEvent.MouseButtonRelease):
        if event.button() == Qt.LeftButton:
            if etype == QEvent.MouseButtonPress:
                slice_click(tl, event.position().toPoint())
            # No selection, clip drag, trim, marker / key drag or edit
            return True
        return None
    if etype == QEvent.MouseMove:
        obj.setCursor(SLICE_CURSOR)
        slice_hover(tl, event.position().toPoint())
        return True
    if etype == QEvent.Leave:
        _set_lines(tl, [])
        return None
    if etype == QEvent.Wheel:
        # The view pans: the old line is stale until the next move
        _set_lines(tl, [])
        return None
    return None


def paint_slice_lines(tl, painter):
    for r in getattr(tl, "_slice_lines", None) or []:
        painter.fillRect(r, SLICE_LINE_COLOR)
