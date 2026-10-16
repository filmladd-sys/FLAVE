# clip_snap.py
# Pure snap math for timeline clip drags (move and trim). No Qt. ASCII only.


def snap_delta(raw_delta, edges, targets, ppf, snap_px):
    # Delta only (see snap_delta_target)
    return snap_delta_target(raw_delta, edges, targets, ppf, snap_px)[0]


def snap_delta_target(raw_delta, edges, targets, ppf, snap_px):
    # (delta, target_frame), or (None, None) when nothing snaps. Same
    # distance rule as before; target_frame is the frame snapped to.
    # raw_delta: unrounded frame delta from the mouse (float).
    # edges: dragged edge frames at delta 0 (move: in and out; trim: the
    # dragged edge). targets: frames of other clips' in and out points.
    # Returns the integer delta that puts the nearest edge exactly on the
    # nearest target within snap_px pixels at ppf pixels per frame, or None
    # when nothing is close enough (or snap_px <= 0).
    if snap_px <= 0 or ppf <= 0 or not edges or not targets:
        return None, None
    best = None
    for e in edges:
        pos = e + raw_delta
        for t in targets:
            dist = abs(t - pos) * ppf
            if dist <= snap_px and (best is None or dist < best[0]):
                best = (dist, int(t) - int(e), int(t))
    return (None, None) if best is None else (best[1], best[2])


def snap_targets(clips_by_track, exclude_ids):
    # In and out frames of every clip not in exclude_ids (all tracks)
    out = set()
    for clips in clips_by_track.values():
        for c in clips:
            if c[0] in exclude_ids:
                continue
            start = int(c[2])
            out.add(start)
            out.add(start + max(1, int(c[3])))
    return sorted(out)
