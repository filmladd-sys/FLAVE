# sequence_timeline_ruler.py
# Frame ruler and scrubber header for the Sequence Timeline, plus zoom and
# ruler label helpers. No timeline window here. ASCII only.

import math
from PySide6.QtWidgets import QHeaderView, QWidget
from PySide6.QtCore import Qt, QTimer
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
# BUILD 130: scrub auto-scroll. Within SCRUB_EDGE_PX of either edge of the
# visible Clips area the view pans and the playhead stays SCRUB_EDGE_PX in.
# While the button is held in (or past) a margin, a SCRUB_TICK_MS timer keeps
# panning at SCRUB_TICK_GAIN px per tick per px of depth (depth capped at
# SCRUB_DEPTH_MAX px).
SCRUB_EDGE_PX = 60
SCRUB_TICK_MS = 30
SCRUB_TICK_GAIN = 0.25
SCRUB_DEPTH_MAX = 240
PREVIEW_BAR_HEIGHT = 3   # preview cache bar drawn under the ruler marks
AUDIO_BAR_HEIGHT = PREVIEW_BAR_HEIGHT   # BUILD 137: audio mix bar below it


class RulerHeader(QHeaderView):
    # Horizontal header that paints a frame ruler and scrubber in the Clips section.
    def __init__(self, timeline):
        super().__init__(Qt.Horizontal)
        self.timeline = timeline
        self.setSectionsClickable(False)
        self.setSectionsMovable(False)
        self.setFixedHeight(RULER_HEIGHT + PREVIEW_BAR_HEIGHT + AUDIO_BAR_HEIGHT)
        self._dragging = False
        # BUILD 130: scrub auto-scroll state
        self._scrub_x = None        # last mouse x in the Clips area
        self._scrub_rem = 0.0       # sub-frame pan remainder (px)
        self._scrub_panned = False  # view moved during this drag
        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(SCRUB_TICK_MS)
        self._edge_timer.timeout.connect(self._edge_tick)

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
            text_y = min(rect.bottom() - PREVIEW_BAR_HEIGHT - AUDIO_BAR_HEIGHT,
                         top + 8 + text_h)
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
        # BUILD 137: the video bar sits right above the audio bar
        bar_y = rect.bottom() - AUDIO_BAR_HEIGHT - PREVIEW_BAR_HEIGHT + 1
        # Clamped to the sequence: nothing drawn past the last frame
        bar_right = min(rect.right() + 1, int(left + (total - start) * ppf))
        if bar_right > rect.left():
            painter.fillRect(rect.left(), bar_y, bar_right - rect.left(),
                             PREVIEW_BAR_HEIGHT, QColor(170, 40, 40))
            # BUILD 137: audio mix bar, one state for the whole sequence:
            # green = {id}_previewaudio.wav current, red = dirty / missing /
            # mixing / failed. Same extent as the video bar. Cached state
            # only (sequence_preview_audio.audio_bar_ok): no query here.
            # BUILD 139: ranges -- [] green, None all red, else green with
            # the still-dirty frame ranges red (audio_bar_ranges, cached)
            audio_dirty = None
            if tl.sequence_id is not None:
                try:
                    from sequence_preview_audio import audio_bar_ranges
                    audio_dirty = audio_bar_ranges(tl._project_db(), tl.sequence_id)
                except Exception:
                    audio_dirty = None
            a_y = bar_y + PREVIEW_BAR_HEIGHT
            a_w = bar_right - rect.left()
            if audio_dirty is None:
                painter.fillRect(rect.left(), a_y, a_w, AUDIO_BAR_HEIGHT,
                                 QColor(170, 40, 40))
            else:
                painter.fillRect(rect.left(), a_y, a_w, AUDIO_BAR_HEIGHT,
                                 QColor(40, 170, 60))
                for f0, f1 in audio_dirty:
                    if f1 < first or f0 > last:
                        continue
                    x0 = max(rect.left(), int(left + (f0 - start) * ppf))
                    x1 = min(bar_right, int(left + (f1 + 1 - start) * ppf))
                    if x0 < bar_right:
                        painter.fillRect(x0, a_y, max(1, x1 - x0),
                                         AUDIO_BAR_HEIGHT, QColor(170, 40, 40))
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
        # Snap line (white, playhead center-line style) while a drag snaps
        snap = getattr(tl, "_snap_frame", None)
        if snap is not None:
            wx = int(left + (snap - start) * ppf)
            if rect.left() <= wx <= rect.right():
                painter.fillRect(wx, rect.top(), 1, rect.height(), QColor(255, 255, 255))
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
            self._scrub_x = None
            self._scrub_rem = 0.0
            self._scrub_panned = False
            # BUILD 130: a click inside a margin pans at once (the clicked
            # frame ends SCRUB_EDGE_PX in); holding there keeps panning
            self._edge_scrub(x)
            self._edge_timer.start()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            # Outside the widget too (the press grabs the mouse)
            self._edge_scrub(int(event.position().x()))
            return
        super().mouseMoveEvent(event)

    def _clips_geometry(self):
        # (left x of the Clips area in this header, visible Clips width)
        start = self.sectionViewportPosition(CLIPS_COL)
        w = min(self.sectionSize(CLIPS_COL), self.viewport().width() - start)
        return start, max(1, w)

    def _edge_scrub(self, x, tick=False):
        # Mouse x (header coordinates) -> view pan + playhead (scrub_edge)
        tl = self.timeline
        start, w = self._clips_geometry()
        xc = x - start
        last = xc if tick else self._scrub_x
        view, frame, rem = scrub_edge(
            xc, last, w, tl.view_start_frame, tl.pixels_per_frame,
            tl.total_frames(), tl.visible_frames(), self._scrub_rem, tick)
        self._scrub_x = xc
        self._scrub_rem = rem
        if view != tl.view_start_frame:
            # The existing horizontal scroll: view_start_frame + repaint of
            # the ruler, the clips and the overlays (playhead, drop preview)
            tl.view_start_frame = view
            self._scrub_panned = True
            self.viewport().update()
            tl.table.viewport().update()
            overlay = getattr(tl, "playhead_overlay", None)
            if overlay is not None:
                overlay.update()
        self._scrub_to(frame)

    def _edge_tick(self):
        # Held in or past a margin: keep panning (stops on release)
        if not self._dragging or self._scrub_x is None:
            self._edge_timer.stop()
            return
        start, _w = self._clips_geometry()
        self._edge_scrub(int(self._scrub_x + start), tick=True)

    def _scrub_to(self, frame):
        # BUILD 111: move the playhead; if the frame changed (and not
        # playing), a one-frame audio burst from the preview mix WAV
        tl = self.timeline
        before = tl.current_frame
        tl.set_current_frame(frame)
        if tl.current_frame != before:
            from sequence_player_audio import scrub_burst
            scrub_burst(tl)

    def wheelEvent(self, event):
        self.timeline.wheel_pan(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            self._edge_timer.stop()
            if self._scrub_panned:
                # Keep the panned view (saved once, not per move)
                self._scrub_panned = False
                try:
                    self.timeline._save_view_state()
                except Exception:
                    pass
            return
        super().mouseReleaseEvent(event)


def scrub_edge(x, last_x, width, view, ppf, total, visible, rem=0.0, tick=False,
               margin=SCRUB_EDGE_PX):
    # BUILD 130: one scrub step. x / last_x: mouse x in the Clips area (may
    # be < 0 or > width when past the widget); last_x None = press.
    # Returns (view_start_frame, playhead frame, sub-frame remainder px).
    # Middle region: frame under the mouse, view unchanged. In a margin
    # (x < margin or x > width - margin) while the view can still move:
    # pan by the mouse travel into the margin toward that edge (a press
    # pans the clicked frame to the margin), plus on a timer tick
    # SCRUB_TICK_GAIN * depth; the playhead stays at the margin. At frame 0
    # (left) or the end (right) the view stops and the playhead follows the
    # mouse up to the edge.
    ppf = float(ppf) if ppf and ppf > 0 else 1.0
    total = max(1, int(total))
    width = max(1, int(width))
    view = int(view)
    max_start = max(0, total - max(1, int(visible)))
    m = max(0, min(int(margin), (width - 1) // 3))
    left_m, right_m = m, width - m

    def at(px, v):
        return max(0, min(total - 1, v + int(round(px / ppf))))

    if m > 0 and x < left_m and view > 0:
        ref = left_m if last_x is None else min(last_x, left_m)
        pan = max(0.0, ref - x)
        if tick:
            pan += SCRUB_TICK_GAIN * min(SCRUB_DEPTH_MAX, left_m - x)
        f = (pan + rem) / ppf
        df = int(f)
        rem = (f - df) * ppf
        view = max(0, view - df)
        if view == 0:
            return view, at(x, view), 0.0
        return view, at(left_m, view), rem
    if m > 0 and x > right_m and view < max_start:
        ref = right_m if last_x is None else max(last_x, right_m)
        pan = max(0.0, x - ref)
        if tick:
            pan += SCRUB_TICK_GAIN * min(SCRUB_DEPTH_MAX, x - right_m)
        f = (pan + rem) / ppf
        df = int(f)
        rem = (f - df) * ppf
        view = min(max_start, view + df)
        if view == max_start:
            return view, at(x, view), 0.0
        return view, at(right_m, view), rem
    return view, at(x, view), 0.0


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
        # BUILD 125: footage drop preview boxes
        if getattr(tl, "_drop_preview", None):
            from sequence_timeline_droppreview import paint_preview
            painter = QPainter(self)
            paint_preview(tl, painter)
            painter.end()
        # BUILD 131: slice-mode line (yellow, 1 px) on the clip under the
        # mouse and its link partners
        if getattr(tl, "_slice_lines", None):
            from sequence_timeline_slice import paint_slice_lines
            painter = QPainter(self)
            paint_slice_lines(tl, painter)
            painter.end()
        # BUILD 148: rubber-band selection box (white border)
        if getattr(tl, "_rubber_rect", None) is not None:
            from sequence_timeline_rubber import paint_rubber
            painter = QPainter(self)
            paint_rubber(tl, painter)
            painter.end()
        # Snap line (white) through every track while a drag snaps
        snap = getattr(tl, "_snap_frame", None)
        if snap is not None:
            wx = int(left + (snap - tl.view_start_frame) * tl.pixels_per_frame)
            if left <= wx <= right and 0 <= wx < self.width():
                painter = QPainter(self)
                painter.fillRect(wx, 0, 1, self.height(), QColor(255, 255, 255))
                painter.end()
