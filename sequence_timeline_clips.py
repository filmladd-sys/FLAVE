# sequence_timeline_clips.py
# BUILD 162: clip parts of the Sequence Timeline window, split out of
# sequence_timeline.py with no behavior change: ClipDelegate and its paint
# helpers (clip backgrounds, collapsed clip bars, clip names, sequence clip
# icons), clip hit tests, click selection, move drag / edge trim (snap,
# cross-track move, preview ranges) and the clip right-click menu.
# Functions take the SequenceTimeline as self; sequence_timeline.py imports
# them and attaches them to the window class (same pattern as
# sequence_timeline_sql.py / sequence_timeline_tracks.py). The collapse
# triangle cell is painted by sequence_timeline_tracks.paint_collapse_cell.
# Code already in its own file (clipboard, slice, rubber band, link,
# keyframe, markers, sql, tracks) is imported, not moved. ASCII only.

import sqlite3
from bisect import bisect_left, bisect_right
from sequence_timeline_ruler import CLIPS_COL
from sequence_timeline_video import (out_frame_for, video_out_pixmap,
                                     icon_source_frame)
from sequence_timeline_audio import _draw_waveform
from sequence_timeline_captions import (
    is_caption_id, clip_table_and_id, paint_caption_bar,
)
from sequence_timeline_sql import (
    EDGE_HIT_PX, COLLAPSE_COL, ROLE_COLLAPSED, EDIT_TRACK_TYPES,
    _get_global_setting,
)
from sequence_timeline_tracks import paint_collapse_cell
from PySide6.QtWidgets import (
    QMessageBox, QMenu, QApplication, QStyledItemDelegate,
)
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QPainter, QColor, QPixmap


# BUILD 108: clip backgrounds
VIDEO_CLIP_BG = QColor(70, 90, 130)
AUDIO_CLIP_BG = QColor(80, 80, 80)

# Zoomed-out drawing: clips narrower than TINY_CLIP_W px are one solid bar in
# the outline color (adjacent ones merged), plus their markers. Clips
# narrower than CLIP_DETAIL_MIN_W px get no icons (not fetched or made), no
# names or caption text, no key graphs and no sequence clip icons.
TINY_CLIP_W = 3
CLIP_DETAIL_MIN_W = 12
CLIP_OUTLINE = QColor(160, 180, 220)
CLIP_OUTLINE_SELECTED = QColor(255, 255, 160)


class ClipDelegate(QStyledItemDelegate):
    # Waveform drawing lives in sequence_timeline_audio.py
    _draw_waveform = _draw_waveform

    # Paints track background and clips in the Clips column
    def __init__(self, timeline):
        super().__init__(timeline.table)
        self.timeline = timeline

    def paint(self, painter, option, index):
        if index.column() == COLLAPSE_COL:
            # BUILD 126: down triangle = open, right triangle = collapsed
            super().paint(painter, option, index)
            paint_collapse_cell(self.timeline, painter, option, index)   # BUILD 161
            return
        if index.column() != CLIPS_COL:
            super().paint(painter, option, index)
            return
        tl = self.timeline
        rect = option.rect
        # Only clips inside the area being repainted are drawn (a playhead
        # move repaints narrow strips); layout such as sticky names still
        # uses the whole cell
        vis = paint_area(painter, rect)
        if vis.isEmpty():
            return
        painter.save()
        painter.setClipRect(vis)
        name_item = tl.table.item(index.row(), 0)
        collapsed = bool(name_item.data(ROLE_COLLAPSED)) if name_item is not None else False
        track_id = name_item.data(Qt.UserRole) if name_item is not None else None
        track_type = (name_item.data(Qt.UserRole + 4) or "").lower() if name_item is not None else ""
        bg = name_item.data(Qt.UserRole + 3) if name_item is not None else ""
        # BUILD 52 stored #403050 on keyframe tracks; treat like caption/video default (none)
        if bg and track_type == "keyframe" and bg.lower() == "#403050":
            bg = ""
        if bg:
            color = QColor(bg)
            if color.isValid():
                painter.fillRect(rect, color)
        ppf = tl.pixels_per_frame
        markers = getattr(tl, "_markers", None)
        # BUILD 303: clip speed (clip_speed.py): label suffix, and the out
        # icon / waveform cover the clip's source span
        from clip_speed import speed_of, speed_label, source_span
        speeds = getattr(tl, "_clip_speeds", None)
        tiny = _TinyRun(painter, rect.top(), rect.height())
        for clip in visible_clips(tl, tl.clips_by_track.get(track_id, []), rect, vis):
            (clip_id, footage_id, start_frame, length_frames, filename,
             clip_enabled, clip_solo, in_frame) = clip[:8]
            # Clip rect is the time span only, whatever the icon sizes
            x = int(rect.left() + (start_frame - tl.view_start_frame) * ppf)
            w = max(1, int(length_frames * ppf))
            if x + w < vis.left() or x > vis.right():
                continue
            clip_rect = QRect(x, rect.top(), w, rect.height())
            if w < TINY_CLIP_W:
                tiny.add(x, w, clip_id in tl._selected_clip_ids, clip_enabled)
                if (markers and not collapsed and track_type not in ("caption", "keyframe")
                        and clip_id is not None and clip_id > 0 and markers.get(clip_id)):
                    tiny.flush()
                    from keyframe import split_title_rect
                    from markers import paint_markers
                    paint_markers(painter, split_title_rect(clip_rect)[1], clip, markers,
                                  ppf, getattr(tl, "_hover_marker_id", None),
                                  speed_of(speeds, clip_id))
                continue
            tiny.flush()
            detail = w >= CLIP_DETAIL_MIN_W
            # Disabled clips draw dimmer
            painter.setOpacity(1.0 if clip_enabled else 0.4)
            if collapsed:
                # BUILD 126: bar + outline + name only (no icons, waveform,
                # key boxes or markers)
                paint_collapsed_clip(painter, clip_rect, track_type,
                                     clip_id in tl._selected_clip_ids,
                                     ((filename or "")
                                      + speed_label(speed_of(speeds, clip_id))
                                      + (" [S]" if clip_solo else ""))
                                     if detail else "",
                                     visible_rect=rect)
                painter.setOpacity(1.0)
                continue
            if track_type == "keyframe":
                from keyframe import paint_keyframe_clip
                keys = clip[9] if len(clip) > 9 else []
                ktype = clip[10] if len(clip) > 10 else ""
                paint_keyframe_clip(
                    painter, clip_rect,
                    ((filename or "") + speed_label(speed_of(speeds, clip_id)))
                    if detail else "",
                    keys=keys if detail else None,
                    clip_start=start_frame, ppf=ppf, stored_type=ktype,
                    linked=(len(clip) > 8 and clip[8] is not None),
                    visible_rect=rect,
                    selected_key_id=getattr(tl, "_menu_key_id", None),
                    extra=clip[11] if len(clip) > 11 else None)
                if clip_id in tl._selected_clip_ids:
                    painter.setPen(QColor(255, 255, 160))
                else:
                    painter.setPen(QColor(160, 180, 220))
                painter.drawRect(clip_rect.adjusted(0, 0, -1, -1))
                painter.setOpacity(1.0)
                continue
            # BUILD 128: keyframe-style title bar (black, 1 px white border,
            # white name) on top; the content (thumbnails, waveform, caption
            # bar, SEQ icons, markers) is drawn in the rect below it
            from keyframe import split_title_rect, paint_title_bar
            full_rect = clip_rect
            title_rect, clip_rect = split_title_rect(full_rect)
            ctop, ch = clip_rect.top(), clip_rect.height()
            if track_type == "caption":
                paint_caption_bar(painter, clip_rect)
            else:
                # BUILD 108: audio clips gray; video / image keep (70, 90, 130)
                painter.fillRect(clip_rect, AUDIO_CLIP_BG if track_type == "audio"
                                 else VIDEO_CLIP_BG)
            text_rect = QRect(clip_rect)
            seq_ref = (getattr(tl, "_seq_refs", None) or {}).get(clip_id)
            src_len = source_span(length_frames, speed_of(speeds, clip_id))
            if track_type == "caption":
                # Caption: plain bar, no icon, no waveform. BUILD 236: with a
                # title bar the text is drawn in the pane (word-wrapped);
                # without one (short row) the old one-line name below
                if title_rect is not None and detail:
                    from sequence_timeline_captions import paint_caption_text
                    paint_caption_text(painter, clip_rect, filename or "", rect)
            elif seq_ref is not None:
                # BUILD 105: sequence clip, left / right sequence icons and
                # the child name (no footage icon, waveform or peaks)
                # BUILD 108: track colors (video blue / audio gray)
                painter.fillRect(clip_rect, AUDIO_CLIP_BG if track_type == "audio"
                                 else VIDEO_CLIP_BG)
                ih = ch
                lpix = sequence_clip_icon(ih, False)
                rpix = sequence_clip_icon(ih, True)
                iw = lpix.width() if (lpix is not None and detail) else 0
                if iw and 2 * iw <= w:
                    painter.drawPixmap(x, ctop, lpix)
                    painter.drawPixmap(x + w - iw, ctop, rpix)
                    text_rect.setLeft(x + iw)
                    text_rect.setRight(x + w - iw - 1)
                elif iw:
                    painter.drawPixmap(x, ctop, lpix, 0, 0, min(iw, w), ih)
                    text_rect.setLeft(x + min(iw, w))
            elif track_type == "audio":
                # Audio clips: waveform from stored peaks, no icons
                self._draw_waveform(painter, tl, clip_rect, footage_id,
                                    in_frame, src_len,
                                    (getattr(tl, "_clip_gains", None) or {}).get(clip_id, 0.0))
            else:
                # Video clips show the source frame at their left edge
                # (frame rate conformed); stills use 0
                icon_frame = (icon_source_frame(tl, footage_id, in_frame)
                              if track_type == "video" else 0)
                # Paint reads stored icons only; a missing one is asked of
                # the icon queue (sequence_timeline_iconq), which makes it
                # on a timer and repaints the clip. No requests mid-drag.
                # Narrow clips (under CLIP_DETAIL_MIN_W) neither fetch nor
                # ask for icons.
                from sequence_timeline_iconq import request_icon, icon_failed
                queue_ok = tl._edge_drag is None
                pix = None
                if detail:
                    pix = tl.icon_pixmap(footage_id, ch, icon_frame,
                                         generate=False, clip_id=clip_id)
                    if (pix is None or pix.isNull()) and queue_ok:
                        request_icon(tl, footage_id, icon_frame, ch, clip, track_id)
                has_in = pix is not None and not pix.isNull()
                in_full_w = pix.width() if has_in else 0
                # Out icon: video, only when in and out icons both fit (the
                # out icon is the same footage at the same height, so the
                # same width as the in icon); stills reuse the in icon
                out_failed = False
                if track_type == "video":
                    out_pix = None
                    if detail and 2 * (in_full_w if has_in else ch) <= w:
                        out_pix = video_out_pixmap(tl, footage_id, ch,
                                                   in_frame, src_len, clip_id)
                        if out_pix is None or out_pix.isNull():
                            out_src = icon_source_frame(
                                tl, footage_id, out_frame_for(in_frame, src_len))
                            out_failed = icon_failed(tl, footage_id, out_src, ch)
                            if queue_ok and not out_failed:
                                request_icon(tl, footage_id, out_src, ch, clip, track_id)
                else:
                    out_pix = pix
                has_out = out_pix is not None and not out_pix.isNull()
                if has_out:
                    out_full_w = out_pix.width()
                else:
                    out_full_w = in_full_w if has_in else ch
                # Room for icons: the whole clip; the edge handles overlap the
                # icons on both sides
                avail = w
                if in_full_w + out_full_w <= avail:
                    in_w, out_w = in_full_w, out_full_w
                else:
                    # No room for both: skip the out icon entirely (no black
                    # placeholder); the in icon shows its left part
                    out_w = 0
                    in_w = min(in_full_w, avail)
                if has_in and in_w > 0:
                    painter.drawPixmap(x, ctop, pix, 0, 0, in_w, pix.height())
                    text_rect.setLeft(x + in_w)
                if out_w > 0:
                    # Flush right: icon's right edge on the clip's right edge
                    out_x = x + w - out_w
                    if has_out:
                        painter.drawPixmap(out_x, ctop, out_pix,
                                           out_pix.width() - out_w, 0, out_w, out_pix.height())
                    elif out_failed:
                        # Black only for a real missing frame: the icon queue
                        # could not make it (for example past the footage
                        # end); while it is queued the slot shows the clip
                        painter.fillRect(QRect(out_x, ctop, out_w, ch),
                                         QColor(0, 0, 0))
                    text_rect.setRight(out_x - 1)
            # Markers: square at the top of the content (just below the title
            # bar) plus a line (video / image / audio only); hit-testing is
            # by x only, so it still lines up
            from markers import paint_markers
            if track_type != "caption":
                paint_markers(painter, clip_rect, clip, markers, ppf,
                              getattr(tl, "_hover_marker_id", None),
                              speed_of(speeds, clip_id))
            # Outline: drawn after the title bar (below)
            label = ""
            if detail:
                label = filename or ""
                if clip_solo:
                    label += " [S]"
                # BUILD 165: non-zero clip gain after the name
                gain_db = (getattr(tl, "_clip_gains", None) or {}).get(clip_id, 0.0)
                if gain_db:
                    from clip_gain import gain_label
                    label += gain_label(gain_db)
                # BUILD 303: speed != 100 after the name / gain (" 200%")
                label += speed_label(speed_of(speeds, clip_id))
            if title_rect is not None:
                # BUILD 128: the name lives in the title bar (sticky left,
                # elided); no in-content name. BUILD 236: caption title =
                # "Caption | For Clip | In - Out" (+ [S]), text in the pane
                if track_type == "caption" and detail:
                    from sequence_timeline_captions import caption_title
                    label = caption_title(tl, clip, track_id) + speed_label(
                        speed_of(speeds, clip_id)) + (" [S]" if clip_solo else "")
                paint_title_bar(painter, title_rect, label, rect)
            elif clip_rect.width() > 0 and detail:
                # Row too short for a bar: BUILD 127 in-content name
                draw_clip_name(painter, text_rect, rect, label,
                               caption=(track_type == "caption"),
                               backing=(track_type == "audio" and seq_ref is None),
                               fallback_rect=clip_rect)
            # Outline last, around the whole clip including the title bar
            # (selected: light yellow)
            if clip_id in tl._selected_clip_ids:
                painter.setPen(QColor(255, 255, 160))
            else:
                painter.setPen(QColor(160, 180, 220))
            painter.drawRect(full_rect.adjusted(0, 0, -1, -1))
            painter.setOpacity(1.0)
        tiny.flush()
        painter.restore()


def _clip_start(clip):
    return clip[2]


def visible_clips(tl, clips, rect, vis):
    # The part of a track's clip list (sorted by start, as refresh leaves
    # it) that can reach the repainted area vis, found by bisect so paint
    # cost follows the visible clips. A margin of a few pixels covers the
    # 1 px minimum clip width; the caller still culls each clip. During a
    # drag (lists edited in place, maybe unsorted) the whole list is used.
    ppf = tl.pixels_per_frame
    if len(clips) < 64 or ppf <= 0 or tl._edge_drag is not None:
        return clips
    margin = 4.0 / ppf + 1
    f0 = tl.view_start_frame + (vis.left() - rect.left()) / ppf - margin
    f1 = tl.view_start_frame + (vis.right() - rect.left()) / ppf + margin
    longest = max(c[3] for c in clips)
    try:
        lo = bisect_left(clips, f0 - longest, key=_clip_start)
        hi = bisect_right(clips, f1, key=_clip_start)
    except TypeError:
        return clips      # Python before 3.10: no bisect key
    return clips[lo:hi]


class _TinyRun:
    # Clips under TINY_CLIP_W px: one solid bar in the outline color (what
    # the outline alone covered at that width); adjacent bars with the same
    # selection and enabled state are merged into one fillRect
    def __init__(self, painter, top, height):
        self.painter = painter
        self.top = top
        self.height = height
        self.key = None
        self.left = self.right = 0

    def add(self, x, w, selected, enabled):
        key = (bool(selected), bool(enabled))
        if key == self.key and x <= self.right + 1:
            self.right = max(self.right, x + w - 1)
            return
        self.flush()
        self.key, self.left, self.right = key, x, x + w - 1

    def flush(self):
        if self.key is None:
            return
        selected, enabled = self.key
        self.painter.setOpacity(1.0 if enabled else 0.4)
        self.painter.fillRect(QRect(self.left, self.top, self.right - self.left + 1,
                                    self.height),
                              CLIP_OUTLINE_SELECTED if selected else CLIP_OUTLINE)
        self.painter.setOpacity(1.0)
        self.key = None


def paint_area(painter, cell):
    # Part of cell that this paint redraws: the painter clip if one is set,
    # else the widget update region (the paint engine system clip, mapped
    # from device to logical coordinates, 1 px margin for rounding). The
    # whole cell when neither is known (painting to an image).
    area = None
    if painter.hasClipping():
        area = painter.clipBoundingRect().toAlignedRect()
    else:
        engine = painter.paintEngine()
        region = engine.systemClip() if engine is not None else None
        if region is not None and not region.isEmpty():
            inverse, ok = painter.deviceTransform().inverted()
            if ok:
                area = inverse.mapRect(region.boundingRect()).adjusted(-1, -1, 1, 1)
    if area is None:
        return QRect(cell)
    return cell.intersected(area)


_SEQ_ICON_CACHE = {}


COLLAPSED_BAR_COLORS = {"audio": AUDIO_CLIP_BG, "caption": QColor(30, 110, 110),
                        "keyframe": QColor(30, 110, 110)}


NAME_MIN_PX = 24   # narrower than this between the icons: draw over them


def draw_clip_name(painter, text_rect, visible_rect, label, caption=False,
                   backing=False, pad=4, fallback_rect=None):
    # BUILD 127: clip name, left-aligned with padding, sticky to the
    # visible left edge of the Clips area, elided at the clip / visible
    # right edge; vertically centered in text_rect as before. Same font and
    # color (230, 230, 230). backing: subtle dark box behind the text.
    # fallback_rect (the whole clip): used, with the backing, when the
    # visible space between the in / out icons is under NAME_MIN_PX (for
    # example only the out icon is on screen), so the name stays readable
    from keyframe import sticky_name_rect
    r = sticky_name_rect(text_rect, visible_rect, pad)
    if fallback_rect is not None and (r is None or r.width() < NAME_MIN_PX):
        r = sticky_name_rect(fallback_rect, visible_rect, pad)
        backing = True
    if r is None or r.width() <= 2:
        return
    text = " ".join(label.split()) if caption else label
    fm = painter.fontMetrics()
    text = fm.elidedText(text, Qt.ElideRight, r.width())
    if not text:
        return
    if backing:
        tw = min(r.width(), fm.horizontalAdvance(text))
        th = min(r.height(), fm.height())
        painter.fillRect(QRect(r.left() - 2, r.center().y() - th // 2 - 1, tw + 4, th + 2),
                         QColor(0, 0, 0, 150))
    painter.setPen(QColor(230, 230, 230))
    painter.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, text)


def paint_collapsed_clip(painter, clip_rect, track_type, selected, label,
                         visible_rect=None):
    # BUILD 126: collapsed track clip: bar, outline, name in a font that
    # fits the 16 px row, and nothing else. BUILD 127: name left-aligned
    # and sticky like open clips (draw_clip_name).
    from PySide6.QtGui import QFont
    painter.fillRect(clip_rect, COLLAPSED_BAR_COLORS.get(track_type, VIDEO_CLIP_BG))
    painter.setPen(QColor(255, 255, 160) if selected else QColor(160, 180, 220))
    painter.drawRect(clip_rect.adjusted(0, 0, -1, -1))
    if not label:
        return
    font = QFont(painter.font())
    font.setPixelSize(max(7, min(11, clip_rect.height() - 4)))
    painter.save()
    painter.setFont(font)
    draw_clip_name(painter, clip_rect, visible_rect, label, caption=True, pad=3)
    painter.restore()


def moving_marker_frames(markers, group, drag_clip, speeds=None):
    # BUILD 110: timeline frames, at delta 0, of the visible markers on the
    # clips of a move drag (group entries {"clip", "start", ...}; the
    # dragged clip itself if the group lacks it). Positive clip ids only
    # (video / image / audio), like marker drawing. speeds: clip speed map
    # (clip_speed.load_clip_speeds).
    from markers import visible_offset
    out = []
    if not markers:
        return out
    members = [(m["clip"], int(m.get("start", m["clip"][2]))) for m in (group or [])]
    if drag_clip is not None and all(c[0] != drag_clip[0] for c, _s in members):
        members.append((drag_clip, int(drag_clip[2])))
    for c, start0 in members:
        try:
            if int(c[0]) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        for _mid, mframe, _rgb, _name in markers.get(c[0], ()):
            off = visible_offset(c, mframe, (speeds or {}).get(c[0], 100))
            if off is not None:
                f = start0 + int(off)
                if f not in out:
                    out.append(f)
    return out


def sequence_clip_icon(height, right):
    # BUILD 105: square in / out icon for sequence clips, drawn once per
    # height: a stack of three frames with 'SEQ'; the right icon is the
    # mirror image (like the video in / out icons at the clip edges)
    height = int(height)
    if height <= 0:
        return None
    key = (height, bool(right))
    if key in _SEQ_ICON_CACHE:
        return _SEQ_ICON_CACHE[key]
    from PySide6.QtGui import QPixmap, QPainter, QFont, QTransform
    pix = QPixmap(height, height)
    pix.fill(QColor(40, 28, 70))
    p = QPainter(pix)
    try:
        p.setRenderHint(QPainter.Antialiasing)
        m = max(2, height // 8)
        step = max(2, height // 10)
        box = height - 2 * m - 2 * step
        for i in range(3):
            off = m + (2 - i) * step
            r = QRect(off, m + i * step, max(2, box), max(2, box))
            p.fillRect(r, QColor(120, 95, 180) if i < 2 else QColor(170, 140, 230))
            p.setPen(QColor(20, 12, 40))
            p.drawRect(r)
    finally:
        p.end()
    if right:
        # Mirror the frame stack only; the label stays readable
        pix = pix.transformed(QTransform().scale(-1, 1))
    p = QPainter(pix)
    try:
        f = QFont()
        f.setPixelSize(max(6, box // 3))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(30, 18, 55))
        lx = (height - m - box) if right else m
        p.drawText(QRect(lx, m + 2 * step, max(2, box), max(2, box)),
                   Qt.AlignCenter, "SEQ")
    finally:
        p.end()
    _SEQ_ICON_CACHE[key] = pix
    return pix


def _linked_clips(self, clip):
    # Other clips sharing clip's link_id (any track), as live lists.
    # Footage, caption, and keyframe clips all participate.
    link_id = clip[8] if len(clip) > 8 else None
    if link_id is None:
        return []
    return [c for clips in self.clips_by_track.values() for c in clips
            if c[0] != clip[0] and len(c) > 8 and c[8] == link_id]


def _clip_edge_at(self, pos):
    # Return (clip, "left" or "right") if pos is within EDGE_HIT_PX of an
    # image or video clip edge, else None
    row = self.table.rowAt(pos.y())
    if row < 0:
        return None
    name_item = self.table.item(row, 0)
    if name_item is None:
        return None
    if (name_item.data(Qt.UserRole + 4) or "").lower() not in EDIT_TRACK_TYPES:
        return None
    x_in_clips = self._x_in_clips(pos)
    if x_in_clips < -EDGE_HIT_PX:
        return None
    ppf = self.pixels_per_frame
    best = None
    best_dist = EDGE_HIT_PX + 1
    for clip in self.clips_by_track.get(name_item.data(Qt.UserRole), []):
        left = int((clip[2] - self.view_start_frame) * ppf)
        width = max(1, int(clip[3] * ppf))
        # Last painted pixel column of the clip (same rect the delegate draws)
        right = left + width - 1
        for edge, edge_x in (("left", left), ("right", right)):
            dist = abs(x_in_clips - edge_x)
            # <= so the right edge wins a tie on very narrow clips
            if dist <= EDGE_HIT_PX and (dist < best_dist or
                                        (edge == "right" and dist == best_dist)):
                best = (clip, edge)
                best_dist = dist
    return best


def _clip_by_id(self, clip_id):
    for clips in self.clips_by_track.values():
        for c in clips:
            if c[0] == clip_id:
                return c
    return None


def _clear_selection(self):
    if self._selected_clip_ids or self._selection_solo:
        self._selected_clip_ids = set()
        self._selection_order = []
        self._selection_solo = False
        self.table.viewport().update()
    self._update_edit_actions()


def _select_clip(self, clip, mode):
    # mode: "normal" = clip + link partners, "solo" = only clip with
    # selection solo on, "add" = add clip and its link partners (keep
    # existing selection; drop nothing)
    if mode == "add":
        if self._selection_solo:
            self._selection_solo = False
        members = [clip] + self._linked_clips(clip)
        for m in members:
            if m[0] not in self._selected_clip_ids:
                self._selected_clip_ids.add(m[0])
                self._selection_order.append(m[0])
    elif mode == "solo":
        self._selected_clip_ids = {clip[0]}
        self._selection_order = [clip[0]]
        self._selection_solo = True
    else:
        members = [clip] + self._linked_clips(clip)
        self._selection_order = [c[0] for c in members]
        self._selected_clip_ids = set(self._selection_order)
        self._selection_solo = False
    self.table.viewport().update()
    self._update_edit_actions()


def _selected_clips(self):
    # Live clip lists of the selection, in selection order
    out = []
    for cid in self._selection_order:
        c = self._clip_by_id(cid)
        if c is not None:
            out.append(c)
    return out


def _drag_group(self, clip, edge="move"):
    # Clips that take the drag delta, each with its drag-start values
    # and its own-track neighbor bounds (group members ignored).
    # Solo: only clip. Move of a selected clip: every selected clip plus
    # their link partners. Otherwise: clip plus its link partners.
    if self._selection_solo:
        members = [clip]
    elif edge == "move" and clip[0] in self._selected_clip_ids:
        members = [clip]
        for c in self._selected_clips() + [clip]:
            for m in [c] + self._linked_clips(c):
                if all(m[0] != x[0] for x in members):
                    members.append(m)
    else:
        members = [clip] + self._linked_clips(clip)
    ids = {c[0] for c in members}
    group = []
    from keyframe import is_keyframe_clip_id
    for c in members:
        prev_end, next_start = self._neighbor_bounds(c, c[2], c[3], ignore_ids=ids)
        # BUILD 134: every non-moving clip on this member's track, for
        # the jump-past-a-blocker move (clip_drag.py)
        blocks = [(o[2], o[2] + o[3])
                  for o in self.clips_by_track.get(self._clip_track_id(c), [])
                  if o[0] not in ids]
        entry = {"clip": c, "start": c[2], "length": c[3], "in": c[7],
                 "prev_end": prev_end, "next_start": next_start,
                 "blocks": blocks,
                 "free_in": _no_source_floor(self, c),
                 # BUILD 303: clip speed percent (clip_speed.py)
                 "speed": (getattr(self, "_clip_speeds", None) or {}).get(c[0], 100)}
        if is_keyframe_clip_id(c[0]) and len(c) > 9:
            entry["keys"] = list(c[9])
        group.append(entry)
    if edge == "right":
        _add_source_caps(self, group)
    return group


def _add_source_caps(self, group):
    # BUILD 301: for a right trim, each video / audio footage clip gets
    # "src_end", the cap on its source out-point (in + length, sequence
    # frames): footage.duration rounded up to a whole second
    # (sequence_timeline_sql.source_cap_frames). Image, caption, keyframe
    # and sequence clips, and footage without a duration, get none.
    from sequence_timeline_sql import footage_cap_map
    seq_refs = getattr(self, "_seq_refs", None) or {}
    want = [m for m in group
            if m["clip"][1] is not None and not m["free_in"]
            and m["clip"][0] not in seq_refs
            and self._track_type(self._clip_track_id(m["clip"])) in ("video", "audio")]
    if not want:
        return
    try:
        conn = sqlite3.connect(self._project_db())
        try:
            caps = footage_cap_map(conn.cursor(), [m["clip"][1] for m in want],
                                   getattr(self, "fps", None))
        finally:
            conn.close()
    except (sqlite3.Error, TypeError):
        return
    for m in want:
        cap = caps.get(int(m["clip"][1]))
        if cap is not None:
            m["src_end"] = cap


def _no_source_floor(self, clip):
    # True for keyframe, caption and image (still) clips: they have no
    # source media to run out of, so a left trim may take their in-point
    # below source 0. Video and audio clips, and sequence clips on any
    # track (their in-point is a frame of the child sequence), stop at 0.
    from keyframe import is_keyframe_clip_id
    if is_keyframe_clip_id(clip[0]) or is_caption_id(clip[0]):
        return True
    if clip[0] in (getattr(self, "_seq_refs", None) or {}):
        return False
    return self._track_type(self._clip_track_id(clip)) == "image"


# BUILD 238: edge auto-scroll while a clip / selection / link group is
# moved or (BUILD 300) edge-trimmed, left or right edge, with the scrub's
# rule and margin
# (sequence_timeline_ruler.scrub_edge, SCRUB_EDGE_PX, SCRUB_TICK_MS timer):
# in a margin the view pans by the mouse travel into it plus a timer tick
# while held there, stopping at frame 0 / the end. The tick re-runs the
# move or trim at the last mouse position, so the clips (or the trimmed
# edge, within the usual trim clamps) follow the frame under the cursor as
# the view scrolls. Stops when the mouse leaves the margin or on
# release (the panned view is saved then).
def _move_pan(self, drag, pos, tick=False):
    # Pans the view if pos is in a margin; True while it is in one
    from sequence_timeline_ruler import scrub_edge, SCRUB_EDGE_PX
    left = self.table.columnViewportPosition(CLIPS_COL)
    width = max(1, min(self.table.columnWidth(CLIPS_COL),
                       self.table.viewport().width() - left))
    xc = pos.x() - left
    last = xc if tick else drag.get("pan_x")
    # BUILD 300: the scroll limit is the sequence end or the dragged group's
    # live end, whichever is later, so a clip moved or trimmed past the end
    # keeps the view scrolling (the end's right pad stays ahead of it)
    total = self.total_frames()
    for m in drag.get("group") or []:
        c = m["clip"]
        total = max(total, int(c[2]) + int(c[3]))
    view, _frame, rem = scrub_edge(
        xc, last, width, self.view_start_frame, self.pixels_per_frame,
        total, self.visible_frames(), drag.get("pan_rem", 0.0), tick)
    drag["pan_x"] = xc
    drag["pan_rem"] = rem
    if view != self.view_start_frame:
        self.view_start_frame = view
        drag["panned"] = True
        self.ruler.viewport().update()
        overlay = getattr(self, "playhead_overlay", None)
        if overlay is not None:
            overlay.update()
    m = max(0, min(SCRUB_EDGE_PX, (width - 1) // 3))
    return m > 0 and (xc < m or xc > width - m)


def _move_pan_timer(self):
    t = getattr(self, "_move_pan_timer_obj", None)
    if t is None:
        from PySide6.QtCore import QTimer
        from sequence_timeline_ruler import SCRUB_TICK_MS
        t = QTimer(self)
        t.setInterval(SCRUB_TICK_MS)
        t.timeout.connect(lambda: _move_pan_tick(self))
        self._move_pan_timer_obj = t
    return t


def _move_pan_tick(self):
    drag = getattr(self, "_edge_drag", None)
    if drag is None or drag.get("pan_pos") is None:
        _move_pan_timer(self).stop()
        return
    self._edge_drag_move(drag["pan_pos"], tick=True)


def _move_pan_stop(self, drag):
    # Release: no more panning; keep the panned view
    t = getattr(self, "_move_pan_timer_obj", None)
    if t is not None:
        t.stop()
    if drag is not None and drag.get("panned"):
        try:
            self._update_status()
            self._save_view_state()
        except Exception:
            pass


def _edge_drag_move(self, pos, tick=False):
    # The dragged clip asks for a frame delta; every clip in its link
    # group takes the same delta, clamped so no member leaves its own
    # neighbors' gap (each on its own track). tick: auto-scroll timer
    drag = self._edge_drag
    clip = drag["clip"]
    group = drag.get("group") or self._drag_group(clip)
    drag["group"] = group
    edge = drag["edge"]
    # BUILD 238 (moves) / BUILD 300 (trims): pan at the edges first, then
    # use the frame under the cursor in the (possibly scrolled) view
    drag["pan_pos"] = pos
    timer = _move_pan_timer(self)
    if _move_pan(self, drag, pos, tick):
        if not timer.isActive():
            timer.start()
    elif timer.isActive():
        timer.stop()
    frame = self._frame_at_clips_x(self._x_in_clips(pos))
    if edge == "move":
        target = self._move_target_track(pos, drag)
        if target is not None:
            # Track move only: start_frame stays at drag-start; mouse x
            # does not change time. Release, then drag again, for time.
            from sequence_timeline_insert import set_insert_frame
            set_insert_frame(self, None)
            self._cross_track_preview(drag, target)
            return
        if drag.get("target_track") is not None:
            # Back on the source track: return the group to its own rows
            # and restore the drag-start start_frames before time move
            _restore_cross_track(self, drag)
            drag["target_track"] = None
            drag["target_ok"] = True
            drag["cur_delta"] = 0
        delta = (frame - drag["grab_offset"]) - drag["orig_start"]
    elif edge == "left":
        delta = frame - drag["orig_start"]
    else:
        delta = frame - (drag["orig_start"] + drag["orig_length"])
    delta = self._snap_drag_delta(drag, group, pos, delta)
    from sequence_timeline_insert import insert_held
    if edge == "move" and insert_held():
        # Shift held: insert drag (sequence_timeline_insert.py). The group
        # may pass over other clips; only frame 0 stops it.
        delta = max(delta, -min(m["start"] for m in group))
        drag["cur_delta"] = delta
    elif edge == "move" and all("blocks" in m for m in group):
        # BUILD 134: waits at a blocking clip's edge, jumps past it
        # once the mouse is past it and the whole group fits there
        from clip_drag import legal_components, move_delta
        comps = drag.get("legal")
        if comps is None:
            comps = legal_components(
                [(m["start"], m["length"], m["blocks"]) for m in group])
            drag["legal"] = comps
        delta = move_delta(comps, drag.get("cur_delta", 0), delta,
                           drag["grab_offset"], drag["orig_length"])
        drag["cur_delta"] = delta
    else:
        lo, hi = self._group_delta_range(group, edge)
        delta = max(lo, min(hi, delta)) if lo <= hi else 0
    for m in group:
        c = m["clip"]
        if edge == "move":
            c[2] = m["start"] + delta
            if "keys" in m:
                c[9] = [(item[0], item[1] + delta, item[2])
                        for item in m["keys"]]
        elif edge == "left":
            # Start trim: keyframe keys keep their sequence frames (the
            # edge reveals or hides them); only a move shifts them
            # BUILD 303: the in-point moves by the source frames delta
            # timeline frames cover at the clip's speed
            from clip_speed import source_offset
            c[2] = m["start"] + delta
            c[3] = m["length"] - delta
            c[7] = m["in"] + source_offset(delta, m.get("speed", 100))
        else:
            c[3] = m["length"] + delta
    if edge == "move":
        # Shift held: white cut line at the insert frame
        from sequence_timeline_insert import insert_watch, set_insert_frame
        active = insert_held()
        set_insert_frame(self, min(m["clip"][2] for m in group) if active else None)
        insert_watch(self, pos, active)
    self.table.viewport().update()
    self.status_bar.showMessage(f"Clip start {clip[2]}, length {clip[3]}")


def _snap_pixels(self):
    # Global setting 'Pixel Snap-To' (pixels, default 2), read per drag
    try:
        return max(0, int(_get_global_setting("Pixel Snap-To", "2") or "2"))
    except (TypeError, ValueError):
        return 2


def _snap_drag_delta(self, drag, group, pos, delta):
    # Move: the dragged clip's in or out point; trim: the dragged edge.
    # Snaps to the nearest in/out point of any clip on any track that is
    # not being moved or trimmed (linked partners left in place count),
    # within snap_px pixels. Distance uses the unrounded mouse position,
    # so it is a real pixel distance at the current zoom. The group
    # moves by the one returned delta; the clamp after it still allows
    # butting up to a neighbor.
    snap_px = drag.get("snap_px", 0)
    ppf = float(self.pixels_per_frame or 0)
    if snap_px <= 0 or ppf <= 0:
        self._set_snap_frame(None)
        return delta
    try:
        from clip_snap import snap_delta_target, snap_targets
        x = self._x_in_clips(pos)
        frame_f = max(0.0, self.view_start_frame + x / ppf)
        start, length = drag["orig_start"], drag["orig_length"]
        edge = drag["edge"]
        if edge == "move":
            raw = (frame_f - drag["grab_offset"]) - start
            edges = [start, start + length]
            # BUILD 110: visible markers of every clip moving together
            # are snap sources too, at their drag-start (delta 0)
            # frames. Listed after the edges so an edge wins a tie.
            edges += moving_marker_frames(
                getattr(self, "_markers", None), group, drag["clip"],
                getattr(self, "_clip_speeds", None))
        elif edge == "left":
            raw = frame_f - start
            edges = [start]
        else:
            raw = frame_f - (start + length)
            edges = [start + length]
        ids = {m["clip"][0] for m in group}
        ids.add(drag["clip"][0])
        from markers import marker_frames
        targets = sorted(set(snap_targets(self.clips_by_track, ids)) | set(
            marker_frames(getattr(self, "_markers", None), self.clips_by_track, ids,
                          getattr(self, "_clip_speeds", None))))
        snapped, target = snap_delta_target(
            raw, edges, targets, ppf, snap_px)
    except Exception as e:
        # print(f"Snap failed: {e}")
        self._set_snap_frame(None)
        return delta
    self._set_snap_frame(target)
    return delta if snapped is None else snapped


def _set_snap_frame(self, frame):
    # White snap line frame (None = hidden); repaint ruler and tracks
    # only when it changes
    if getattr(self, "_snap_frame", None) == frame:
        return
    self._snap_frame = frame
    try:
        self.ruler.viewport().update()
        self.playhead_overlay.update()
    except Exception:
        pass


def _group_delta_range(self, group, edge):
    # Allowed (lo, hi) frame delta for all members at once
    lo, hi = -10 ** 9, 10 ** 9
    for m in group:
        start, length, in_f = m["start"], m["length"], m["in"]
        prev_end, next_start = m["prev_end"], m["next_start"]
        if edge == "move":
            # start + d >= max(0, prev_end); end + d <= next_start
            lo = max(lo, max(0, prev_end) - start)
            if next_start is not None:
                hi = min(hi, next_start - (start + length))
        elif edge == "left":
            # start + d >= prev_end and >= 0, length - d >= 1, and for
            # clips with source media in + d >= 0. Keyframe, caption and
            # image clips (free_in) have no source floor, so a linked
            # stack is held only by its video / audio members' in-points.
            # BUILD 303: at speed s the in-point moves by floor(d * s /
            # 100), so in + that >= 0 is d >= -floor(in * 100 / s).
            lo = max(lo, max(0, prev_end) - start)
            speed = m.get("speed", 100)
            if not m.get("free_in"):
                lo = max(lo, -in_f if speed == 100 else -((in_f * 100) // speed))
            hi = min(hi, length - 1)
        else:
            # length + d >= 1; end + d <= next_start; BUILD 301: in +
            # length + d <= src_end (footage clips, _add_source_caps), so
            # the group stops at the tightest cap. A clip already past its
            # cap may not grow but is not cut back. BUILD 303: at speed s
            # the source used is clip_speed.source_span(length, s), so the
            # longest length is max_length_for_source(src_end - in, s).
            lo = max(lo, 1 - length)
            if next_start is not None:
                hi = min(hi, next_start - (start + length))
            speed = m.get("speed", 100)
            if m.get("src_end") and speed == 100:
                hi = min(hi, max(0, m["src_end"] - (in_f + length)))
            elif m.get("src_end"):
                from clip_speed import max_length_for_source
                hi = min(hi, max(0, max_length_for_source(
                    m["src_end"] - in_f, speed) - length))
    return lo, hi


def _neighbor_bounds(self, clip, start, length, ignore_ids=None):
    # (prev_end, next_start) for clip's track: end of the nearest clip
    # before start (0 if none) and start of the nearest clip at or after
    # start + length (None if none). The clip itself is ignored.
    prev_end = 0
    next_start = None
    end = start + length
    for c in self.clips_by_track.get(self._clip_track_id(clip), []):
        if c[0] == clip[0] or (ignore_ids and c[0] in ignore_ids):
            continue
        c_start, c_end = c[2], c[2] + c[3]
        if c_start < start:
            prev_end = max(prev_end, c_end)
        elif c_start >= end or c_start >= start:
            next_start = c_start if next_start is None else min(next_start, c_start)
    return prev_end, next_start


def _clip_image_track_row(self, pos):
    row = self.table.rowAt(pos.y())
    if row < 0:
        return None
    name_item = self.table.item(row, 0)
    if name_item is None:
        return None
    if (name_item.data(Qt.UserRole + 4) or "").lower() not in EDIT_TRACK_TYPES:
        return None
    return name_item


def _clip_at(self, pos):
    # Image or video clip whose body (not an edge) is under pos, else None
    name_item = self._clip_image_track_row(pos)
    if name_item is None:
        return None
    x_in_clips = self._x_in_clips(pos)
    if x_in_clips < 0 or self._clip_edge_at(pos) is not None:
        return None
    ppf = self.pixels_per_frame
    for clip in reversed(self.clips_by_track.get(name_item.data(Qt.UserRole), [])):
        left = (clip[2] - self.view_start_frame) * ppf
        right = left + clip[3] * ppf
        if left <= x_in_clips <= right:
            return clip
    return None


def _move_target_track(self, pos, drag):
    # Track under the mouse if it differs from the source track and has
    # the same type (video->video, audio->audio, image->image), else None
    src = drag.get("src_track")
    if src is None:
        return None
    row = self.table.rowAt(pos.y())
    if row < 0:
        return None
    name_item = self.table.item(row, 0)
    if name_item is None:
        return None
    tid = name_item.data(Qt.UserRole)
    if tid is None or tid == src:
        return None
    if (name_item.data(Qt.UserRole + 4) or "").lower() != self._track_type(src):
        return None
    return tid


def _relocate_clip(self, clip, track_id):
    # Move the in-memory clip list to another track's row (preview only)
    for clips in self.clips_by_track.values():
        for i, c in enumerate(clips):
            if c is clip:
                del clips[i]
                break
    self.clips_by_track.setdefault(track_id, []).append(clip)
    self._caption_partner_cache = None


# Track moves keep time and move the whole drag group (a linked stack, or
# the selection): the dragged clip goes to the track under the mouse.
# BUILD 259: pack move first (_pack_plan): every member, captions and
# keyframes included, moves by the same number of rows in the track list,
# so a stack on V1, C1, A1, A2 lands on the next V2, C2, A3, A4 laid out
# the same way. It is used when every destination row has its member's
# track type and is free over the member's frames. Otherwise the type
# move: every other video / audio / image member moves the same number of
# tracks of its own type in the same direction, keyframe and caption
# members stay on their tracks, and the move is refused when a member has
# no such track or would overlap a clip that is not moving. Mouse x is
# ignored.

CROSS_TRACK_TYPES = ("video", "audio", "image")


def _tracks_of_type(self, ttype):
    # Track ids of one type, top to bottom
    out = []
    for r in range(self.table.rowCount()):
        item = self.table.item(r, 0)
        if item is not None and (item.data(Qt.UserRole + 4) or "").lower() == ttype:
            out.append(item.data(Qt.UserRole))
    return out


def _pack_plan(self, drag, target, src_tracks):
    # BUILD 259: {clip id: track id} moving every member by the row offset
    # from the dragged clip's track to target, or None when a destination
    # row is missing, has another track type, or holds a clip that is not
    # moving over that member's drag-start frames
    rows, kinds = {}, []
    for r in range(self.table.rowCount()):
        item = self.table.item(r, 0)
        tid = None if item is None else item.data(Qt.UserRole)
        rows[tid] = r
        kinds.append((tid, "" if item is None else
                      (item.data(Qt.UserRole + 4) or "").lower()))
    src = drag["src_track"]
    if src not in rows or target not in rows:
        return None
    step = rows[target] - rows[src]
    group = drag.get("group") or [{"clip": drag["clip"]}]
    ids = set(m["clip"][0] for m in group)
    plan = {}
    for m in group:
        c = m["clip"]
        r = rows.get(src_tracks.get(c[0]))
        if r is None or not (0 <= r + step < len(kinds)):
            return None
        tid, kind = kinds[r + step]
        if tid is None or kind != kinds[r][1]:
            return None
        start = m.get("start", c[2])
        end = start + c[3]
        for o in self.clips_by_track.get(tid, []):
            if o[0] not in ids and o[2] < end and o[2] + o[3] > start:
                return None
        plan[c[0]] = tid
    return plan


def _cross_track_plan(self, drag, target):
    # {clip id: target track id} for every member that changes track, or
    # None when a member has nowhere to go
    clip = drag["clip"]
    src = drag["src_track"]
    src_tracks = drag.setdefault(
        "src_tracks", {m["clip"][0]: self._clip_track_id(m["clip"])
                       for m in drag.get("group") or [{"clip": clip}]})
    plan = _pack_plan(self, drag, target, src_tracks)
    if plan is not None:
        return plan
    order = _tracks_of_type(self, self._track_type(src))
    if src not in order or target not in order:
        return None
    steps = order.index(target) - order.index(src)
    plan = {}
    for m in drag.get("group") or [{"clip": clip}]:
        c = m["clip"]
        if c is clip:
            plan[c[0]] = target
            continue
        own = src_tracks.get(c[0])
        ttype = self._track_type(own)
        if ttype not in CROSS_TRACK_TYPES:
            continue
        tracks = _tracks_of_type(self, ttype)
        if own not in tracks:
            return None
        i = tracks.index(own) + steps
        if not (0 <= i < len(tracks)):
            return None
        plan[c[0]] = tracks[i]
    return plan


def _restore_cross_track(self, drag):
    # Every member back on its drag-start track at its drag-start frame
    for m in drag.get("group") or []:
        c = m["clip"]
        own = (drag.get("src_tracks") or {}).get(c[0])
        if own is not None and self._clip_track_id(c) != own:
            self._relocate_clip(c, own)
        c[2] = m["start"]
    clip = drag["clip"]
    if self._clip_track_id(clip) != drag["src_track"]:
        self._relocate_clip(clip, drag["src_track"])
    clip[2] = drag["orig_start"]


def _cross_track_preview(self, drag, target):
    self._set_snap_frame(None)
    clip = drag["clip"]
    plan = _cross_track_plan(self, drag, target)
    _restore_cross_track(self, drag)
    ok = plan is not None
    if ok:
        for m in drag.get("group") or [{"clip": clip}]:
            c = m["clip"]
            if c[0] in plan:
                self._relocate_clip(c, plan[c[0]])
        ids = set(plan)
        for cid, tid in plan.items():
            c = self._clip_by_id(cid)
            if c is None:
                continue
            end = c[2] + c[3]
            for o in self.clips_by_track.get(tid, []):
                if o[0] in ids:
                    continue
                if o[2] < end and o[2] + o[3] > c[2]:
                    ok = False
                    break
    drag["target_track"] = target
    drag["target_ok"] = ok
    drag["track_plan"] = plan if ok else None
    self.table.viewport().update()
    if ok:
        self.status_bar.showMessage(
            f"Move to track {target}, frame {clip[2]} (time locked)")
    elif plan is None:
        self.status_bar.showMessage("No matching track for a linked clip")
    else:
        self.status_bar.showMessage("Overlaps a clip on the target track")


def _move_drag_release(self):
    drag = self._edge_drag
    self._edge_drag = None
    self._set_snap_frame(None)
    from sequence_timeline_insert import set_insert_frame
    set_insert_frame(self, None)
    clip = drag["clip"]
    target = drag.get("target_track")
    if target is not None:
        # Track change only: start_frames stay at drag start; the group
        # moves by _cross_track_plan (overlaps were checked in memory
        # against every clip that is not moving)
        plan = drag.get("track_plan")
        refused = not drag.get("target_ok", True) or not plan
        if not refused:
            try:
                conn = sqlite3.connect(self._project_db())
                cur = conn.cursor()
                from undo import push_undo
                push_undo(self._project_db(), "Move clip to track")
                for cid, tid in plan.items():
                    table, row_id = clip_table_and_id(cid)
                    cur.execute(
                        f"UPDATE {table} SET track_id = ? WHERE id = ?",
                        (tid, row_id))
                conn.commit()
                conn.close()
            except Exception as e:
                QMessageBox.warning(self, "Move Clip", f"Could not save clip: {e}")
        if refused:
            self._after_clip_change("Move refused: overlaps a clip on the target track")
        else:
            # BUILD 139: a track move changes the layer order only where
            # the stack differs; the snapshot diff at the refresh marks
            # exactly those frames (was: the clip's whole range)
            self._after_clip_change(f"Clip moved to track {target}")
        return
    group = drag.get("group") or [{"clip": clip}]
    new_start = max(0, int(clip[2]))
    if new_start == drag["orig_start"]:
        return
    from sequence_timeline_insert import insert_held, insert_drop
    if insert_held():
        # Shift held at the drop: slice and push every track to make room
        insert_drop(self, group)
        return
    # BUILD 134: drop guard -- a final position that overlaps a clip on
    # any moved clip's track (or is below 0) is refused, nothing saved
    from clip_drag import overlaps
    if all("blocks" in m for m in group) and overlaps(
            [(m["clip"][2], m["clip"][3], m["blocks"]) for m in group]):
        self._after_clip_change("Move refused: overlaps a clip")
        return
    from undo import push_undo
    push_undo(self._project_db(), "Move clip")
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        for m in group:
            c = m["clip"]
            from keyframe import is_keyframe_clip_id, persist_keyframe_clip_time
            if is_keyframe_clip_id(c[0]):
                persist_keyframe_clip_time(cur, c)
                continue
            table, row_id = clip_table_and_id(c[0])
            cur.execute(f"UPDATE {table} SET start_frame = ? WHERE id = ?",
                        (max(0, int(c[2])), row_id))
        conn.commit()
        conn.close()
        # Preview: a whole linked group alone over its frames moves its
        # clean frames with it (run right after the next forced
        # retarget, see _retarget_preview); otherwise old and new
        # ranges go dirty
        # BUILD 146: no per-edit plan: the mile-marker diff at the
        # refresh finds the pure move (and its dirty spans) by itself
        # ranges = self._move_preview_ranges(group, drag)
        # plan = self._preview_shift_plan(group, drag)
        # if plan is not None:
        #     self._preview_pending_shift = (plan, ranges)
        # BUILD 139: otherwise the snapshot diff at the refresh marks
        # only the frames that changed (was: old and new ranges)
    except Exception as e:
        QMessageBox.warning(self, "Move Clip", f"Could not save clip: {e}")
    self._after_clip_change(f"Clip moved to frame {new_start}")
    leftover = getattr(self, "_preview_pending_shift", None)
    if leftover:
        # The retarget did not run the shift: rebuild instead
        self._preview_pending_shift = None
        self._mark_preview_dirty(leftover[1])


def _move_preview_ranges(self, group, drag):
    # Inclusive frame ranges a time move changes in the preview: old and
    # new ranges of moved video/image and keyframe clips, plus linked
    # partners left in place (solo drag) that composite with them.
    from keyframe import is_keyframe_clip_id
    ranges = []
    ids = set()
    kf_links = set()
    vis_links = set()
    for m in group:
        c = m["clip"]
        ids.add(c[0])
        link_id = c[8] if len(c) > 8 else None
        if is_keyframe_clip_id(c[0]):
            if link_id is not None:
                kf_links.add(link_id)
        elif self._track_type(self._clip_track_id(c)) in ("video", "image"):
            if link_id is not None:
                vis_links.add(link_id)
        else:
            continue
        ln = max(1, int(c[3]))
        old_ln = max(1, int(m.get("length", drag.get("orig_length", c[3]))))
        old = max(0, int(m.get("start", drag["orig_start"])))
        new = max(0, int(c[2]))
        ranges.append((old, old + old_ln - 1))
        ranges.append((new, new + ln - 1))
    if kf_links or vis_links:
        for track_id, clips in self.clips_by_track.items():
            kind = self._track_type(track_id)
            for c in clips:
                link_id = c[8] if len(c) > 8 else None
                if c[0] in ids or link_id is None:
                    continue
                if ((link_id in kf_links and kind in ("video", "image"))
                        or (link_id in vis_links and is_keyframe_clip_id(c[0]))):
                    s0 = max(0, int(c[2]))
                    ranges.append((s0, s0 + max(1, int(c[3])) - 1))
    return ranges


def _preview_shift_plan(self, group, drag):
    # (first, last, delta) when the clean preview rows can move with the
    # group (FramesShift): the whole linked group moved by one delta and
    # no other video/image clip touches the old or new frame span. Any
    # doubt returns None and the caller marks old and new ranges dirty.
    if self.sequence_id is None or not group:
        return None
    try:
        from keyframe import is_keyframe_clip_id
        ids = set()
        links = set()
        deltas = set()
        span = []
        visual = False
        for m in group:
            c = m["clip"]
            ids.add(c[0])
            if len(c) > 8 and c[8] is not None:
                links.add(c[8])
            old = int(m.get("start", drag["orig_start"]))
            deltas.add(max(0, int(c[2])) - old)
            kind = self._track_type(self._clip_track_id(c))
            if is_keyframe_clip_id(c[0]) or kind in ("video", "image"):
                visual = visual or kind in ("video", "image")
                span.append((old, old + max(1, int(c[3])) - 1))
        # BUILD 139: an unlinked clip (group of one) may shift too; the
        # snapshot check (shift_ok) proves the move before it runs
        if len(deltas) != 1 or not visual or not span:
            return None
        if not links and len(group) != 1:
            return None
        delta = deltas.pop()
        if delta == 0:
            return None
        first = min(a for a, _b in span)
        last = max(b for _a, b in span)
        if first + delta < 0:
            return None
        for track_id, clips in self.clips_by_track.items():
            kind = self._track_type(track_id)
            for c in clips:
                if c[0] in ids:
                    continue
                # Part of a moved link left behind: not the whole group
                if len(c) > 8 and c[8] is not None and c[8] in links:
                    return None
                if kind not in ("video", "image"):
                    continue
                s0 = int(c[2])
                e0 = s0 + max(1, int(c[3])) - 1
                for a, b in ((first, last), (first + delta, last + delta)):
                    if s0 <= b and e0 >= a:
                        return None
        # print(f"Preview shift plan frames {first}..{last} by {delta}")
        return (first, last, delta)
    except Exception as e:
        # print(f"Preview shift skipped: {e}")
        return None


def _ranges_for_clips(self, clips):
    # Inclusive (first, last) frame ranges for preview dirty marks.
    ranges = []
    for c in clips:
        in_f = max(0, int(c[2]))
        length = max(1, int(c[3]))
        ranges.append((in_f, in_f + length - 1))
    return ranges


def _clip_context_menu(self, clip, pos):
    # Right-click on an unselected clip selects it like a normal click
    if clip[0] not in self._selected_clip_ids:
        self._select_clip(clip, "normal")
    from keyframe import is_keyframe_clip_id, add_key as _add_key
    if is_keyframe_clip_id(clip[0]):
        menu = QMenu(self)
        clip_acts = self._add_clipboard_actions(menu)     # BUILD 159: top
        key_menu = menu.addMenu("Key")
        key_click_action = key_menu.addAction("Add At Mouse Click")
        key_tick_action = key_menu.addAction("Add At Playhead")
        # BUILD 270: paste a copied key (Copy Keyframe on a key box)
        from keyframe import can_paste_key, paste_frame, paste_key
        paste_menu = key_menu.addMenu("Paste")
        paste_acts = {paste_menu.addAction("At Mouse Click"): "mouse",
                      paste_menu.addAction("At Playhead"): "playhead",
                      paste_menu.addAction("At Clip Center"): "center"}
        paste_menu.setEnabled(can_paste_key(clip))
        # BUILD 260: keys outside the clip's [in, out) of the right-clicked
        # keyframe clip, or of every selected keyframe clip when it is
        # selected; disabled when there are none
        from keyframe import keys_beyond_horizon
        horizon_clips = [clip]
        if clip[0] in self._selected_clip_ids:
            horizon_clips += [c for c in self._selected_clips()
                              if c[0] != clip[0] and is_keyframe_clip_id(c[0])]
        horizon_action = key_menu.addAction("Delete Keys Beyond Event Horizon")
        horizon_action.setEnabled(any(keys_beyond_horizon(c) for c in horizon_clips))
        # BUILD 263 / 266: listed when a clip in the same scope has a key
        # beyond the horizon; in each such clip the nearest one is centered
        # and the other keys beyond it are deleted
        center_action = None
        if any(keys_beyond_horizon(c) for c in horizon_clips):
            center_action = key_menu.addAction("Center Key Beyond Event Horizon")
        # BUILD 303: clip speed, whole link stack (clip_speed.py)
        speed_action = menu.addAction("Speed...")
        rename_action = menu.addAction("Rename")
        # BUILD 221: a new keyframe clip of any type on this track, starting
        # at this clip's out frame (the Add Keyframe Clip dialog; it fills
        # the free gap after this clip, see keyframe.add_keyframe_clip)
        add_after_action = menu.addAction("Add Keyframe Clip After")
        # BUILD 133: Link / Unlink from the whole selection
        link_action, unlink_action = self._add_link_actions(menu, clip)
        menu.addSeparator()
        # clip_acts = self._add_clipboard_actions(menu)     # BUILD 156
        # menu.addSeparator()
        delete_action = menu.addAction("Delete")
        ripple_action = menu.addAction("Ripple Delete")     # BUILD 245
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action is None:
            return
        # if self._run_clipboard_action(action, clip_acts, clip):
        if self._run_clipboard_action(action, clip_acts, clip, pos):   # BUILD 160
            return
        if action in paste_acts:
            click_frame = self.view_start_frame + self._x_in_clips(pos) / self.pixels_per_frame
            paste_key(self, clip, paste_frame(clip, paste_acts[action],
                                              click_frame, self.current_frame))
        elif action == key_click_action:
            click_frame = self.view_start_frame + self._x_in_clips(pos) / self.pixels_per_frame
            _add_key(self, clip, click_frame)
        elif action == key_tick_action:
            # Playhead frame; add_key clamps it to the clip [in, out)
            _add_key(self, clip, int(self.current_frame))
        elif center_action is not None and action == center_action:
            from keyframe import center_key_beyond_horizon
            center_key_beyond_horizon(self, horizon_clips)
        elif action == horizon_action:
            from keyframe import delete_keys_beyond_horizon
            delete_keys_beyond_horizon(self, horizon_clips)
        elif action == add_after_action:
            self.add_keyframe_clip(self._clip_track_id(clip),
                                   int(clip[2]) + max(1, int(clip[3])))
        elif action == speed_action:
            from clip_speed import edit_clip_speed
            edit_clip_speed(self, clip)
        elif action == rename_action:
            from keyframe import rename_keyframe_clip
            rename_keyframe_clip(self, clip)
        elif link_action is not None and action == link_action:
            self.link_selected_clips(clip)
        elif unlink_action is not None and action == unlink_action:
            self.unlink_clips(clip)
        elif action == delete_action:
            self.delete_selected_clip()
        elif action == ripple_action:
            self.ripple_delete_clips()
        return
    if is_caption_id(clip[0]):
        # Caption clip menu: Edit, Slice, Link or Unlink, Delete
        # (BUILD 133: one of the two, same rule as the other clip menus)
        menu = QMenu(self)
        clip_acts = self._add_clipboard_actions(menu)     # BUILD 159: top
        edit_action = menu.addAction("Edit")
        slice_action = menu.addAction("Slice")
        speed_action = menu.addAction("Speed...")     # BUILD 303
        link_action, unlink_action = self._add_link_actions(menu, clip)
        menu.addSeparator()
        # clip_acts = self._add_clipboard_actions(menu)     # BUILD 156
        # menu.addSeparator()
        delete_action = menu.addAction("Delete")
        ripple_action = menu.addAction("Ripple Delete")     # BUILD 245
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        # if self._run_clipboard_action(action, clip_acts, clip):
        if self._run_clipboard_action(action, clip_acts, clip, pos):   # BUILD 160
            return
        if action == edit_action:
            self.edit_caption(clip)
        elif action == slice_action:
            self.slice_clip(clip, self._x_in_clips(pos))
        elif action == speed_action:
            from clip_speed import edit_clip_speed
            edit_clip_speed(self, clip)
        elif link_action is not None and action == link_action:
            self.link_selected_clips(clip)
        elif unlink_action is not None and action == unlink_action:
            self.unlink_clips(clip)
        elif action == delete_action:
            self.delete_selected_clip()
        elif action == ripple_action:
            self.ripple_delete_clips()
        return
    enabled = bool(clip[5])
    menu = QMenu(self)
    clip_acts = self._add_clipboard_actions(menu)     # BUILD 159: top
    # Gain... on audio-track clips only (the only rows the preview audio
    # mixes); right-clicked clip only, like Enable / Solo
    gain_action = None
    gen_caps_action = None
    # BUILD 105: sequence clips have no footage: no Gain / Proxy
    is_seq_clip = clip[0] in (getattr(self, "_seq_refs", None) or {})
    if self._track_type(self._clip_track_id(clip)) == "audio" and not is_seq_clip:
        gain_action = menu.addAction("Gain...")
        # BUILD 225: transcribe into a new caption track above this track
        gen_caps_action = menu.addAction("Generate Captions")
        menu.addSeparator()
    slice_action = menu.addAction("Slice")
    # BUILD 283: Scene Detect (Slice) on video-track clips with footage
    # (sequence_timeline_scenes; slices this clip and its link partners)
    scene_action = None
    if self._track_type(self._clip_track_id(clip)) == "video" and not is_seq_clip:
        scene_action = menu.addAction("Scene Detect (Slice)")
    # BUILD 303: clip speed, whole link stack (clip_speed.py)
    speed_action = menu.addAction("Speed...")
    marker_menu = menu.addMenu("Marker")
    marker_click_action = marker_menu.addAction("Add To Mouse Click")
    marker_tick_action = marker_menu.addAction("Add to Time Tick")
    # BUILD 133: Link / Unlink from the whole selection
    link_action, unlink_action = self._add_link_actions(menu, clip)
    menu.addSeparator()
    enable_action = menu.addAction("Disable" if enabled else "Enable")
    solo_action = menu.addAction("Solo")
    solo_action.setCheckable(True)
    solo_action.setChecked(bool(clip[6]))
    # Proxy submenu on video / image clips only (not audio)
    proxy_actions = {}
    if (self._track_type(self._clip_track_id(clip)) in ("video", "image")
            and not is_seq_clip):
        from clip_proxy import add_clip_proxy_submenu
        proxy_actions = add_clip_proxy_submenu(self, menu, clip)
    menu.addSeparator()
    # clip_acts = self._add_clipboard_actions(menu)     # BUILD 156
    # menu.addSeparator()
    delete_action = menu.addAction("Delete")
    # BUILD 245: Delete, then close the gaps (sequence_timeline_sql.ripple_delete_clips)
    ripple_action = menu.addAction("Ripple Delete")
    action = menu.exec(self.table.viewport().mapToGlobal(pos))
    # if self._run_clipboard_action(action, clip_acts, clip):
    if self._run_clipboard_action(action, clip_acts, clip, pos):   # BUILD 160
        return
    if action is not None and action in proxy_actions:
        from clip_proxy import apply_clip_proxy
        apply_clip_proxy(self, clip, proxy_actions[action])
        return
    if action == delete_action:
        # Same as the Delete key: selected clips, plus link partners
        # unless selection solo
        self.delete_selected_clip()
        return
    if action == ripple_action:
        self.ripple_delete_clips()
        return
    if action == speed_action:
        from clip_speed import edit_clip_speed
        edit_clip_speed(self, clip)
        return
    if gain_action is not None and action == gain_action:
        from clip_gain import edit_clip_gain
        edit_clip_gain(self, clip)
        return
    if gen_caps_action is not None and action == gen_caps_action:
        from sequence_timeline_captions import generate_captions
        generate_captions(self, clip)
        return
    if scene_action is not None and action == scene_action:
        from sequence_timeline_scenes import scene_detect
        scene_detect(self, clip)
        return
    if action is None:
        return
    if action == marker_click_action:
        self._add_marker(clip, pos)
    elif action == marker_tick_action:
        self._add_marker(clip, None)
    elif action == slice_action:
        self.slice_clip(clip, self._x_in_clips(pos))
    elif link_action is not None and action == link_action:
        self.link_selected_clips(clip)
    elif unlink_action is not None and action == unlink_action:
        self.unlink_clips(clip)
    elif action == enable_action:
        self._toggle_clip_flag(clip[0], "enabled")
    elif action == solo_action:
        self._toggle_clip_flag(clip[0], "solo")


def _edge_drag_release(self):
    drag = self._edge_drag
    self._edge_drag = None
    self._set_snap_frame(None)
    clip = drag["clip"]
    # print(f"ICON: edge-drag release clip_id={clip[0]} in_frame={clip[7]} "
          # f"edge={drag['edge']} (icon requested at row height on next paint, generate=True)")
    if (clip[2] == drag["orig_start"] and clip[3] == drag["orig_length"]
            and clip[7] == drag["orig_in"]):
        return
    group = drag.get("group") or [{"clip": clip}]
    from undo import push_undo
    push_undo(self._project_db(), "Trim clip")
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        for m in group:
            c = m["clip"]
            from keyframe import is_keyframe_clip_id, persist_keyframe_clip_time
            if is_keyframe_clip_id(c[0]):
                # edge trim: keys stay at their sequence frames
                persist_keyframe_clip_time(cur, c, shift_keys=False)
                continue
            if is_caption_id(c[0]):
                cur.execute(
                    "UPDATE captions SET start_frame = ?, length_frames = ? WHERE id = ?",
                    (c[2], c[3], -c[0])
                )
                continue
            cur.execute(
                "UPDATE clips SET start_frame = ?, length_frames = ?, in_frame = ? "
                "WHERE id = ?",
                (c[2], c[3], c[7], c[0])
            )
        conn.commit()
        conn.close()
        # Preview (BUILD 139): the snapshot diff at the refresh below
        # marks only the frames the trim changes (the added / removed
        # end, or the whole clip when its source or keys shift); was:
        # old and new ranges of trimmed clips and linked partners
    except Exception as e:
        QMessageBox.warning(self, "Clip Edge", f"Could not save clip: {e}")
    # The video clip of the group gets its out icon (dragged or partner)
    video_members = [m["clip"] for m in group if self._clip_is_video(m["clip"])]
    if video_members:
        clip = video_members[0]
    track_id = self._clip_track_id(clip)
    is_video = bool(video_members)
    self.refresh()
    if drag["edge"] == "right" and is_video:
        # clip is the live list edited by the drag, so clip[3] is the new
        # length. Out frame = in_frame + length_frames - 1
        from clip_speed import source_span
        out_frame = out_frame_for(clip[7], source_span(
            clip[3], (getattr(self, "_clip_speeds", None) or {}).get(clip[0], 100)))
        # print(f"ICON out release clip={clip[0]} in_frame={clip[7]} length={clip[3]} "
              # f"out_frame={out_frame} track_height={self._track_row_height(track_id)}")
        self._generate_out_icon(clip[1], track_id, out_frame)
    self.table.viewport().update()
    self.ruler.viewport().update()
    self._refresh_sequence_manager()
    # Length may have changed; keep frame and view inside it
    self._clamp_view()
    self.current_frame = max(0, min(self.total_frames() - 1, self.current_frame))
    self._save_view_state()
    self.status_bar.showMessage(f"Clip start {clip[2]}, length {clip[3]}", 3000)


def _clip_track_id(self, clip):
    for track_id, clips in self.clips_by_track.items():
        for c in clips:
            if c[0] == clip[0]:
                return track_id
    return None


def _clip_press_select(self, event, press_pos):
    # BUILD 162: the click-selection part of eventFilter's left press
    # (normal / Alt / Shift / already selected / empty space). Returns
    # hit_clip for the caller, which still uses it after the key check.
    hit_clip = self._clip_at(press_pos)
    edge_hit = self._clip_edge_at(press_pos)
    if hit_clip is None and edge_hit is not None:
        hit_clip = edge_hit[0]
    # Event modifiers plus the live keyboard state, so Alt is seen
    # even if the platform drops it from the mouse event
    mods = event.modifiers() | QApplication.queryKeyboardModifiers()
    if hit_clip is None:
        if self._x_in_clips(press_pos) >= 0:
            self._clear_selection()
    elif mods & Qt.AltModifier:
        # Alt: ONLY this clip, no link partners, solo on
        self._select_clip(hit_clip, "solo")
    elif mods & Qt.ShiftModifier:
        self._select_clip(hit_clip, "add")
    elif hit_clip[0] in self._selected_clip_ids:
        # Already selected (solo or multi): keep it so an edge
        # or move drag uses the same selection and does not clear
        # Alt solo
        pass
    else:
        self._select_clip(hit_clip, "normal")
    return hit_clip


def _clip_press_drag(self, event):
    # BUILD 162: eventFilter's left press, edge trim / move drag start.
    # True when a drag started (the caller returns True then).
    hit = self._clip_edge_at(event.position().toPoint())
    if hit is not None:
        clip, edge = hit
        self._edge_drag = {
            "clip": clip,
            "edge": edge,
            "orig_start": clip[2],
            "orig_length": clip[3],
            "orig_in": clip[7],
            "snap_px": self._snap_pixels(),
        }
        self._edge_drag["group"] = self._drag_group(clip, edge)
        return True
    clip = self._clip_at(event.position().toPoint())
    if clip is not None:
        press_frame = self._frame_at_clips_x(self._x_in_clips(event.position().toPoint()))
        self._edge_drag = {
            "clip": clip,
            "edge": "move",
            "orig_start": clip[2],
            "orig_length": clip[3],
            "grab_offset": press_frame - clip[2],
            "src_track": self._clip_track_id(clip),
            "target_track": None,
            "target_ok": True,
            "snap_px": self._snap_pixels(),
        }
        self._edge_drag["group"] = self._drag_group(clip, "move")
        return True
    return False


def _clip_edge_cursor(self, obj, pos):
    # BUILD 162: eventFilter's mouse move, edge-resize pointer over a clip edge
    if self._clip_edge_at(pos) is not None:
        obj.setCursor(Qt.SplitHCursor)
    else:
        obj.unsetCursor()


def _clip_drag_release(self):
    # BUILD 162: eventFilter's left release of a clip drag (move or edge)
    _move_pan_stop(self, self._edge_drag)     # BUILD 238
    if self._edge_drag["edge"] == "move":
        self._move_drag_release()
    else:
        self._edge_drag_release()
