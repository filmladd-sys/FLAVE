# sequence_preview_diff.py
# BUILD 139: which preview frames an edit changes, from the timeline
# snapshot before and after it (sequence_player.timeline_snapshot, the same
# data the preview worker composites from). Pure Python, no Qt, no db.
# ASCII only.
#
# The sequence is cut into segments at every clip edge, keyframe clip edge
# and key (k and k + 1). Inside one segment every frame has the same layer
# stack, and each keyframe effect uses the same key pair (or hold value),
# so a per-segment descriptor that is equal before and after proves the
# frames are the same picture. Anything unequal is reported as changed.

import bisect

WHOLE = (0, 2 ** 40)


def _points(keys):
    # Same points / order as keyframe.evaluate_keyframes
    pts = []
    for item in keys or []:
        if item is None:
            continue
        if len(item) >= 3:
            pts.append((int(item[1]), float(item[2])))
        elif len(item) >= 2:
            pts.append((int(item[0]), float(item[1])))
    pts.sort(key=lambda p: (p[0], p[1]))
    return pts


def _bracket(pts, f, d=0):
    # The key pair evaluate_keyframes uses at frame f (or the held value).
    # d: frames to subtract (compare a shifted copy). None = no keys.
    if not pts:
        return None
    if len(pts) == 1 or f <= pts[0][0]:
        return ("h", pts[0][1])
    if f >= pts[-1][0]:
        return ("h", pts[-1][1])
    for i in range(len(pts) - 1):
        f0, v0 = pts[i]
        f1, v1 = pts[i + 1]
        if f0 <= f <= f1:
            if f1 == f0:
                return ("h", v1)
            return ("l", f0 - d, v0, f1 - d, v1)
    return ("h", pts[-1][1])


class _Effect(object):
    # One effect type (scale / position X / position Y / opacity / crop_x /
    # crop_y, BUILD 147; gamma / brightness / contrast, BUILD 154; rotate,
    # BUILD 219; border px and r / g / b / a channels, BUILD 222; move x and
    # y, BUILD 294; crop_xy x and y, BUILD 295): linked
    # keyframe clips indexed by link_id, as keyframe._pick_above_keyframe
    def __init__(self, clips):
        self.by_link = {}
        for link_id, in_f, out_f, keys, order in clips or []:
            if link_id is None or order is None:
                continue
            self.by_link.setdefault(link_id, []).append(
                (int(in_f), int(out_f), _points(keys), int(order)))

    def pick(self, link_id, f, clip_order):
        if link_id is None or clip_order is None:
            return None
        best = None
        for in_f, out_f, pts, order in self.by_link.get(link_id, ()):
            if order >= int(clip_order) or not (in_f <= f < out_f):
                continue
            if best is None or order > best[3]:
                best = (in_f, out_f, pts, order)
        return best

    def edges(self):
        out = []
        for items in self.by_link.values():
            for in_f, out_f, pts, _o in items:
                out.append(in_f)
                out.append(out_f)
                for k, _v in pts:
                    out.append(k)
                    out.append(k + 1)
        return out


class SnapshotIndex(object):
    # Per-frame layer descriptor of one snapshot, by segment
    def __init__(self, snap):
        rows = snap[0]
        clips_by_track = snap[1]
        self.track_order = snap[4] if len(snap) >= 5 else {}
        self.proxies = snap[6] if len(snap) >= 7 else {}
        # Frame rate conform by footage id (snap[19]): a changed footage or
        # sequence rate changes the source frames, so it is in desc
        self.fps_conform = snap[19] if len(snap) > 19 else {}
        # self.effects = [_Effect(snap[i] if len(snap) > i else [])
        #                 for i in (2, 3, 5, 7)]
        # BUILD 147: + crop_x (snap[8]) and crop_y (snap[9]): their clip
        # edges and keys are mile markers and in every span descriptor
        # self.effects = [_Effect(snap[i] if len(snap) > i else [])
        #                 for i in (2, 3, 5, 7, 8, 9)]
        # BUILD 154: + gamma / brightness / contrast (snap[10], [11], [12])
        # self.effects = [_Effect(snap[i] if len(snap) > i else [])
        #                 for i in (2, 3, 5, 7, 8, 9, 10, 11, 12)]
        # BUILD 219: + rotate (snap[13])
        # self.effects = [_Effect(snap[i] if len(snap) > i else [])
        #                 for i in (2, 3, 5, 7, 8, 9, 10, 11, 12, 13)]
        # BUILD 222: + border px and r / g / b / a (snap[14] .. [18]), so
        # a border key edit (any channel) refreshes the frames it changes
        # BUILD 294: + move x and y (snap[20], [21])
        # BUILD 295: + crop_xy x and y (snap[22], [23])
        self.effects = [_Effect(snap[i] if len(snap) > i else [])
                        for i in (2, 3, 5, 7, 8, 9, 10, 11, 12, 13,
                                  14, 15, 16, 17, 18, 20, 21, 22, 23)]
        any_solo = any(solo for _t, _e, solo, _k in rows)
        self.tracks = []    # bottom first: (kind, order, starts, clips, sorted)
        for track_id, enabled, solo, kind in reversed(rows):
            if not enabled or (any_solo and not solo):
                continue
            clips = list(clips_by_track.get(track_id, []))
            by_start = sorted(clips, key=lambda c: int(c[2]))
            no_overlap = by_start == clips and all(
                int(by_start[i][2]) + int(by_start[i][3]) <= int(by_start[i + 1][2])
                for i in range(len(by_start) - 1))
            self.tracks.append((kind, self.track_order.get(track_id),
                                [int(c[2]) for c in clips], clips, no_overlap))
        self.end = 0
        for t in self.tracks:
            for c in t[3]:
                self.end = max(self.end, int(c[2]) + int(c[3]))

    def edges(self):
        out = [0]
        for t in self.tracks:
            for c in t[3]:
                out.append(int(c[2]))
                out.append(int(c[2]) + int(c[3]))
        for e in self.effects:
            out.extend(e.edges())
        return out

    @staticmethod
    def _clip_at(track, f):
        # The composite takes the first enabled clip covering f in list
        # order. Sorted, non-overlapping track (the normal case): bisect.
        _kind, _order, starts, clips, no_overlap = track
        if no_overlap:
            i = bisect.bisect_right(starts, f) - 1
            if i >= 0:
                c = clips[i]
                if c[5] and int(c[2]) <= f < int(c[2]) + int(c[3]):
                    return c
            return None
        for c in clips:
            if c[5] and int(c[2]) <= f < int(c[2]) + int(c[3]):
                return c
        return None

    def desc(self, f, d=0):
        # Layer stack at frame f. d shifts it back by d frames, so a block
        # moved by d compares equal to its old copy.
        layers = []
        for track in self.tracks:
            kind, order = track[0], track[1]
            c = self._clip_at(track, f)
            if c is None:
                continue
            link_id = c[8] if len(c) > 8 else None
            conform = None
            if kind == "video":
                src = int(c[7]) - (int(c[2]) - d)
                conform = self.fps_conform.get(c[1])
            else:
                src = 0
            fx = []
            for e in self.effects:
                p = e.pick(link_id, f, order)
                fx.append(None if p is None else _bracket(p[2], f, d))
            layers.append((kind, c[1], src, self.proxies.get(c[0]), conform,
                           tuple(fx)))
        return tuple(layers)


def _segments(a, b, edges):
    # [lo, hi) pieces of [a, b) cut at edges
    cuts = sorted(set(e for e in edges if a < e < b))
    out = []
    lo = a
    for e in cuts:
        out.append((lo, e))
        lo = e
    if lo < b:
        out.append((lo, b))
    return out


def _merge(ranges):
    # Inclusive ranges, sorted and merged (touching ranges join)
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def changed_ranges(old_snap, new_snap):
    # Inclusive (first, last) frame ranges whose picture may differ
    old = SnapshotIndex(old_snap)
    new = SnapshotIndex(new_snap)
    end = max(old.end, new.end)
    if end <= 0:
        return []
    edges = old.edges() + new.edges()
    changed = []
    for lo, hi in _segments(0, end, edges):
        if old.desc(lo) != new.desc(lo):
            changed.append((lo, hi - 1))
    return _merge(changed)


def shift_ok(old_snap, new_snap, first, last, delta):
    # True when frames first..last of the old snapshot are, frame for frame,
    # the same picture at f + delta in the new one (a pure move)
    old = SnapshotIndex(old_snap)
    new = SnapshotIndex(new_snap)
    edges = old.edges() + [e - delta for e in new.edges()]
    for lo, hi in _segments(int(first), int(last) + 1, edges):
        a = old.desc(lo)
        if not a or a != new.desc(lo + delta, delta):
            return False
    return True


def subtract(ranges, cut):
    # Inclusive ranges minus inclusive ranges
    out = []
    for a, b in ranges:
        pieces = [(a, b)]
        for c0, c1 in cut:
            nxt = []
            for p0, p1 in pieces:
                if c1 < p0 or c0 > p1:
                    nxt.append((p0, p1))
                    continue
                if p0 < c0:
                    nxt.append((p0, c0 - 1))
                if c1 < p1:
                    nxt.append((c1 + 1, p1))
            pieces = nxt
        out.extend(pieces)
    return out
