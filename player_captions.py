# player_captions.py
# BUILD 102: soft-caption overlay for the Sequence Player. Captions are
# drawn with QPainter on top of the presented GL picture, right before the
# buffer swap. They are never part of the CUDA composite, the preview JPEGs
# or {ProjectName}_{sequence_id}_Preview.db. ASCII only.
#
# caption_snapshot() runs once per timeline snapshot (GUI thread, one DB
# query); paint_captions() runs per shown frame and reads only the snapshot.
#
# Captions table columns used: text, font_family, font_size, pos_x, pos_y,
# scale (all exist in new_project._init_db). NULL font/size/pos/scale
# means the default style: bottom-centered white text on a dark
# translucent box. pos_x/pos_y are sequence pixels (the caption edit
# window leaves them NULL unless set). Per axis (BUILD 103): a set X
# centers the box on that X, blank X is horizontal center; a set Y centers
# the box on that Y, blank Y uses the default bottom stack. font_size is in sequence pixels, times scale.
# Font size, wrap width, padding and position all scale with the displayed
# picture (sequence px times picture height / sequence height), so a
# windowed player shows a scaled-down copy of the full screen layout.
# BUILD 298: text is measured with the painter that draws it (the overlay
# image) and laid out in the same wrap-width rect, so the wrapping drawn is
# the wrapping measured and no line falls outside its box.

import sqlite3

from PySide6.QtCore import Qt

DEFAULT_FONT_FAMILY = "Arial"
DEFAULT_SIZE_FRAC = 1.0 / 18.0     # default font px as a share of seq height
BOTTOM_MARGIN_FRAC = 0.06          # gap under the lowest default caption
WIDTH_FRAC = 0.8                   # wrap width as a share of picture width
GAP_FRAC = 0.01                    # gap between stacked default captions


def caption_snapshot(tl):
    # Returns a list of (track_row, start, length, text, font_family,
    # font_size, pos_x, pos_y, scale) for enabled caption clips on enabled
    # caption tracks. track_row 0 = top track.
    tracks = {}
    try:
        for r in range(tl.table.rowCount()):
            item = tl.table.item(r, 0)
            if item is None:
                continue
            if (item.data(Qt.UserRole + 4) or "").lower() != "caption":
                continue
            if not bool(item.data(Qt.UserRole + 1)):
                continue                       # track disabled
            tracks[item.data(Qt.UserRole)] = r
    except Exception:
        return []
    if not tracks:
        return []
    # Clip enabled flag from the in-memory clips (captions have no enabled
    # column today, so this is always 1; honored if that changes)
    enabled = {}
    for tid in tracks:
        for c in (getattr(tl, "clips_by_track", {}) or {}).get(tid, []):
            try:
                if c[0] is not None and c[0] < 0:
                    enabled[-int(c[0])] = bool(c[5])
            except Exception:
                pass
    try:
        project_db = tl._project_db()
    except Exception:
        project_db = None
    if not project_db:
        return []
    out = []
    try:
        conn = sqlite3.connect(project_db)
        try:
            rows = conn.execute(
                "SELECT id, track_id, start_frame, length_frames, text, "
                "font_family, font_size, pos_x, pos_y, scale "
                "FROM captions WHERE sequence_id = ?",
                (int(tl.sequence_id),)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    for (cid, tid, start, length, text, fam, size, px, py, scale) in rows:
        if tid not in tracks or not enabled.get(int(cid), True):
            continue
        if not (text or "").strip():
            continue
        out.append((tracks[tid], int(start or 0), max(1, int(length or 1)),
                    text, fam, size, px, py, scale))
    return out


def captions_at(snap, frame):
    # Captions covering the frame, drawn bottom track first, top track last
    hit = [c for c in (snap or []) if c[1] <= frame < c[1] + c[2]]
    hit.sort(key=lambda c: (-c[0], c[1]))
    return hit


def default_slots(n_default):
    # Stack index per default caption in draw order: the top track (drawn
    # last) gets slot 0 = lowest on screen; lower tracks stack above it.
    return [n_default - 1 - i for i in range(n_default)]


def paint_captions(painter, snap, frame, pic_x, pic_y, pic_w, pic_h,
                   seq_w, seq_h):
    # pic_* is the letterboxed picture rect in device-independent pixels
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QFont, QColor
    caps = captions_at(snap, frame)
    if not caps or pic_w <= 0 or pic_h <= 0:
        return 0
    k = pic_h / float(max(1, seq_h))        # sequence px -> display px
    wrap_w = pic_w * WIDTH_FRAC
    flags = int(Qt.AlignHCenter | Qt.TextWordWrap)
    laid = []
    for c in caps:
        _row, _s, _l, text, fam, size, px, py, scale = c
        try:
            sc = float(scale) if scale else 1.0
        except (TypeError, ValueError):
            sc = 1.0
        if size:
            fpx = float(size) * sc * k
        else:
            fpx = seq_h * DEFAULT_SIZE_FRAC * sc * k
        font = QFont(fam or DEFAULT_FONT_FAMILY)
        font.setPixelSize(max(6, int(round(fpx))))
        # BUILD 298: measured on the painter's own device (not the screen's
        # QFontMetricsF, whose widths can differ from what the image draws)
        painter.setFont(font)
        br = painter.boundingRect(QRectF(0, 0, wrap_w, 100000), flags,
                                  str(text).replace("\r\n", "\n"))
        pad = max(2.0, fpx * 0.25)
        bw, bh = br.width() + 2 * pad, br.height() + 2 * pad
        custom = py is not None      # Y set: placed, not stacked
        laid.append([c, font, pad, bw, bh, custom, px, py])
    defaults = [l for l in laid if not l[5]]
    slots = default_slots(len(defaults))
    # Lowest slot sits on the bottom margin; each slot above adds the
    # heights of the ones below it
    by_slot = sorted(zip(slots, defaults), key=lambda t: t[0])
    y_bottom = pic_y + pic_h - pic_h * BOTTOM_MARGIN_FRAC
    pos = {}
    for _slot, l in by_slot:
        pos[id(l)] = y_bottom - l[4]
        y_bottom -= l[4] + pic_h * GAP_FRAC
    box = QColor(0, 0, 0, 160)
    white = QColor(255, 255, 255)
    painter.save()
    for l in laid:
        c, font, pad, bw, bh, custom, px, py = l
        if px is not None:
            cx = pic_x + float(px) * k
        else:
            cx = pic_x + pic_w / 2.0
        if custom:
            top = pic_y + float(py) * k - bh / 2.0
        else:
            top = pos[id(l)]
        r = QRectF(cx - bw / 2.0, top, bw, bh)
        painter.fillRect(r, box)
        painter.setFont(font)
        painter.setPen(white)
        # BUILD 298: lines laid out in the wrap-width rect they were
        # measured in, centered on the box (the box is the measured text
        # plus pad), never clipped. The tight box rect used before re-wrapped
        # the last word onto a hidden line when drawn text came out a
        # fraction wider than measured (e.g. 'Called FLAVE,' -> 'Called').
        painter.drawText(QRectF(cx - wrap_w / 2.0, top + pad, wrap_w,
                                bh - 2 * pad),
                         flags | int(Qt.TextDontClip),
                         str(c[3]).replace("\r\n", "\n"))
    painter.restore()
    return len(laid)


def caption_image(snap, frame, pw, ph, seq_w, seq_h):
    # BUILD 104: captions for the frame painted on the CPU into a
    # transparent premultiplied image the size of the displayed picture
    # (device pixels). The player uploads it as an overlay texture.
    from PySide6.QtGui import QImage, QPainter
    img = QImage(max(1, int(pw)), max(1, int(ph)),
                 QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    try:
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        paint_captions(p, snap, frame, 0, 0, img.width(), img.height(),
                       seq_w, seq_h)
    finally:
        p.end()
    return img
