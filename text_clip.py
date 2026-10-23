# text_clip.py
# WYSIWYG Text clips (BUILD 308): one text box per clip on a 'text' track
# (WYSIWYG Text), stored in the text_clips table (new_project.TEXT_CLIPS_SQL).
# Per-clip style: font family, font size (sequence px), color '#RRGGBB';
# x / y are the box center in sequence pixels. BUILD 311: optional drop
# shadow (shadow_on, shadow_color '#RRGGBBAA', shadow_blur radius px,
# shadow_angle, shadow_offset px; see new_project.TEXT_CLIPS_SQL and
# shadow_delta). This module holds the ids,
# the load / add / update helpers (link_id: sequence_timeline_captions.
# persist_caption_link_id) and the text image (QPainter -> RGBA
# QImage, cached). Timeline wiring: sequence_timeline_text.py. Style
# window: text_style_window.py. ASCII only.

import sqlite3
import threading

TEXT_TRACK_TYPE = "text"
TEXT_TRACK_LABEL = "WYSIWYG Text"
TEXT_TABLE = "text_clips"
# In-memory clip ids: -(TEXT_ID_BASE + text_clips.id), below the keyframe
# clip range (keyframe.KEYFRAME_ID_BASE) and the caption range (-captions.id)
TEXT_ID_BASE = 2000000000
DEFAULT_TEXT = "Text"
DEFAULT_COLOR = "#ffffff"
DEFAULT_SECONDS = 5
# BUILD 311: the five shadow fields follow the six text fields (style
# tuples of load_text_styles, player_text.text_rgba)
TEXT_STYLE_KEYS = ("text", "font_family", "font_size", "color", "x", "y",
                   "shadow_on", "shadow_color", "shadow_blur", "shadow_angle",
                   "shadow_offset")
SHADOW_KEYS = TEXT_STYLE_KEYS[6:]
DEFAULT_SHADOW_COLOR = "#000000B4"
DEFAULT_SHADOW_BLUR = 6
DEFAULT_SHADOW_ANGLE = 135
DEFAULT_SHADOW_OFFSET = 6


def is_text_clip_id(clip_id):
    return clip_id is not None and int(clip_id) <= -TEXT_ID_BASE


def text_mem_id(db_id):
    return -(TEXT_ID_BASE + int(db_id))


def text_db_id(clip_id):
    return (-int(clip_id)) - TEXT_ID_BASE


def first_line(text):
    # Timeline label: the first line of the text
    return (text or "").split("\n", 1)[0]


def default_font_family():
    # The application's default (sans) font family
    try:
        from PySide6.QtGui import QGuiApplication
        return QGuiApplication.font().family()
    except Exception:
        return "Sans Serif"


def default_font_size(seq_h):
    # Sequence height / 12, in sequence px
    return max(1, int(round(int(seq_h or 1080) / 12.0)))


def load_text_clips(cur, sequence_id, clips):
    # Text clips into clips ({track_id: [entry]}): entries like captions,
    # [mem id, None, start, length, first line of text, 1, 0, 0, link_id]
    try:
        cur.execute(
            "SELECT id, track_id, start_frame, length_frames, text, link_id "
            "FROM text_clips WHERE sequence_id = ? ORDER BY start_frame, id",
            (sequence_id,))
    except sqlite3.OperationalError:
        return      # project without the table
    for tid, track_id, start, length, text, link_id in cur.fetchall():
        clips.setdefault(track_id, []).append(
            [text_mem_id(tid), None, max(0, int(start or 0)),
             max(1, int(length or 1)), first_line(text), 1, 0, 0, link_id])


def insert_text_clip(cur, sequence_id, track_id, start_frame, length_frames,
                     seq_w, seq_h, family=None):
    # New text_clips row with the defaults ('Text', default sans font,
    # size seq_h / 12, white, centered); returns its in-memory id
    cur.execute(
        "INSERT INTO text_clips (sequence_id, track_id, start_frame, "
        "length_frames, text, font_family, font_size, color, x, y) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(sequence_id), int(track_id), int(start_frame),
         int(length_frames), DEFAULT_TEXT, family or default_font_family(),
         default_font_size(seq_h), DEFAULT_COLOR,
         int(seq_w) // 2, int(seq_h) // 2))
    return text_mem_id(cur.lastrowid)


def read_text_style(project_db, clip_mem_id):
    # {key: value} of TEXT_STYLE_KEYS (text, font, color, position and the
    # shadow fields) of one text clip, or None
    try:
        conn = sqlite3.connect(project_db)
        try:
            row = conn.execute(
                "SELECT %s FROM text_clips WHERE id = ?" % ", ".join(
                    TEXT_STYLE_KEYS), (text_db_id(clip_mem_id),)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return dict(zip(TEXT_STYLE_KEYS, row)) if row else None


def write_text_field(cur, clip_mem_id, key, value):
    # One style field of one text clip (key from TEXT_STYLE_KEYS)
    if key not in TEXT_STYLE_KEYS:
        raise ValueError("unknown text clip field: %s" % key)
    cur.execute("UPDATE text_clips SET %s = ? WHERE id = ?" % key,
                (value, text_db_id(clip_mem_id)))


def load_text_styles(project_db, sequence_id):
    # BUILD 309: {mem id: (text, font_family, font_size, color, x, y,
    # shadow_on, shadow_color, shadow_blur, shadow_angle, shadow_offset)} of
    # every text clip of the sequence (TEXT_STYLE_KEYS order), the
    # compositor's text layers (timeline_snapshot index [29]). {} on any
    # failure or a project without the table.
    out = {}
    if not project_db or sequence_id is None:
        return out
    try:
        conn = sqlite3.connect(project_db)
        try:
            rows = conn.execute(
                "SELECT id, %s FROM text_clips WHERE sequence_id = ?" % ", ".join(
                    TEXT_STYLE_KEYS), (int(sequence_id),)).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, TypeError, ValueError):
        return out
    for row in rows:
        out[text_mem_id(row[0])] = tuple(row[1:])
    return out


# ---- Text image: the clip's box drawn at its place in a sequence-size
# transparent frame (QImage ARGB32 premultiplied), the layer the
# compositor blends like an image clip (player_text uploads it). Cached
# per (clip, text, style, position, shadow, sequence size); a change of any of
# them makes a new entry. BUILD 309: at most _IMAGE_CACHE_MAX host images
# (one is seq_w x seq_h x 4 bytes, 8 MB at 1080p); the GUI thread and
# the compositor threads share the cache under _IMAGE_LOCK.

_IMAGE_CACHE = {}
_IMAGE_CACHE_MAX = 8
_IMAGE_LOCK = threading.Lock()


def text_font(family, size_px):
    from PySide6.QtGui import QFont
    font = QFont(family or default_font_family())
    font.setPixelSize(max(1, int(size_px or 1)))
    return font


def text_box_rect(text, family, size_px, x, y):
    # QRect of the text box (all lines, centered on x / y) in sequence px
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtCore import QRect, Qt
    fm = QFontMetrics(text_font(family, size_px))
    r = fm.boundingRect(QRect(0, 0, 100000, 100000),
                        int(Qt.AlignHCenter | Qt.AlignTop), text or "")
    w, h = r.width(), r.height()
    return QRect(int(x) - w // 2, int(y) - h // 2, w, h)


def parse_rgba(color, default=DEFAULT_SHADOW_COLOR):
    # (r, g, b, a) 0..255 of '#RRGGBBAA' or '#RRGGBB' (alpha 255); the
    # default's on a bad value
    for c in (color, default):
        h = (c or "").strip().lstrip("#")
        try:
            if len(h) in (6, 8):
                v = [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]
                return tuple(v + [255] * (4 - len(v)))
        except ValueError:
            pass
    return (0, 0, 0, 255)


def shadow_delta(angle, offset):
    # (dx, dy) sequence px of the shadow: offset px in the direction angle,
    # degrees clockwise from straight up (90 = right, 135 = down-right,
    # 180 = down, 270 = left; the Premiere / After Effects Drop Shadow
    # direction); screen y grows down
    import math
    a = math.radians(float(angle or 0))
    d = float(offset or 0)
    return int(round(d * math.sin(a))), int(round(-d * math.cos(a)))


def norm_shadow(shadow):
    # Shadow fields (on, color, blur, angle, offset) with defaults for
    # None, or None when the shadow is off or invisible (alpha 0)
    if not shadow:
        return None
    on, color, blur, angle, offset = (tuple(shadow) + (None,) * 5)[:5]
    if not on:
        return None
    color = color or DEFAULT_SHADOW_COLOR
    if parse_rgba(color)[3] <= 0:
        return None
    blur = DEFAULT_SHADOW_BLUR if blur is None else max(0, int(blur))
    angle = DEFAULT_SHADOW_ANGLE if angle is None else int(angle) % 360
    offset = DEFAULT_SHADOW_OFFSET if offset is None else int(offset)
    return (1, color, blur, angle, offset)


def _box_blur(arr, k, axis):
    # Running mean of width 2k + 1 along axis (zeros beyond the edges)
    import numpy as np
    n = arr.shape[axis]
    pad = [(0, 0), (0, 0)]
    pad[axis] = (k + 1, k)
    c = np.cumsum(np.pad(arr, pad), axis=axis)
    hi = np.take(c, np.arange(2 * k + 1, 2 * k + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return (hi - lo) / float(2 * k + 1)


def _shadow_image(mask, blur, rgba):
    # Shadow picture from mask (Format_RGBA8888_Premultiplied, the text in
    # white): its alpha blurred, times the shadow alpha, in the shadow
    # color. Blur: three box passes per axis of half-width round(blur / 3)
    # (numpy; reach ~blur px, close to a Gaussian of sigma ~blur / 2.4).
    # Without numpy: the same box passes with QPainter (the tinted mask
    # drawn 2k + 1 times shifted, additive, each at 1 / (2k + 1) opacity;
    # 8-bit rounding makes it slightly fainter).
    from PySide6.QtGui import QImage, QPainter, QColor
    from PySide6.QtCore import Qt
    r, g, b, a = rgba
    w, h = mask.width(), mask.height()
    k = int(round(blur / 3.0)) if blur > 0 else 0
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        raw = np.frombuffer(bytes(mask.constBits())[: mask.sizeInBytes()],
                            dtype=np.uint8)
        al = raw.reshape(h, mask.bytesPerLine())[:, : w * 4].reshape(
            h, w, 4)[:, :, 3].astype(np.float32)
        if k > 0:
            for _ in range(3):
                al = _box_blur(al, k, 1)
                al = _box_blur(al, k, 0)
        al = np.clip(al * (a / 255.0), 0.0, 255.0)
        out = np.empty((h, w, 4), dtype=np.uint8)
        out[:, :, 0] = np.round(al * (r / 255.0))
        out[:, :, 1] = np.round(al * (g / 255.0))
        out[:, :, 2] = np.round(al * (b / 255.0))
        out[:, :, 3] = np.round(al)
        return QImage(out.tobytes(), w, h, w * 4,
                      QImage.Format_RGBA8888_Premultiplied).copy()
    img = mask.copy()
    p = QPainter(img)
    try:
        p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p.fillRect(img.rect(), QColor(r, g, b, a))
    finally:
        p.end()
    for _ in range(3 if k > 0 else 0):
        for horizontal in (True, False):
            out = QImage(w, h, QImage.Format_RGBA8888_Premultiplied)
            out.fill(Qt.transparent)
            p = QPainter(out)
            try:
                p.setCompositionMode(QPainter.CompositionMode_Plus)
                p.setOpacity(1.0 / (2 * k + 1))
                for i in range(-k, k + 1):
                    p.drawImage(i if horizontal else 0, 0 if horizontal else i, img)
            finally:
                p.end()
            img = out
    return img


def _draw_shadow(p, text, family, size_px, x, y, shadow):
    # Draw the drop shadow of the text box with painter p (before the
    # text): the text mask in a margin big enough for the blur, blurred,
    # placed at the box offset by shadow_delta
    from PySide6.QtGui import QImage, QPainter, QColor
    from PySide6.QtCore import Qt, QRect
    _on, color, blur, angle, offset = shadow
    box = text_box_rect(text, family, size_px, x, y)
    k = int(round(blur / 3.0)) if blur > 0 else 0
    m = 3 * k + max(2, int(size_px or 1) // 4)    # blur reach + overhangs
    mask = QImage(box.width() + 2 * m, box.height() + 2 * m,
                  QImage.Format_RGBA8888_Premultiplied)
    mask.fill(Qt.transparent)
    mp = QPainter(mask)
    try:
        mp.setRenderHint(QPainter.Antialiasing)
        mp.setRenderHint(QPainter.TextAntialiasing)
        mp.setFont(text_font(family, size_px))
        mp.setPen(QColor(255, 255, 255))
        mp.drawText(QRect(m, m, box.width(), box.height()),
                    int(Qt.AlignHCenter | Qt.AlignTop), text)
    finally:
        mp.end()
    dx, dy = shadow_delta(angle, offset)
    p.drawImage(box.left() - m + dx, box.top() - m + dy,
                _shadow_image(mask, blur, parse_rgba(color)))


def render_text_image(text, family, size_px, color, x, y, seq_w, seq_h,
                      clip_key=None, shadow=None):
    # Sequence-size RGBA image, transparent except the text (center-aligned
    # lines, box centered on x / y). clip_key (e.g. the clip id) only
    # separates cache entries. BUILD 311: shadow (shadow_on, shadow_color,
    # shadow_blur, shadow_angle, shadow_offset), when on, is drawn under
    # the text (_draw_shadow).
    from PySide6.QtGui import QImage, QPainter, QColor
    from PySide6.QtCore import Qt
    # x / y unset: the frame center
    x = int(seq_w) // 2 if x is None else x
    y = int(seq_h) // 2 if y is None else y
    shadow = norm_shadow(shadow)
    key = (clip_key, text, family, int(size_px or 1), color, int(x), int(y),
           int(seq_w), int(seq_h), shadow)
    with _IMAGE_LOCK:
        img = _IMAGE_CACHE.get(key)
    if img is not None:
        return img
    img = QImage(int(seq_w), int(seq_h), QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    if (text or "").strip():
        p = QPainter(img)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            if shadow is not None:
                _draw_shadow(p, text, family, size_px, x, y, shadow)
            p.setFont(text_font(family, size_px))
            c = QColor(color or DEFAULT_COLOR)
            p.setPen(c if c.isValid() else QColor(DEFAULT_COLOR))
            p.drawText(text_box_rect(text, family, size_px, x, y),
                       int(Qt.AlignHCenter | Qt.AlignTop), text)
        finally:
            p.end()
    with _IMAGE_LOCK:
        while len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX:
            _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)))
        _IMAGE_CACHE[key] = img
    return img
