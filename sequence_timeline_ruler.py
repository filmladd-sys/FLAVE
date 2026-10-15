# sequence_timeline_ruler.py
# Frame ruler and scrubber header for the Sequence Timeline, plus zoom and
# ruler label helpers. No timeline window here. ASCII only.

import math
from PySide6.QtWidgets import QHeaderView, QWidget
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen, QPainter


def _tic_setting(key, default):
    # Playhead tick height from global settings; default if missing or bad
    try:
        from sequence_timeline_sql import _get_global_setting
        value = int(str(_get_global_setting(key, default)).strip())
    except Exception:
        return default
    return value if value >= 0 else default

CLIPS_COL = 2   # Clips column index, shared with sequence_timeline.py
RULER_HEIGHT = 24
PREVIEW_BAR_HEIGHT = 3   # preview cache bar drawn under the ruler marks


class RulerHeader(QHeaderView):
    # Horizontal header that paints a frame ruler and scrubber in the Clips section.
    def __init__(self, timeline):
        super().__init__(Qt.Horizontal)
        self.timeline = timeline
        self.setSectionsClickable(False)
        self.setSectionsMovable(False)
        self.setFixedHeight(RULER_HEIGHT + PREVIEW_BAR_HEIGHT)
        self._dragging = False

    def paintSection(self, painter, rect, logical_index):
        if logical_index != CLIPS_COL:
            super().paintSection(painter, rect, logical_index)
            return
        tl = self.timeline
        ppf = tl.pixels_per_frame
        fps = tl.fps
        painter.save()
        painter.setClipRect(rect)
        painter.fillRect(rect, QColor(40, 40, 40))
        top = rect.top()
        left = rect.left()
        total = tl.total_frames()
        start = tl.view_start_frame
        frame_pen = QPen(QColor(140, 140, 140))
        frame_pen.setWidth(1)
        second_pen = QPen(QColor(255, 255, 255))
        second_pen.setWidth(1)
        # Only paint frames in view: view_start_frame .. view_start_frame + visible
        first = start
        last = min(total - 1, start + tl.visible_frames())
        interval = ruler_label_interval(ppf * fps) if fps > 0 and ppf > 0 else 0
        # Frame ticks only when they are at least 1 px apart
        if ppf >= 1:
            painter.setPen(frame_pen)
            for f in range(first, last + 1):
                x = int(left + (f - start) * ppf)
                painter.drawLine(x, top, x, top + 1)
        # Second ticks: first frame of each second (fractional fps like 29.97).
        # Skipped when seconds are denser than 1 px; numbered ticks still draw.
        if fps > 0:
            sec_step = 1 if ppf * fps >= 1 else max(1, interval)
            sec = int(math.ceil(first / fps / sec_step)) * sec_step
            while True:
                f = int(math.ceil(sec * fps - 1e-9))
                if f > last:
                    break
                x = int(left + (f - start) * ppf)
                numbered = interval > 0 and sec % interval == 0
                painter.setPen(second_pen)
                painter.drawLine(x, top, x, top + (8 if numbered else 5))
                sec += sec_step
        # Time labels at a zoom-dependent interval, centered under the second tick
        if interval > 0:
            label_font = painter.font()
            label_font.setPointSizeF(max(6.0, label_font.pointSizeF() - 2))
            painter.setFont(label_font)
            painter.setPen(QColor(190, 190, 190))
            text_h = painter.fontMetrics().height()
            fm = painter.fontMetrics()
            text_y = min(rect.bottom() - PREVIEW_BAR_HEIGHT, top + 8 + text_h)
            first_sec = int(math.ceil(first / fps / interval)) * interval
            sec = first_sec
            while True:
                # Same frame the second tick is drawn on
                f = int(math.ceil(sec * fps - 1e-9))
                if f > last:
                    break
                x = int(left + (f - start) * ppf)
                # Centered on the tick, below its 8px numbered top
                text = ruler_label_text(sec)
                text_x = x - fm.horizontalAdvance(text) // 2
                painter.drawText(text_x, text_y, text)
                sec += interval
        # Preview cache bar under the ruler: red = missing or dirty,
        # green = clean row in {sequence_id}_preview.db. One range query.
        bar_y = rect.bottom() - PREVIEW_BAR_HEIGHT + 1
        # Clamped to the sequence: nothing drawn past the last frame
        bar_right = min(rect.right() + 1, int(left + (total - start) * ppf))
        if bar_right > rect.left():
            painter.fillRect(rect.left(), bar_y, bar_right - rect.left(),
                             PREVIEW_BAR_HEIGHT, QColor(170, 40, 40))
        clean = []
        if tl.sequence_id is not None and last >= first:
            try:
                # In-memory set (loaded once from {id}_preview.db): no db open
                from sequence_preview import clean_set
                cs = clean_set(tl._project_db(), tl.sequence_id)
                clean = sorted(f for f in list(cs) if first <= f <= last)
            except Exception:
                clean = []
        green = QColor(40, 170, 60)
        run_start = None
        prev = None
        for f in clean + [None]:
            if f is not None and prev is not None and f == prev + 1:
                prev = f
                continue
            if run_start is not None:
                x0 = int(left + (run_start - start) * ppf)
                x1 = int(left + (prev + 1 - start) * ppf)
                painter.fillRect(x0, bar_y, max(1, x1 - x0), PREVIEW_BAR_HEIGHT, green)
            run_start = prev = f
        # Scrubber, hidden when off screen
        sx = int(left + (tl.current_frame - start) * ppf)
        if rect.left() <= sx <= rect.right():
            # Playhead mark: center line full ruler height (it continues down
            # the tracks in PlayheadOverlay); side lines 1 x outer_h at sx-1
            # and sx+1, same top. SequencePlayer_TicCenterLengthPixels unused.
            # center_h = _tic_setting("SequencePlayer_TicCenterLengthPixels", 10)
            outer_h = _tic_setting("SequencePlayer_TicsOuterLengthPixels", 6)
            red = QColor(255, 80, 80)
            top = rect.top()
            painter.fillRect(sx, top, 1, rect.height(), red)
            painter.fillRect(sx - 1, top, 1, outer_h, red)
            painter.fillRect(sx + 1, top, 1, outer_h, red)
        painter.restore()

    def _frame_at(self, x):
        start = self.sectionViewportPosition(CLIPS_COL)
        tl = self.timeline
        frame = tl.view_start_frame + int(round((x - start) / tl.pixels_per_frame))
        return max(0, min(tl.total_frames() - 1, frame))

    def mousePressEvent(self, event):
        x = int(event.position().x())
        if self.logicalIndexAt(x) == CLIPS_COL and event.button() == Qt.LeftButton:
            self._dragging = True
            self.timeline.set_current_frame(self._frame_at(x))
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.timeline.set_current_frame(self._frame_at(int(event.position().x())))
            return
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        self.timeline.wheel_pan(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            return
        super().mouseReleaseEvent(event)


RULER_LABEL_INTERVALS = (1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600, 7200)
RULER_LABEL_MIN_PX = 60
ZOOM_MIN_PPF = 0.01
ZOOM_MAX_PPF = 20.0
ZOOM_STEP = 1.25


def clamp_zoom(ppf):
    try:
        ppf = float(ppf)
    except (TypeError, ValueError):
        ppf = 2.0
    if ppf != ppf:
        ppf = 2.0
    return max(ZOOM_MIN_PPF, min(ZOOM_MAX_PPF, ppf))


def ruler_label_interval(px_per_second):
    # Smallest interval (seconds) whose labels are at least 60 px apart;
    # the next step is at most 6x, so spacing lands about 60-120+ px
    for interval in RULER_LABEL_INTERVALS:
        if interval * px_per_second >= RULER_LABEL_MIN_PX:
            return interval
    return RULER_LABEL_INTERVALS[-1]


def ruler_label_text(seconds):
    # Under a minute: "0", "5", "10". From 60 s: "1:00", "1:30". From 1 h: "1:00:00"
    seconds = int(seconds)
    if seconds < 60:
        return str(seconds)
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


class PlayheadOverlay(QWidget):
    # Transparent child of the track table viewport: draws the playhead
    # center line through every visible track, on top of the clips.
    def __init__(self, timeline, viewport):
        super().__init__(viewport)
        self.timeline = timeline
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setGeometry(viewport.rect())
        self.raise_()

    def paintEvent(self, event):
        tl = self.timeline
        table = tl.table
        left = table.columnViewportPosition(CLIPS_COL)
        right = left + table.columnWidth(CLIPS_COL) - 1
        sx = int(left + (tl.current_frame - tl.view_start_frame) * tl.pixels_per_frame)
        if left <= sx <= right and 0 <= sx < self.width():
            painter = QPainter(self)
            painter.fillRect(sx, 0, 1, self.height(), QColor(255, 80, 80))
            painter.end()
