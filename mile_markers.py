# mile_markers.py
# BUILD 146: mile markers (change points). One sorted list of sequence
# frames where the composite (picture or sound) can change: every clip in /
# out, every keyframe clip edge and key (scale, position X / Y, opacity,
# volume; BUILD 147: crop_x / crop_y, BUILD 154: gamma / brightness /
# contrast, BUILD 219: rotate, BUILD 222: border px / color, BUILD 294:
# move x / y, BUILD 295: crop_xy x / y, BUILD 296: crop_edges, all via
# SnapshotIndex.effects), enable / disable, gain and proxy switches (attributes of a clip,
# so they change at its in / out). Between two markers the structure is
# constant (same clips, same keyframe interpolation segment); only the
# interpolated value moves (existing linear evaluation).
#
# A completed edit builds the new list and diffs it against the previous
# one: each span carries a descriptor (video: the layer stack; audio: the
# rows sounding in it, sample exact), spans whose descriptors differ are
# the dirty ranges, matching spans are kept. No edit types are walked.
# A pure move is found from the spans too: a dirty span whose content,
# relative to its own start, equals an old span (span hash) at some delta,
# verified frame for frame, is a relocation (JPEG rows copied, WAV samples
# copied) instead of a rebuild.
#
# Video and audio use the same list (MarkerList); the audio side compares
# against the list of the WAV on disk (rebuilt from its layout file), which
# can be older than the previous edit while a mix is pending. Pure Python,
# no Qt, no db. ASCII only.

import bisect
import math

from peaks import PEAKS_SAMPLE_RATE
from sequence_preview_diff import SnapshotIndex, _segments, _merge, subtract, WHOLE

SAMPLE_RATE = PEAKS_SAMPLE_RATE      # 48000, as sequence_preview_audio
MAX_CANDIDATES = 16                  # relocation deltas tried per diff


def _s(frame, fps):
    # Same as sequence_preview_audio._frame_to_sample
    if not fps or fps <= 0:
        fps = 29.97
    return int(round(float(frame) * SAMPLE_RATE / float(fps)))


# ---------------------------------------------------------------- video

class VideoIndex(SnapshotIndex):
    # The preview snapshot's layer stack per frame (sequence_preview_diff),
    # plus the footage path behind each footage id (a relink / undo changes
    # the picture with the same ids) and the sequence size.
    def __init__(self, snap, paths=None, size=None):
        SnapshotIndex.__init__(self, snap)
        self.paths = dict(paths or {})
        self.size = tuple(size) if size else None

    def desc(self, f, d=0):
        base = SnapshotIndex.desc(self, f, d)
        if not self.paths:
            return base
        return tuple((k, (fid, self.paths.get(fid)), s, p, cf, fx)
                     for k, fid, s, p, cf, fx in base)

    def rel(self, f):
        # Content relative to frame f (source frame and key frames counted
        # from f): equal for a block and its moved copy
        return self.desc(f, f)


def _rel_table(index, edges, end, rel):
    # rel descriptor -> [span starts] of the non-empty spans of a list
    table = {}
    for lo, _hi in _segments(0, end, edges):
        r = rel(lo)
        if r:
            table.setdefault(r, []).append(lo)
    return table


def _candidates(changed, new_edges, new_rel, old_table):
    # Relocation deltas: a dirty new span whose relative content equals an
    # old span's. Most frequent first, capped.
    count = {}
    for a, b in changed:
        for lo, _hi in _segments(a, b + 1, new_edges):
            r = new_rel(lo)
            if not r:
                continue
            for lo_o in old_table.get(r, ()):
                d = lo - lo_o
                if d:
                    count[d] = count.get(d, 0) + 1
    order = sorted(count.items(), key=lambda kv: (-kv[1], abs(kv[0])))
    return [d for d, _n in order[:MAX_CANDIDATES]]


def _clip_deltas(old, new):
    # BUILD 258: start deltas of the clips found in both snapshots (same
    # clip id) that moved, most frequent first. A ripple delete moves every
    # clip right of the cut by one delta, and a new span can start inside
    # an old span (a clip on another track that did not move ends there),
    # which the span-start table of _candidates cannot match. Proposals
    # only: diff_video proves every span before it relocates.
    starts = {}
    for t in old.tracks:
        for c in t[3]:
            starts[c[0]] = int(c[2])
    count = {}
    for t in new.tracks:
        for c in t[3]:
            s0 = starts.get(c[0])
            if s0 is not None and int(c[2]) != s0:
                d = int(c[2]) - s0
                count[d] = count.get(d, 0) + 1
    order = sorted(count.items(), key=lambda kv: (-kv[1], abs(kv[0])))
    return [d for d, _n in order[:MAX_CANDIDATES]]


def _runs(frames):
    # Sorted frame list -> inclusive runs
    out = []
    for f in frames:
        if out and f == out[-1][1] + 1:
            out[-1] = (out[-1][0], f)
        else:
            out.append((f, f))
    return out


def diff_video(old, new):
    # (dirty, relocations): dirty = inclusive frame ranges to rebuild;
    # relocations = [(first, last, delta)] in OLD frames: the clean JPEG
    # rows of first..last are the picture of first+delta..last+delta now
    # (proved span by span). Dirty never contains a relocation target.
    if old.size != new.size:
        return [WHOLE], []
    end = max(old.end, new.end)
    if end <= 0:
        return [], []
    old_e = old.edges()
    new_e = new.edges()
    changed = []
    for lo, hi in _segments(0, end, old_e + new_e):
        if old.desc(lo) != new.desc(lo):
            changed.append((lo, hi - 1))
    changed = _merge(changed)
    if not changed:
        return [], []
    table = _rel_table(old, old_e, old.end, old.rel)
    left = list(changed)
    relocs = []
    cands = _candidates(changed, new_e, new.rel, table)
    cands += [d for d in _clip_deltas(old, new) if d not in cands]
    for d in cands:
        if not left:
            break
        cuts = new_e + [e + d for e in old_e]
        hit = []
        for a, b in left:
            for lo, hi in _segments(a, b + 1, cuts):
                if lo - d < 0:
                    continue
                o = old.desc(lo - d)
                if o and o == new.desc(lo, d):
                    hit.append((lo, hi - 1))
        hit = _merge(hit)
        if not hit:
            continue
        for p, q in hit:
            relocs.append((p - d, q - d, d))
        left = subtract(left, hit)
        left = [r for r in left if r[1] >= r[0]]
    return _merge(left), relocs


# ---------------------------------------------------------------- audio

def _keys_at(pts, frame):
    return tuple(v for k, v in pts if k == frame)


class _Row(object):
    __slots__ = ("idx", "track", "basename", "ident", "gain", "link", "start",
                 "in_f", "length", "a0", "a1", "reach", "src0", "count",
                 "fend", "cands")


class AudioIndex(object):
    # Per-frame sound descriptor of one mix layout (sequence_preview_audio
    # _layout_of: rows in mix order, volume clips, track order, fps, n).
    # A frame's descriptor determines every sample of that frame exactly
    # (same rows in the same sum order, same source offset in samples, same
    # gain, same written footprint inside the frame, same volume segment
    # for the frame and for its first sample, which the volume mask may
    # count in the frame before), so equal descriptors = equal samples.
    def __init__(self, layout):
        self.fps = float(layout["fps"])
        self.total = int(layout["total"])
        self.n = int(layout["n"])
        order = dict((int(t), i) for i, t in enumerate(layout.get("tracks") or []))
        vols = []
        for link_id, in_f, out_f, pts, track_id in layout.get("vols") or []:
            o = order.get(track_id)
            if o is None or not pts:
                continue
            vols.append((link_id, int(in_f), int(out_f),
                         [(int(k), float(v)) for k, v in pts], int(o)))
        fps, n = self.fps, self.n
        self.rows = []
        by_track = {}
        marks = set([0])
        for i, r in enumerate(layout.get("rows") or []):
            track_id, footage_id, basename, start, in_f, length, link, gain, ident = r
            start, in_f, length = int(start), int(in_f), int(length)
            if length <= 0 or not basename:
                continue
            a0 = _s(start, fps)
            count = _s(length, fps)
            src0 = _s(in_f, fps)
            if count <= 0 or a0 >= n:
                continue
            try:
                slen = int(str(ident).split(":")[1]) // 2
            except Exception:
                slen = None
            take = count if slen is None else min(count, slen - src0)
            if take <= 0:
                continue
            a1 = min(n, a0 + take)
            if a1 <= a0:
                continue
            row = _Row()
            row.idx = i
            row.track = int(track_id)
            row.basename = basename
            row.ident = ident
            row.gain = float(gain or 0.0)
            row.link = link
            row.start, row.in_f, row.length = start, in_f, length
            row.a0, row.a1 = a0, a1
            row.reach = min(n, a0 + count)
            row.src0, row.count = src0, count
            row.fend = self.frame_of(a1 - 1)
            clip_order = order.get(int(track_id))
            row.cands = []
            if link is not None and clip_order is not None:
                row.cands = sorted([v for v in vols if v[0] == link and v[4] < clip_order],
                                   key=lambda v: v[4])
            self.rows.append(row)
            by_track.setdefault(row.track, []).append(row)
            marks.update((start, row.fend, row.fend + 1))
            for _l, vin, vout, pts, _o in row.cands:
                marks.update((vin, vin + 1, vout, vout + 1))
                for k, _v in pts:
                    marks.update((k, k + 1))
        self.tracks = []
        for rows in by_track.values():
            rows.sort(key=lambda r: (r.start, r.idx))
            self.tracks.append(([r.start for r in rows], rows,
                                max(r.fend - r.start for r in rows)))
        self.end = min(self.total, max([r.fend + 1 for r in self.rows] or [0]))
        self.marks = sorted(m for m in marks if 0 <= m <= self.total)

    def frame_of(self, x):
        # Frame whose samples [S(g), S(g + 1)) hold sample x
        g = int(x * self.fps / SAMPLE_RATE)
        while _s(g + 1, self.fps) <= x:
            g += 1
        while g > 0 and _s(g, self.fps) > x:
            g -= 1
        return g

    def present(self, g):
        # Rows writing a sample of frame g, in mix (sum) order
        out = []
        for starts, rows, span in self.tracks:
            i = bisect.bisect_right(starts, g) - 1
            while i >= 0 and starts[i] >= g - span - 1:
                r = rows[i]
                if r.start <= g <= r.fend:
                    out.append(r)
                i -= 1
        out.sort(key=lambda r: r.idx)
        return out

    def _vol(self, r, g):
        # Volume segment applied at frame g (mask frame): keys at the last
        # key frame <= g and the first >= g + 1 of the nearest clip above
        win = None
        for v in r.cands:
            if v[1] <= g < v[2]:
                win = v
        if win is None:
            return None
        pts = win[3]
        lower = [k for k, _v in pts if k <= g]
        upper = [k for k, _v in pts if k >= g + 1]
        lo = max(lower) if lower else None
        hi = min(upper) if upper else None
        return (lo, _keys_at(pts, lo) if lo is not None else None,
                hi, _keys_at(pts, hi) if hi is not None else None,
                None if lo is not None else pts[0][1])

    def vol_state(self, r, g):
        if not r.cands:
            return None
        a, b = self._vol(r, g), self._vol(r, g - 1)
        if a is None and b is None:
            return None
        return (a, b)

    def desc(self, g):
        fs, fe = _s(g, self.fps), _s(g + 1, self.fps)
        out = []
        for r in self.present(g):
            lo, hi = max(r.a0, fs), min(r.a1, fe)
            part = None if (lo == fs and hi == fe) else (lo - fs, hi - fs)
            out.append((r.basename, r.ident, r.gain, r.src0 - r.a0, part,
                        self.vol_state(r, g)))
        return tuple(out)

    def rel(self, g):
        # Content relative to frame g for move matching; None (no match)
        # where a volume curve applies (its gain follows the absolute
        # sample position, so a moved copy is not sample exact)
        out = []
        for r in self.present(g):
            if self.vol_state(r, g) is not None:
                return None
            out.append((r.basename, r.ident, r.gain, r.src0, r.count, g - r.start))
        return tuple(out)


def _ident(r):
    return (r.basename, r.ident, r.gain, r.src0, r.count, r.length, r.in_f)


def _audio_pair(old, new, g_old, g_new, d):
    # Sample delta when every row of old frame g_old is the same row moved
    # by d frames in new frame g_new (same order, no volume curve), else None
    a, b = old.present(g_old), new.present(g_new)
    if not a or len(a) != len(b):
        return None
    ds = None
    for ro, rn in zip(a, b):
        if _ident(ro) != _ident(rn) or rn.start - ro.start != d:
            return None
        if old.vol_state(ro, g_old) is not None or new.vol_state(rn, g_new) is not None:
            return None
        k = rn.a0 - ro.a0
        if ds is None:
            ds = k
        elif k != ds:
            return None
    return ds


def _union(ranges):
    out = []
    for a, b in sorted(ranges):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _minus(ranges, cut):
    # [a, b) sample ranges minus [a, b) ranges
    out = []
    cut = _union(cut)
    for a, b in ranges:
        pieces = [(a, b)]
        for c0, c1 in cut:
            nxt = []
            for p0, p1 in pieces:
                if c1 <= p0 or c0 >= p1:
                    nxt.append((p0, p1))
                    continue
                if p0 < c0:
                    nxt.append((p0, c0))
                if c1 < p1:
                    nxt.append((c1, p1))
            pieces = nxt
        out.extend(pieces)
    return _union(out)


def _intersect(ranges, other):
    out = []
    other = _union(other)
    for a, b in _union(ranges):
        for c0, c1 in other:
            lo, hi = max(a, c0), min(b, c1)
            if lo < hi:
                out.append((lo, hi))
    return _union(out)


def plan_audio(old, new):
    # Sample plan turning the WAV of AudioIndex old into the WAV of new
    # (see sequence_preview_audio _partial_pcm), or None (full mix: other
    # fps). Keys: n_old, n_new, copies [(src, dst, len)], zero, remix
    # ([a, b) samples), remix_len, copy_len, frames (touched), remix_frames
    # (the bar's red: remixed or emptied spans; a relocation's copy and the
    # emptied span it left are not red), dirty (frame ranges).
    if float(old.fps) != float(new.fps):
        return None
    fps = new.fps
    n_old, n_new = old.n, new.n
    end = min(new.total, max(old.end, new.end))
    changed = []
    if end > 0:
        for lo, hi in _segments(0, end, old.marks + new.marks):
            if old.desc(lo) != new.desc(lo):
                changed.append((lo, hi - 1))
    changed = _merge(changed)
    # relocations (pure moves), frame runs with one sample delta each
    copies = []
    moved_frames = []      # new frames whose samples are copied
    source_frames = []     # old frames a relocation came from
    left = list(changed)
    if changed:
        table = _rel_table(old, old.marks, old.end, old.rel)
        for d in _candidates(changed, new.marks, new.rel, table):
            if not left:
                break
            cuts = new.marks + [m + d for m in old.marks]
            runs = []
            for a, b in left:
                for lo, hi in _segments(a, b + 1, cuts):
                    if lo - d < 0:
                        continue
                    ds = _audio_pair(old, new, lo - d, lo, d)
                    if ds is None:
                        continue
                    if runs and runs[-1][1] == lo - 1 and runs[-1][2] == ds:
                        runs[-1] = (runs[-1][0], hi - 1, ds)
                    else:
                        runs.append((lo, hi - 1, ds))
            got = []
            for p, q, ds in runs:
                x0 = max(_s(p, fps), _s(p - d, fps) + ds, ds, 0)
                x1 = min(_s(q + 1, fps), _s(q - d + 1, fps) + ds, n_old + ds, n_new)
                if x1 <= x0:
                    continue
                copies.append((x0 - ds, x0, x1 - x0))
                got.append((p, q))
                source_frames.append((p - d, q - d))
            if got:
                moved_frames.extend(got)
                left = [r for r in subtract(left, got) if r[1] >= r[0]]
    # samples to rewrite: changed frames, minus the copied samples; split
    # into remix (a new row may write there) and zero (no new row reaches)
    need = _union([(_s(a, fps), min(n_new, _s(b + 1, fps))) for a, b in changed])
    need = _minus(need, [(dst, dst + ln) for _src, dst, ln in copies])
    reach = _union([(r.a0, r.reach) for r in new.rows])
    remix = _intersect(need, reach)
    zero = _minus(need, reach)
    remix_len = sum(b - a for a, b in remix)
    touched = _union(remix + zero + [(dst, dst + ln) for _src, dst, ln in copies])
    # bar: dirty frames that are not a relocation target or its emptied source
    red = [r for r in subtract(changed, _merge(moved_frames)) if r[1] >= r[0]]
    srcs = _merge(source_frames)
    if srcs:
        keep = [r for r in subtract(red, srcs) if r[1] >= r[0]]
        for a, b in red:
            for s0, s1 in srcs:
                lo, hi = max(a, s0), min(b, s1)
                if lo <= hi:
                    keep.extend((g0, g1 - 1) for g0, g1 in _segments(lo, hi + 1, new.marks)
                                if new.present(g0))
        red = keep
    return {"n_old": n_old, "n_new": n_new, "copies": copies, "zero": zero,
            "remix": remix, "remix_len": remix_len,
            "copy_len": sum(ln for _s0, _d, ln in copies),
            "frames": _sample_frames(touched, fps),
            "remix_frames": _merge(red), "dirty": changed}


def _sample_frames(ranges, fps):
    per = fps / float(SAMPLE_RATE)
    frames = []
    for a, b in ranges:
        f0 = int(a * per)
        f1 = int(math.ceil(b * per)) - 1
        frames.append((f0, max(f0, f1)))
    return frames


# ---------------------------------------------------------------- the list

class MarkerList(object):
    # The mile markers of one timeline state: the sorted change points of
    # the picture and the sound, with the per-span descriptors behind them.
    # video: VideoIndex (preview snapshot) or None; audio: AudioIndex (mix
    # layout) or None. inputs / layout: the audio mix inputs it came from.
    def __init__(self, video=None, audio=None, inputs=None, layout=None):
        self.video = video
        self.audio = audio
        self.inputs = inputs
        self.layout = layout
        marks = set([0])
        if video is not None:
            marks.update(e for e in video.edges() if e >= 0)
        if audio is not None:
            marks.update(audio.marks)
        self.markers = sorted(marks)

    def spans(self):
        # [lo, hi) between consecutive markers
        m = self.markers
        return [(m[i], m[i + 1]) for i in range(len(m) - 1)]
