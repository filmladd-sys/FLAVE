# edit_points.py
# Pure |< / >| transport target math for the Sequence Timeline. ASCII only.


def end_target(points, total_frames):
    # max(latest out-point, total_frames - 1)
    return max([int(total_frames) - 1] + [int(p) for p in points])


def prev_edit_target(points, current, end):
    # Previous edit point strictly before current; wraps to end when there
    # is none or current is 0
    current = int(current)
    before = [p for p in points if p < current]
    if current <= 0 or not before:
        return int(end)
    return max(before)


def next_edit_target(points, current, end):
    # Next edit point strictly after current; wraps to 0 when there is none
    # or current is at or past end
    current = int(current)
    after = [p for p in points if p > current]
    if current >= int(end) or not after:
        return 0
    return min(after)
