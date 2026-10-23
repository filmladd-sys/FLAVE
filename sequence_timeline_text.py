# sequence_timeline_text.py
# WYSIWYG Text track wiring for SequenceTimeline (BUILD 308): + WYSIWYG
# Text (track type "text") and Add Text Clip on an empty spot of a text
# track. Data, ids and the text image: text_clip.py. Style window:
# text_style_window.py. Every other clip edit (move, trim, slice, copy /
# paste, link, delete, speed) goes through the shared clip code with
# sequence_timeline_captions.clip_table_and_id -> text_clips.

import sqlite3
from PySide6.QtWidgets import QMessageBox


def _add_text_track(self):
    # Same name dialog / insert path as the other tracks via _add_track
    from text_clip import TEXT_TRACK_TYPE, TEXT_TRACK_LABEL
    self._add_track(TEXT_TRACK_TYPE, "+ " + TEXT_TRACK_LABEL)


def add_text_clip(self, track_id, click_frame):
    # Add Text Clip: a DEFAULT_SECONDS text clip at the mouse mark with the
    # defaults (text_clip.insert_text_clip), one undo step; refused when it
    # would overlap a text clip on this track. BUILD 315: its style is the
    # last used text style (text_clip.remembered_text_style; each missing
    # or invalid field its default).
    from text_clip import insert_text_clip, remembered_text_style, DEFAULT_SECONDS
    project_db = self._project_db()
    if not project_db:
        return None
    start_frame = max(0, int(click_frame))
    length_frames = max(1, int(round(DEFAULT_SECONDS * float(self.fps))))
    seq_w, seq_h = self._sequence_size()
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        if self._range_overlaps(cur, track_id, start_frame, length_frames):
            conn.close()
            self.status_bar.showMessage(
                "Add Text Clip refused: overlaps a text clip on this track",
                3000)
            return None
        conn.close()
        from undo import push_undo
        push_undo(project_db, "Add text clip")
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        mem_id = insert_text_clip(cur, self.sequence_id, track_id,
                                  start_frame, length_frames, seq_w, seq_h,
                                  style=remembered_text_style(seq_h))
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Add Text Clip",
                            f"Could not add text clip: {e}")
        return None
    self._after_clip_change(f"Added text clip at frame {start_frame}")
    return mem_id
