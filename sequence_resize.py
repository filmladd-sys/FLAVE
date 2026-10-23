# sequence_resize.py
# BUILD 316: Sequence Settings resolution presets and the rescale that runs
# when an existing sequence changes size (sequence_add.SequenceAddDialog).
# ASCII only.
#
# The size may change only to the same aspect ratio (integer cross
# multiply, same_aspect); then k = new_w / old_w and every value stored in
# sequence pixels is multiplied by k so the sequence looks the same:
#   - position X / position Y keys (keyframes.value, absolute center px)
#   - move keys (keyframe_move x, y, absolute center px)
#   - border keys (keyframe_border px, clamped to the border range)
#   - text clips: x, y, font_size, shadow_blur, shadow_offset (INTEGER
#     columns: rounded, clamped to the Text Style ranges; NULL stays NULL)
#   - scale keys of scale keyframe clips that govern a video / image clip
#     (a footage clip's 100% is its native pixel size, so it must grow by k)
# Video / image clips with no scale key (and keyless scale keyframe clips)
# stay at their native 100% size, centered when they have no position key:
# nothing is added for them (user decision, BUILD 316).
# Left alone: crops (source px), captions, opacity, color, rotate, volume,
# sequence clips, keys of scale keyframe clips that govern only text clips
# (text is drawn on a sequence-size canvas with its font already scaled).

import math

# (label, width, height) in drop-down order
PRESETS = (
    # BUILD 322: 480p presets, square pixels (Wide: 480 * 16 / 9 = 853.33,
    # rounded to the even 854 the encoders need; aspect 1.779 vs 1.778)
    ("NTSC 480p (640x480)", 640, 480),
    ("Wide 480p (854x480)", 854, 480),
    ("1K Horizontal (1920x1080)", 1920, 1080),
    ("2K Horizontal (2560x1440)", 2560, 1440),
    ("3K Horizontal (3200x1800)", 3200, 1800),
    ("4K Horizontal (3840x2160)", 3840, 2160),
    ("5K Horizontal (5120x2880)", 5120, 2880),
    ("6K Horizontal (6144x3456)", 6144, 3456),
    ("1K Portrait (1080x1920)", 1080, 1920),
    ("2K Portrait (1440x2560)", 1440, 2560),
    ("3K Portrait (1800x3200)", 1800, 3200),
    ("4K Portrait (2160x3840)", 2160, 3840),
    ("5K Portrait (2880x5120)", 2880, 5120),
    ("6K Portrait (3456x6144)", 3456, 6144),
)
DEFAULT_SIZE = (1920, 1080)
NOT_IMPLEMENTED_TEXT = "This feature has not been implemented yet"


def preset_index(width, height):
    # Index into PRESETS of (width, height), or None
    for i, (_label, w, h) in enumerate(PRESETS):
        if (w, h) == (int(width), int(height)):
            return i
    return None


def custom_label(width, height):
    return "Custom (%dx%d)" % (int(width), int(height))


def same_aspect(old_w, old_h, new_w, new_h):
    return int(old_w) * int(new_h) == int(new_w) * int(old_h)


def _round_int(v):
    return int(math.floor(float(v) + 0.5))


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _overlaps(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def rescale_sequence(cur, sequence_id, old_w, old_h, new_w, new_h):
    # Rescale every sequence-pixel value of one sequence by k = new_w /
    # old_w (caller checked same_aspect and runs this inside its own
    # transaction, after push_undo). Returns a stats dict.
    from keyframe import KEY_VALUE_RANGES
    from text_clip import (FONT_SIZE_RANGE, SHADOW_BLUR_RANGE,
                           SHADOW_OFFSET_RANGE)
    sid = int(sequence_id)
    k = float(new_w) / float(old_w)
    stats = {"k": k}

    # track order: table row (0 = top), like timeline_snapshot
    tracks = cur.execute(
        "SELECT id, track_type, name, sort_order FROM tracks "
        "WHERE sequence_id = ? ORDER BY sort_order, id", (sid,)).fetchall()
    order = dict((t[0], i) for i, t in enumerate(tracks))
    kind = dict((t[0], (t[1] or "").lower()) for t in tracks)

    kclips = cur.execute(
        "SELECT id, track_id, type, link_id, in_frame, out_frame "
        "FROM keyframe_clips WHERE sequence_id = ?", (sid,)).fetchall()

    def ids_of(ktype):
        return [r[0] for r in kclips if r[2] == ktype]

    def update_in(sql, ids):
        n = 0
        for i in ids:
            n += cur.execute(sql, (int(i),)).rowcount
        return n

    # position X / Y: absolute center px
    stats["position_keys"] = update_in(
        "UPDATE keyframes SET value = value * %r WHERE keyframe_clip_id = ?"
        % k, ids_of("position X") + ids_of("position Y"))
    # move: absolute center px
    stats["move_keys"] = update_in(
        "UPDATE keyframe_move SET x = x * %r, y = y * %r "
        "WHERE keyframe_clip_id = ?" % (k, k), ids_of("move"))
    # border width px
    lo, hi = KEY_VALUE_RANGES.get("border", (0.0, 1000.0))
    stats["border_keys"] = update_in(
        "UPDATE keyframe_border SET px = MAX(%r, MIN(%r, px * %r)) "
        "WHERE keyframe_clip_id = ?" % (float(lo), float(hi), k),
        ids_of("border"))

    # text clips
    n_text = 0
    rows = cur.execute(
        "SELECT id, x, y, font_size, shadow_blur, shadow_offset "
        "FROM text_clips WHERE sequence_id = ?", (sid,)).fetchall()
    for tid, x, y, fs, blur, off in rows:
        def sc(v, rng=None):
            if v is None:
                return None
            r = _round_int(float(v) * k)
            return _clamp(r, rng[0], rng[1]) if rng else r
        cur.execute(
            "UPDATE text_clips SET x = ?, y = ?, font_size = ?, "
            "shadow_blur = ?, shadow_offset = ? WHERE id = ?",
            (sc(x), sc(y), sc(fs, FONT_SIZE_RANGE),
             sc(blur, SHADOW_BLUR_RANGE), sc(off, SHADOW_OFFSET_RANGE), tid))
        n_text += 1
    stats["text_clips"] = n_text

    # scale: keys of scale clips that govern video / image footage clips
    # (not sequence clips)
    visual = []     # [clip id, link_id, order, start, end]
    for cid, tr, fid, start, length, link, ref in cur.execute(
            "SELECT c.id, c.track_id, c.footage_id, c.start_frame, "
            "c.length_frames, c.link_id, c.sequence_id_ref FROM clips c "
            "JOIN tracks t ON t.id = c.track_id WHERE t.sequence_id = ?",
            (sid,)).fetchall():
        if kind.get(tr) not in ("video", "image") or fid is None \
                or ref is not None or tr not in order:
            continue
        s = max(0, int(start or 0))
        visual.append([cid, link, order[tr], s, s + max(1, int(length or 1))])
    texts = []
    for tr, start, length, link in cur.execute(
            "SELECT track_id, start_frame, length_frames, link_id "
            "FROM text_clips WHERE sequence_id = ?", (sid,)).fetchall():
        if tr in order:
            s = max(0, int(start or 0))
            texts.append((link, order[tr], s, s + max(1, int(length or 1))))

    scale_kfs = []  # (id, link, order, in, out)
    for kid, tr, ktype, link, in_f, out_f in kclips:
        if ktype != "scale" or link is None or tr not in order:
            continue
        a = max(0, int(in_f or 0))
        scale_kfs.append((kid, link, order[tr], a, max(a + 1, int(out_f or a + 1))))

    def governs(kf, targets):
        kid, link, o, a, b = kf
        return any(t[0] == link and t[1] > o and _overlaps(a, b, t[2], t[3])
                   for t in targets)

    vis_t = [(v[1], v[2], v[3], v[4]) for v in visual]
    n_scale = 0
    n_mixed = 0
    for kf in scale_kfs:
        if not governs(kf, vis_t):
            continue
        if governs(kf, texts):
            n_mixed += 1
        n_scale += cur.execute("UPDATE keyframes SET value = value * %r "
                               "WHERE keyframe_clip_id = ?" % k,
                               (int(kf[0]),)).rowcount
    stats["scale_keys"] = n_scale
    stats["scale_clips_with_text"] = n_mixed
    return stats
