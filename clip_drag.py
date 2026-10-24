# clip_drag.py
# Time-move position for a clip drag that may jump past a
# blocking clip. Pure functions (no Qt). ASCII only.
#
# Deltas are whole frames added to every moved clip's drag-start start.
# A delta is legal when every moved clip stays at frame >= 0 and overlaps
# no other clip on its own track. The legal deltas form sorted components
# [lo, hi] (hi None = no limit to the right).
#
# Rule (move_delta): the desired delta (mouse frame - grab offset - drag
# start, after snap) is used as is when legal. When it is not, the group
# is in front of a blocking region (one clip, or several with gaps too
# small to fit). It stays at the near edge until the MOUSE itself is past
# the region's far edge, then lands at the first legal spot beyond it:
#   moving right: mouse frame >= region end   -> lo of the next component
#   moving left:  mouse frame <  region start -> hi of the previous one
# "Region" edges are measured on the dragged clip's own geometry
# (grab offset g, length L), so for linked partners blocked on another
# track it is as if that blocker were under the mouse's clip.


def legal_components(members):
    # members: [(start, length, [(b0, b1), ...])] -- b = other clips on
    # that member's track (not moving). Returns [[lo, hi], ...] sorted.
    if not members:
        return [[0, None]]
    lo = max(-int(s) for s, _l, _b in members)
    banned = []
    for s, ln, blocks in members:
        s, ln = int(s), max(1, int(ln))
        for b0, b1 in blocks or []:
            b0, b1 = int(b0), int(b1)
            if b1 <= b0:
                continue
            # overlap when b0 < s + d + ln and s + d < b1
            a, z = b0 - s - ln + 1, b1 - s - 1
            if z >= lo:
                banned.append((max(a, lo), z))
    banned.sort()
    comps = []
    cur = lo
    for a, z in banned:
        if a > cur:
            comps.append([cur, a - 1])
        cur = max(cur, z + 1)
    comps.append([cur, None])
    return comps


def _comp_index(comps, d):
    for i, (lo, hi) in enumerate(comps):
        if d >= lo and (hi is None or d <= hi):
            return i
    return None


def move_delta(comps, cur_delta, desired, grab, length):
    # Delta to apply. cur_delta: the delta applied now (0 at drag start);
    # its component tells the side the group is on. grab: the dragged
    # clip's grab offset (mouse frame - clip start at press), length: its
    # length.
    desired = int(desired)
    if not comps:
        return 0
    if _comp_index(comps, desired) is not None:
        return desired
    if desired < comps[0][0]:
        return comps[0][0]          # frame 0 (or a clip at the start)
    k = _comp_index(comps, int(cur_delta))
    if k is None:
        # Started overlapped (should not happen): nearest legal value
        k = 0
        best = None
        for i, (lo, hi) in enumerate(comps):
            v = lo if desired < lo else (hi if hi is not None else desired)
            if best is None or abs(v - desired) < best:
                best, k = abs(v - desired), i
    # desired sits in the gap between components j and j + 1
    j = max(i for i, (lo, _hi) in enumerate(comps) if lo <= desired)
    near_lo_hi = comps[j][1]
    far_lo = comps[j + 1][0]
    g = max(0, int(grab))
    ln = max(1, int(length))
    if k <= j:
        # Moving right: mouse = start + d + g, region end = start + far_lo
        return far_lo if desired + g >= far_lo else near_lo_hi
    # Moving left: region start = start + near_lo_hi + ln
    return near_lo_hi if desired + g < near_lo_hi + ln else far_lo


def overlaps(members_now):
    # Drop guard: members_now [(start, length, blocks)] at the final
    # position; True if any is below 0 or overlaps a block
    for s, ln, blocks in members_now:
        s, ln = int(s), max(1, int(ln))
        if s < 0:
            return True
        for b0, b1 in blocks or []:
            if b0 < s + ln and b1 > s:
                return True
    return False
