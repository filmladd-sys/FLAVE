# edit_points.py
# Pure |< / >| transport target math for the Sequence Timeline. ASCII only.
#
# The playhead seek (SequenceTimeline.set_current_frame) clamps to
# 0..total_frames-1. Out-points are exclusive (start + length), so the last
# clip's out-point is often total_frames or more and can never be reached.
# Targets are therefore clamped to that same reachable range before dedup.


def end_target(points, total_frames):
    # max(latest out-point, total_frames - 1)
    return max([int(total_frames) - 1] + [int(p) for p in points])


def jump_targets(points, total_frames):
    # Sorted, de-duplicated: 0, every edit point, and end_target, each
    # clamped to the reachable frames 0..total_frames-1
    last = max(0, int(total_frames) - 1)
    raw = {0, end_target(points, total_frames)}
    raw.update(int(p) for p in points)
    return sorted({max(0, min(last, t)) for t in raw})


def prev_edit_target(targets, current):
    # Largest target strictly before current; none (or at 0): the last target
    current = int(current)
    before = [t for t in targets if t < current]
    if current <= 0 or not before:
        return max(targets)
    return max(before)


def next_edit_target(targets, current):
    # Smallest target strictly after current; none (or at/after the last
    # target): 0
    current = int(current)
    after = [t for t in targets if t > current]
    if current >= max(targets) or not after:
        return 0
    return min(after)
