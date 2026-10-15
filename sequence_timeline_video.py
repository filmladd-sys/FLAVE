# Video-only helpers for sequence_timeline.py (no window class here).
# Window methods take the SequenceTimeline instance as self.
import os
from PySide6.QtCore import Qt

VIDEO_EXTENSIONS = (".mov", ".mp4", ".mkv", ".avi", ".m4v")


def out_frame_for(in_frame, length_frames):
    # Source frame shown at a clip's right edge: last frame the clip covers
    return max(0, int(in_frame or 0) + max(1, int(length_frames or 1)) - 1)


def video_out_pixmap(tl, footage_id, height, in_frame, length_frames, clip_id):
    # Video out icon for paint: storage only, never generated here
    # Same key as _generate_out_icon: (footage_id, out frame,
    # height). Cache first, then the stored resized row of
    # that exact frame and height (any width).
    out_frame = out_frame_for(in_frame, length_frames)
    out_pix = tl.icon_pixmap(footage_id, height, out_frame,
                             generate=False, clip_id=clip_id)
    if out_pix is None or out_pix.isNull():
        miss = (out_frame, height)
        if tl._out_paint_miss.get(clip_id) != miss:
            tl._out_paint_miss[clip_id] = miss
            # print(f"ICON out paint miss frame={out_frame} height={height}")
    else:
        tl._out_paint_miss.pop(clip_id, None)
    return out_pix


def _add_video_track(self):
    self._add_track("video", "+ Video")


def _generate_out_icon(self, footage_id, track_id, out_frame):
    # Make, store and cache the out-point icon at track height (same
    # ensure_footage_icon path as the in icon). Called on drop and on
    # right-edge release, never from paint. Paint then finds it in
    # _icon_cache under (footage_id, out_frame, row height).
    out_frame = max(0, int(out_frame))
    if footage_id is None:
        # print("ICON out failed: no footage_id")
        return None
    height = self._track_row_height(track_id)
    if height <= 0:
        # print(f"ICON out failed: no row height for track_id={track_id}")
        return None
    project_db = self._project_db()
    if not project_db or not os.path.isfile(project_db):
        # print(f"ICON out failed: project db missing: {project_db}")
        return None
    try:
        from footage_manager_display import ensure_footage_icon
        pix = ensure_footage_icon(
            project_db, footage_id,
            frame_number=out_frame,
            target_height=height
        )
    except Exception as e:
        # print(f"ICON out failed: {type(e).__name__}: {e}")
        return None
    if pix is not None and not pix.isNull():
        pass
        # print(f"ICON out generated {pix.width()}x{pix.height()}")
    if pix is None or pix.isNull():
        # print(f"ICON out failed: no icon for footage_id={footage_id} frame={out_frame} "
              # f"height={height} (grab failed or frame past end of video)")
        return None
    self._icon_cache[(footage_id, out_frame, height)] = pix
    self.table.viewport().update()
    return pix


def _clip_is_video(self, clip):
    track_id = self._clip_track_id(clip)
    for r in range(self.table.rowCount()):
        item = self.table.item(r, 0)
        if item is not None and item.data(Qt.UserRole) == track_id:
            return (item.data(Qt.UserRole + 4) or "").lower() == "video"
    return False
