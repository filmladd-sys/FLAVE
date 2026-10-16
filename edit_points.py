# edit_points.py
# Pure |< / >| transport target math for the Sequence Timeline. ASCII only.


def end_target(points, total_frames):
    # max(latest out-point, total_frames - 1)
    return max([int(total_frames) - 1] + [int(p) for p in points])


def jump_targets(points, total_frames):
    # Sorted, de-duplicated: 0, every edit point, and end_target
    targets = {0, end_target(points, total_frames)}
    targets.update(int(p) for p in points)
    return sorted(targets)


def prev_edit_target(targets, current):
    # Largest target strictly before current; none: the last target
    current = int(current)
    before = [t for t in targets if t < current]
    return max(before) if before else max(targets)


def next_edit_target(targets, current):
    # Smallest target strictly after current; none: 0
    current = int(current)
    after = [t for t in targets if t > current]
    return min(after) if after else 0
