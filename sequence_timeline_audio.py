# Audio-only helpers for sequence_timeline.py (no window class here).
# _draw_waveform is a ClipDelegate method; the rest are SequenceTimeline
# methods. Each takes the owning instance as self.
from PySide6.QtGui import QColor

AUDIO_EXTENSIONS = (".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg")


_PY_MAX_BUCKETS_PER_PX = 64


def _waveform_lines_np(values, n_buckets, left, x0, x1, first_bucket,
                       buckets_per_px, mid, half, scale):
    # Vertical min..max QLines for columns x0..x1 (the repainted part of the
    # clip) with numpy reductions over each column's buckets (same buckets
    # as the per-column loop). None when numpy is not available.
    try:
        import numpy as np
    except ImportError:
        return None
    from PySide6.QtCore import QLine
    pairs = np.frombuffer(values, dtype=np.int16) if not isinstance(values, list) \
        else np.asarray(values, dtype=np.int16)
    cols = np.arange(x0, x1 + 1, dtype=np.float64) - left
    b0 = np.maximum((first_bucket + cols * buckets_per_px).astype(np.int64), 0)
    b1 = (first_bucket + (cols + 1) * buckets_per_px).astype(np.int64)
    b1 = np.maximum(b1, b0 + 1)
    keep = b0 < n_buckets
    if not keep.any():
        return []
    b0, b1, cols = b0[keep], np.minimum(b1[keep], n_buckets), cols[keep]
    end = int(b1[-1])
    mins = pairs[0:2 * end:2]
    maxs = pairs[1:2 * end:2]
    # reduceat sums [b0[i], b0[i + 1]); one bucket where b0 does not advance
    lo = np.minimum.reduceat(mins, b0)
    hi = np.maximum.reduceat(maxs, b0)
    y_hi = mid - np.clip((hi * scale).astype(np.int64), -half, half)
    y_lo = mid - np.clip((lo * scale).astype(np.int64), -half, half)
    xs = (cols + left).astype(np.int64)
    return [QLine(int(x), int(a), int(x), int(b))
            for x, a, b in zip(xs.tolist(), y_hi.tolist(), y_lo.tolist())]


def _draw_waveform(self, painter, tl, clip_rect, footage_id, in_frame, length_frames,
                   gain_db=0.0):
    # Center line plus a vertical min..max bar per pixel column. The clip's
    # in_frame..in_frame+length_frames maps to peak buckets via fps.
    # No peaks row: the clip is drawn with no waveform.
    # Stored peaks are scaled by the clip gain (dB) at draw time
    # clamped to the clip height; the peaks themselves are not regenerated
    stored = tl.peaks_for(footage_id)
    if stored is None:
        return
    pps, _duration, values = stored
    n_buckets = len(values) // 2
    if n_buckets <= 0 or pps <= 0 or tl.fps <= 0:
        return
    top, h = clip_rect.top(), clip_rect.height()
    mid = top + h // 2
    half = max(1, h // 2 - 2)
    scale = half * (10.0 ** (float(gain_db) / 20.0)) / 32768.0
    painter.setPen(QColor(240, 240, 240))   # Waveform color
    painter.drawLine(clip_rect.left(), mid, clip_rect.right(), mid)
    # Only the visible part of the clip
    view = painter.clipBoundingRect().toRect() if painter.hasClipping() else clip_rect
    x0 = max(clip_rect.left(), view.left())
    x1 = min(clip_rect.right(), view.right())
    if x1 < x0:
        return
    w = max(1, clip_rect.width())
    buckets_per_px = (length_frames / float(tl.fps)) * pps / float(w)
    first_bucket = (in_frame / float(tl.fps)) * pps
    lines = _waveform_lines_np(values, n_buckets, clip_rect.left(), x0, x1,
                               first_bucket, buckets_per_px, mid, half, scale)
    if lines is not None:
        if lines:
            painter.drawLines(lines)
        return
    # Without numpy: per-column loop; wide columns sample at most
    # _PY_MAX_BUCKETS_PER_PX buckets so zoomed-out paints stay bounded
    for px in range(x0, x1 + 1):
        b0 = int(first_bucket + (px - clip_rect.left()) * buckets_per_px)
        b1 = int(first_bucket + (px - clip_rect.left() + 1) * buckets_per_px)
        if b1 <= b0:
            b1 = b0 + 1
        if b0 >= n_buckets:
            break
        b1 = min(b1, n_buckets)
        step = max(1, (b1 - b0) // _PY_MAX_BUCKETS_PER_PX)
        lo = min(values[2 * k] for k in range(b0, b1, step))
        hi = max(values[2 * k + 1] for k in range(b0, b1, step))
        y_hi = mid - max(-half, min(half, int(hi * scale)))
        y_lo = mid - max(-half, min(half, int(lo * scale)))
        painter.drawLine(px, y_hi, px, y_lo)


def _add_audio_track(self):
    self._add_track("audio", "+ Audio")


def _peaks_seconds(self, cur, footage_id):
    # Stored peaks duration_seconds for footage_id, 0.0 if none
    try:
        cur.execute("SELECT duration_seconds FROM peaks WHERE footage_id = ?", (footage_id,))
        row = cur.fetchone()
        return float(row[0]) if row and row[0] else 0.0
    except Exception:
        return 0.0


def _first_audio_track_id(self, cur):
    # Audio track with the lowest sort_order in this sequence, or None
    cur.execute(
        "SELECT id FROM tracks WHERE sequence_id = ? AND LOWER(track_type) = 'audio' "
        "ORDER BY sort_order, id LIMIT 1",
        (self.sequence_id,)
    )
    row = cur.fetchone()
    return row[0] if row else None


def _audio_length_frames(self, cur, footage_id):
    seconds = 0.0
    try:
        cur.execute("SELECT duration_seconds FROM peaks WHERE footage_id = ?",
                    (footage_id,))
        row = cur.fetchone()
        if row and row[0]:
            seconds = float(row[0])
        if seconds <= 0:
            cur.execute("SELECT duration FROM footage WHERE id = ?", (footage_id,))
            row = cur.fetchone()
            if row and row[0]:
                seconds = float(row[0])
    except Exception:
        seconds = 0.0
    if seconds <= 0:
        seconds = 1.0
    return max(1, int(round(seconds * self.fps)))


def peaks_for(self, footage_id):
    # Cached stored peaks for footage_id, or None (cached too)
    if footage_id not in self._peaks_cache:
        from peaks import load_peaks
        self._peaks_cache[footage_id] = load_peaks(self._project_db(), footage_id)
    return self._peaks_cache[footage_id]
