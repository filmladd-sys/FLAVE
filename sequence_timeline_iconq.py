# sequence_timeline_iconq.py
# Sequence Timeline icon generation queue. Paint never grabs frames: a
# visible video / image clip whose in or out icon is not stored asks for it
# here (request_icon), and a GUI-thread QTimer makes a few per tick with
# the usual ensure_footage_icon path (stored full frame scaled, else a
# grab through the cached per-file decoder), stores it, caches it and
# repaints only that clip. Requests for clips no longer on screen are
# dropped when their turn comes (paint asks again when they show). A key
# that cannot be made is remembered as failed (paint then draws the black
# missing-frame slot) until the queue is reset by a timeline refresh or an
# icon cache clear (SequenceTimeline._clear_icon_misses). The decoder and
# the project DB connections are used from the GUI thread only, so no
# worker thread is used. ASCII only.

import time
from PySide6.QtCore import Qt, QRect, QTimer

TICK_MS = 15          # gap between ticks, so input and paints run between
TICK_BUDGET_S = 0.030 # stop starting new icons in a tick after this long


class IconQueue:
    def __init__(self, tl):
        self.tl = tl
        self.pending = {}     # (footage_id, frame, height) -> (clip, track_id)
        self.failed = set()
        self._timer = QTimer(tl.table)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    def request(self, key, clip, track_id):
        if key in self.failed or key in self.pending:
            return
        if key in self.tl._icon_cache:
            return
        self.pending[key] = (clip, track_id)
        if not self._timer.isActive():
            self._timer.start()

    def reset(self):
        self.pending.clear()
        self.failed.clear()
        self._timer.stop()

    def _clip_view_rect(self, clip, track_id):
        # Viewport rect of clip in its track row if any part is visible,
        # else None
        tl = self.tl
        table = tl.table
        from sequence_timeline_ruler import CLIPS_COL
        if clip is None or tl.pixels_per_frame <= 0:
            return None
        row = None
        for r in range(table.rowCount()):
            item = table.item(r, 0)
            if item is not None and item.data(Qt.UserRole) == track_id:
                row = r
                break
        if row is None or table.isRowHidden(row):
            return None
        left = table.columnViewportPosition(CLIPS_COL)
        x = int(left + (clip[2] - tl.view_start_frame) * tl.pixels_per_frame)
        w = max(1, int(clip[3] * tl.pixels_per_frame))
        r = QRect(x, table.rowViewportPosition(row), w, table.rowHeight(row))
        vp = table.viewport().rect()
        vp.setLeft(max(vp.left(), left))
        vp.setRight(min(vp.right(), left + table.columnWidth(CLIPS_COL) - 1))
        if not r.intersects(vp):
            return None
        return r.intersected(vp)

    def _tick(self):
        tl = self.tl
        t0 = time.perf_counter()
        while self.pending and time.perf_counter() - t0 < TICK_BUDGET_S:
            key = next(iter(self.pending))
            clip, track_id = self.pending.pop(key)
            if key in tl._icon_cache:
                continue
            area = self._clip_view_rect(clip, track_id)
            if area is None:
                continue          # off screen now; paint asks again later
            footage_id, frame, height = key
            pix = None
            try:
                from footage_manager_display import ensure_footage_icon
                pix = ensure_footage_icon(tl._project_db(), footage_id,
                                          frame_number=frame, target_height=height)
            except Exception as e:
                print("[icons] make failed footage %s frame %s: %s" % (footage_id, frame, e))
            if pix is None or pix.isNull():
                self.failed.add(key)
                tl._icon_miss[key] = True
            else:
                tl._icon_cache[key] = pix
                tl._icon_miss.pop(key, None)
            tl.table.viewport().update(area)
        if not self.pending:
            self._timer.stop()


def icon_queue(tl):
    # The timeline's queue, made on first use
    q = getattr(tl, "_icon_queue", None)
    if q is None:
        q = IconQueue(tl)
        tl._icon_queue = q
    return q


def request_icon(tl, footage_id, frame, height, clip, track_id):
    # Paint: ask for the icon of footage_id at source frame and height
    if footage_id is None or height <= 0:
        return
    icon_queue(tl).request((footage_id, max(0, int(frame or 0)), height), clip, track_id)


def icon_failed(tl, footage_id, frame, height):
    # True when that icon could not be made (black missing-frame slot)
    q = getattr(tl, "_icon_queue", None)
    return q is not None and (footage_id, max(0, int(frame or 0)), height) in q.failed
