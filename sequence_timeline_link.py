# sequence_timeline_link.py
# Link / Unlink menu decision and the Link write plan for the
# Sequence Timeline. Pure functions over clip lists (clip[0] = mem id,
# clip[8] = link_id) and clips_by_track. ASCII only.
#
# A link_id that only one clip carries (e.g. a partner removed by Delete
# Track) counts as unlinked here.


def link_group_sizes(clips_by_track):
    # {link_id: member count} over every clip type in the sequence
    sizes = {}
    for clips in (clips_by_track or {}).values():
        for c in clips:
            lid = c[8] if len(c) > 8 else None
            if lid is not None:
                sizes[lid] = sizes.get(lid, 0) + 1
    return sizes


def effective_link(clip, sizes):
    # The clip's link_id when its group has 2+ members, else None
    lid = clip[8] if len(clip) > 8 else None
    if lid is None or sizes.get(lid, 0) < 2:
        return None
    return lid


def link_menu_choice(selected, clip, clips_by_track):
    # ("unlink", True) or ("link", enabled). selected = the clips as
    # selected / displayed (normal click = clip + partners). Two or more
    # selected: all in one group -> Unlink; any unlinked member or two
    # groups -> Link; none linked -> Link. One (or none) selected: the
    # clicked clip's own state (Link disabled, nothing to link with).
    sizes = link_group_sizes(clips_by_track)
    sel = list(selected or [])
    if len(sel) < 2:
        if effective_link(clip, sizes) is not None:
            return ("unlink", True)
        return ("link", False)
    links = set(effective_link(c, sizes) for c in sel)
    if len(links) == 1 and None not in links:
        return ("unlink", True)
    return ("link", True)


def link_plan(selected, clip, clips_by_track):
    # (link_id, clips to write) for Link. Winner: the right-clicked clip's
    # group, else the first selected clip (selection order) that is in a
    # group, else the first selected clip's mem id (pre-133 rule). Every
    # selected clip plus ALL members of each group the selection touches
    # join the winner (groups merge, never split); clips already carrying
    # the winner are not rewritten. None when fewer than 2 are selected.
    sel = list(selected or [])
    if len(sel) < 2:
        return None
    sizes = link_group_sizes(clips_by_track)
    winner = None
    if clip is not None and any(c[0] == clip[0] for c in sel):
        winner = effective_link(clip, sizes)
    if winner is None:
        for c in sel:
            winner = effective_link(c, sizes)
            if winner is not None:
                break
    if winner is None:
        winner = sel[0][0]
    groups = set(effective_link(c, sizes) for c in sel) - {None}
    out = []
    seen = set()
    for c in sel:
        if c[0] not in seen:
            seen.add(c[0])
            out.append(c)
    for clips in (clips_by_track or {}).values():
        for c in clips:
            if c[0] not in seen and effective_link(c, sizes) in groups:
                seen.add(c[0])
                out.append(c)
    write = [c for c in out if (c[8] if len(c) > 8 else None) != winner]
    return winner, write
