# Caption-only helpers for sequence_timeline.py (no window class here).
# Window methods take the SequenceTimeline instance as self.
import sqlite3
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

CAPTION_LANGUAGES = ["eng", "spa", "fra", "deu", "ita", "por", "jpn", "kor",
                     "zho", "rus", "ara", "hin"]


def ensure_captions_table(project_db):
    # Schema lives in new_project.py (BUILD 78); kept as a no-op for callers
    return None


def is_caption_id(clip_id):
    # Caption clips use negative in-memory ids (not keyframe negatives)
    if clip_id is None or clip_id >= 0:
        return False
    try:
        from keyframe import is_keyframe_clip_id
        if is_keyframe_clip_id(clip_id):
            return False
    except ImportError:
        pass
    return True


def clip_table_and_id(clip_id):
    # (table, row id) for caption / keyframe / clips
    try:
        from keyframe import is_keyframe_clip_id, db_id_from_mem
        if is_keyframe_clip_id(clip_id):
            return "keyframe_clips", db_id_from_mem(clip_id)
    except ImportError:
        pass
    if is_caption_id(clip_id):
        return "captions", -clip_id
    return "clips", clip_id


def load_captions(cur, sequence_id, clips):
    # Caption clips: captions table, negative in-memory ids; link_id for groups
    try:
        cur.execute(
            "SELECT id, track_id, start_frame, length_frames, text, link_id "
            "FROM captions WHERE sequence_id = ? ORDER BY start_frame, id",
            (sequence_id,)
        )
    except sqlite3.OperationalError:
        # Old scratch DB without captions.link_id: skip caption rows
        return
    for row in cur.fetchall():
        cap_id, track_id, start_frame, length_frames, text, link_id = row
        clips.setdefault(track_id, []).append(
            [-cap_id, None, max(0, int(start_frame or 0)),
             max(1, int(length_frames or 1)), text or "", 1, 0, 0, link_id]
        )


def persist_caption_link_id(cur, clip_mem_id, link_id):
    # clip_mem_id is -captions.id; None clears group membership
    cur.execute(
        "UPDATE captions SET link_id = ? WHERE id = ?",
        (link_id, -int(clip_mem_id))
    )


def paint_caption_bar(painter, clip_rect):
    painter.fillRect(clip_rect, QColor(30, 110, 110))


def caption_label(painter, label, text_rect):
    # Caption text on the bar, one line, elided to fit
    return painter.fontMetrics().elidedText(
        " ".join(label.split()), Qt.ElideRight,
        max(0, text_rect.width() - 6))


CAPTION_TYPES = ["CEA-608", "CEA-708", "mov_text", "WebVTT"]
DEFAULT_CAPTION_TYPE = "WebVTT"


class CaptionTrackDialog(QDialog):
    # BUILD 101: one popup for Name, Language and Type
    def __init__(self, parent, default_name):
        from PySide6.QtWidgets import (QFormLayout, QLineEdit, QComboBox,
                                       QDialogButtonBox)
        super().__init__(parent)
        self.setWindowTitle("+ Caption")
        form = QFormLayout(self)
        self.name_edit = QLineEdit(default_name or "")
        self.name_edit.selectAll()
        form.addRow("Track name:", self.name_edit)
        self.lang_combo = QComboBox()
        self.lang_combo.setEditable(True)
        self.lang_combo.addItems(CAPTION_LANGUAGES)
        self.lang_combo.setCurrentIndex(0)
        form.addRow("Language (ISO 639-2):", self.lang_combo)
        self.type_combo = QComboBox()
        self.type_combo.addItems(CAPTION_TYPES)
        self.type_combo.setCurrentText(DEFAULT_CAPTION_TYPE)
        form.addRow("Type:", self.type_combo)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return ((self.name_edit.text() or "").strip(),
                (self.lang_combo.currentText() or "").strip().lower(),
                self.type_combo.currentText() or DEFAULT_CAPTION_TYPE)

    def _accept(self):
        name, lang, _t = self.values()
        if not name or not lang:
            QMessageBox.warning(self, "+ Caption", "Name and language are required")
            return
        self.accept()


def caption_type_text(language, caption_type, flags=""):
    # BUILD 101: Type column text for a caption track, three lines
    return "CC%s\n%s\n%s" % (flags, language or "",
                              caption_type or DEFAULT_CAPTION_TYPE)


def _add_caption_track(self):
    project_db = self._project_db()
    if not project_db:
        QMessageBox.warning(self, "+ Caption", "No project open")
        return
    from track_names import next_default_name, sequence_track_names
    default = next_default_name(
        "caption", sequence_track_names(project_db, self.sequence_id))
    dlg = CaptionTrackDialog(self, default)
    if dlg.exec() != QDialog.Accepted:
        return
    name, lang, ctype = dlg.values()
    if not name or not lang:
        return
    self._add_track("caption", "+ Caption", name=name, language=lang,
                    caption_type=ctype)


def _sequence_size(self):
    # (width, height) of this sequence, 1920x1080 if unknown
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        cur.execute("SELECT width, height FROM sequences WHERE id = ?",
                    (self.sequence_id,))
        row = cur.fetchone()
        conn.close()
        if row and row[0] and row[1]:
            return int(row[0]), int(row[1])
    except Exception:
        pass
    return 1920, 1080

def add_caption(self, track_id, click_frame):
    # Add Caption: dialog, then a 1-second captions row at the mouse mark
    from caption_edit import CaptionEditDialog
    seq_w, seq_h = self._sequence_size()
    dlg = CaptionEditDialog(self, None, seq_w, seq_h)
    if dlg.exec() != QDialog.Accepted:
        return
    v = dlg.values()
    if v is None:
        return
    start_frame = max(0, int(click_frame))
    length_frames = max(1, int(round(self.fps)))
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        if self._range_overlaps(cur, track_id, start_frame, length_frames):
            conn.close()
            self.status_bar.showMessage(
                "Add Caption refused: overlaps a caption on this track", 3000)
            return
        cur.execute(
            "INSERT INTO captions (sequence_id, track_id, start_frame, length_frames, "
            "text, font_family, font_size, pos_x, pos_y, scale) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.sequence_id, track_id, start_frame, length_frames,
             v["text"], v["font_family"], v["font_size"],
             v["pos_x"], v["pos_y"], v["scale"])
        )
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Add Caption", f"Could not add caption: {e}")
        return
    self._after_clip_change(f"Added caption at frame {start_frame}")

def edit_caption(self, clip):
    # Edit: dialog filled from the captions row; OK saves the changes
    from caption_edit import CaptionEditDialog
    cap_id = -clip[0]
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        cur.execute(
            "SELECT text, font_family, font_size, pos_x, pos_y, scale "
            "FROM captions WHERE id = ?", (cap_id,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Edit Caption", f"Could not read caption: {e}")
        return
    if row is None:
        return
    keys = ("text", "font_family", "font_size", "pos_x", "pos_y", "scale")
    seq_w, seq_h = self._sequence_size()
    dlg = CaptionEditDialog(self, dict(zip(keys, row)), seq_w, seq_h)
    if dlg.exec() != QDialog.Accepted:
        return
    v = dlg.values()
    if v is None:
        return
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        cur.execute(
            "UPDATE captions SET text = ?, font_family = ?, font_size = ?, "
            "pos_x = ?, pos_y = ?, scale = ? WHERE id = ?",
            (v["text"], v["font_family"], v["font_size"],
             v["pos_x"], v["pos_y"], v["scale"], cap_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Edit Caption", f"Could not save caption: {e}")
        return
    self._after_clip_change("Caption updated")


# ---- Generate Captions (BUILD 225): audio clip right-click

_active = set()     # BUILD 233: (sequence_id, clip_id) of runs with an open window


def caption_job_active(sequence_id):
    # True while a Generate Captions run on that sequence has its window open
    return any(sid == int(sequence_id) for sid, _cid in _active)


def any_caption_job_active():
    # True while any Generate Captions run (any sequence) has its window open
    return bool(_active)

def _gen_bridge(self):
    # One QObject per timeline; done(clip_id, result, error) and
    # stage(clip_id, name, a, b) are queued from the worker thread to the
    # GUI thread
    br = getattr(self, "_caption_gen_bridge", None)
    if br is None:
        from PySide6.QtCore import QObject, Signal

        class _Bridge(QObject):
            done = Signal(int, object, str)
            stage = Signal(int, str, float, float)

        br = _Bridge(self)
        br.done.connect(lambda cid, res, err: _finish_generate(self, cid, res, err),
                        Qt.QueuedConnection)
        br.stage.connect(lambda cid, name, a, b: _on_stage(self, cid, name, a, b),
                         Qt.QueuedConnection)
        self._caption_gen_bridge = br
        self._caption_gen_jobs = {}
    return br


def _audio_clip_info(self, clip_id):
    # The audio clip's row (+ footage source), or None if gone / not audio
    conn = sqlite3.connect(self._project_db())
    try:
        row = conn.execute(
            "SELECT c.track_id, c.start_frame, c.length_frames, "
            "COALESCE(c.in_frame, 0), f.original_path, f.filename, "
            "LOWER(COALESCE(t.track_type, '')), t.sequence_id "
            "FROM clips c JOIN tracks t ON t.id = c.track_id "
            "LEFT JOIN footage f ON f.id = c.footage_id WHERE c.id = ?",
            (int(clip_id),)).fetchone()
    finally:
        conn.close()
    if row is None or row[6] != "audio" or int(row[7]) != int(self.sequence_id):
        return None
    import os
    track_id, start, length, in_f, orig, fname = row[:6]
    return {"project_db": self._project_db(), "sequence_id": int(self.sequence_id),
            "clip_id": int(clip_id), "track_id": int(track_id),
            "start_frame": max(0, int(start or 0)),
            "length_frames": max(1, int(length or 1)),
            "in_frame": max(0, int(in_f or 0)),
            "basename": os.path.basename(orig or fname or ""),
            "source_path": orig or fname or "", "fps": float(self.fps or 30.0)}


def generate_captions(self, clip):
    # Generate Captions: transcribe the audio clip's source range on a
    # worker thread (captions_generate.generate); _finish_generate writes
    # the new caption track + captions on the GUI thread as one undo step
    import captions_generate as cg
    title = "Generate Captions"
    if not cg.whisper_available():
        QMessageBox.information(
            self, title, "Generate Captions needs faster-whisper.\n\n"
            "Install it with:\n    %s\n\nthen try again." % cg.INSTALL_HINT)
        return
    _gen_bridge(self)
    cid = int(clip[0])
    if cid in self._caption_gen_jobs:
        self.status_bar.showMessage("Captions are already being generated "
                                    "for this clip", 3000)
        return
    try:
        info = _audio_clip_info(self, cid)
    except Exception as e:
        QMessageBox.warning(self, title, "Could not read the clip: %s" % e)
        return
    if info is None:
        return
    # BUILD 231: a model missing from the local cache is downloaded only
    # after Yes here (the check is local only)
    info["allow_download"] = False
    if not cg.model_present():
        ans = QMessageBox.question(
            self, title,
            'Generate Captions needs the Whisper "%s" model (about %s). '
            'Download it now to:\n%s?' % (cg.WHISPER_MODEL, cg.model_size_text(),
                                          cg.model_cache_dir()),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        info["allow_download"] = True
    import threading
    cancel = threading.Event()
    state = {}
    job = {"info": info, "cancel": cancel, "state": state, "stage": "",
           "cancelled": False}
    self._caption_gen_jobs[cid] = job
    _open_progress(self, cid, job)
    br = self._caption_gen_bridge
    fn = getattr(self, "_caption_transcribe_fn", None)   # tests

    def emit(name, a=0.0, b=0.0):
        try:
            br.stage.emit(cid, name, float(a), float(b))
        except RuntimeError:
            pass   # timeline closed

    def work():
        # BUILD 232: model download (after Yes) as its own step, then the
        # local-only transcription
        try:
            if info["allow_download"]:
                emit("download")
                cg.download_model_files(cg.WHISPER_MODEL, state, cancel)
                info["allow_download"] = False
                emit("downloaded")
            res, err = cg.generate(info, cancel, fn,
                                   progress=lambda a, b: emit("progress", a, b),
                                   stage=emit), ""
        except cg.CaptionGenError as e:
            res, err = None, str(e) or "Transcription failed."
        except Exception as e:
            res, err = None, "Transcription failed: %s" % e
        try:
            br.done.emit(cid, res, err)
        except RuntimeError:
            pass   # timeline closed

    threading.Thread(target=work, name="GenerateCaptions", daemon=True).start()
    self.status_bar.showMessage("Generating captions for %s ..." % (
        info["basename"] or "clip"))


def _mmss(seconds):
    s = max(0, int(seconds))
    return "%02d:%02d" % (s // 60, s % 60)


def _open_progress(self, cid, job):
    # BUILD 232: one progress window for the whole run, under the
    # timeline's menu bar (window_place key caption_progress). Text and bar
    # per stage (_on_stage; the download bytes from a 250 ms timer); Cancel
    # (or closing it) cancels the run (_cancel_generate). BUILD 233:
    # window-modal to the timeline (Qt also blocks the timeline's parent
    # windows: Sequence Manager / Project Manager), undo / redo blocked and
    # the sequence marked busy (caption_job_active) until it closes
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QProgressDialog
    dlg = QProgressDialog("Reading audio...", "Cancel", 0, 0, self)
    dlg.setWindowTitle("Generate Captions")
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setMinimumDuration(0)
    dlg.setMinimumWidth(380)
    dlg.canceled.connect(lambda: _cancel_generate(self, cid))
    from window_place import place_under_parent
    place_under_parent(dlg, self, "caption_progress")
    timer = QTimer(dlg)
    timer.setInterval(250)
    timer.timeout.connect(lambda: _poll_download(self, cid))
    job["dlg"], job["timer"] = dlg, timer
    key = (int(self.sequence_id), int(cid))
    _active.add(key)
    import undo
    undo.set_blocked("captions:%d:%d" % key, True)
    dlg.show()


def _close_progress(job):
    # Close the window without it counting as Cancel; the sequence is
    # editable again and undo / redo work
    key = (int(job["info"]["sequence_id"]), int(job["info"]["clip_id"]))
    _active.discard(key)
    import undo
    undo.set_blocked("captions:%d:%d" % key, False)
    job["timer"].stop()
    dlg = job["dlg"]
    try:
        dlg.canceled.disconnect()
    except (RuntimeError, TypeError):
        pass
    dlg.close()
    dlg.deleteLater()


def _poll_download(self, cid):
    # Download stage: bytes from the tqdm hook (state["cur"]) or, without
    # the hook, the size of the *.incomplete blob being written
    job = (getattr(self, "_caption_gen_jobs", None) or {}).get(cid)
    if job is None or job["cancelled"] or job["stage"] != "download":
        return
    import captions_generate as cg
    st = job["state"]
    total = st.get("total") or 0
    if not total:
        return
    cur = st.get("cur", 0) if st.get("hook") else cg.incomplete_bytes()
    done = min(total, st.get("done", 0) + cur)
    mb = 1024.0 * 1024.0
    dlg = job["dlg"]
    dlg.setRange(0, 1000)
    dlg.setValue(int(1000 * done / total))
    dlg.setLabelText("Downloading Whisper model: %.0f of %.0f MB" % (done / mb, total / mb))


def _on_stage(self, cid, name, a, b):
    # Worker stage / progress -> progress window and status bar
    job = (getattr(self, "_caption_gen_jobs", None) or {}).get(cid)
    if job is None or job["cancelled"]:
        return
    dlg = job["dlg"]
    clip = job["info"]["basename"] or "clip"
    if name == "download":
        job["stage"] = "download"
        dlg.setRange(0, 0)
        dlg.setLabelText("Downloading Whisper model...")
        self.status_bar.showMessage("Downloading Whisper model...")
        job["timer"].start()
    elif name == "downloaded":
        job["timer"].stop()
        job["stage"] = "downloaded"
        self.status_bar.showMessage("Whisper model downloaded")
    elif name == "loading":
        job["stage"] = "loading"
        dlg.setRange(0, 0)
        dlg.setLabelText("Loading Whisper model...")
        self.status_bar.showMessage("Loading Whisper model...")
    elif name in ("transcribing", "progress"):
        if job["stage"] != "transcribing":
            job["stage"] = "transcribing"
            dlg.setRange(0, 1000)
            self.status_bar.showMessage("Transcribing %s..." % clip)
        total = b if b > 0 else job["info"]["length_frames"] / float(job["info"]["fps"] or 30.0)
        dlg.setValue(int(1000 * min(1.0, a / total)) if total > 0 else 0)
        dlg.setLabelText("Transcribing: %s of %s" % (_mmss(a), _mmss(total)))


def _cancel_generate(self, cid):
    # Cancel / window closed: the worker stops at its next check (between
    # segments; a download aborts in the transfer when the tqdm hook is
    # available, else that file finishes in the background); nothing is
    # written. The job entry stays until the worker ends.
    job = (getattr(self, "_caption_gen_jobs", None) or {}).get(cid)
    if job is None or job["cancelled"]:
        return
    job["cancelled"] = True
    job["cancel"].set()
    _close_progress(job)
    if job["stage"] == "download":
        # huggingface_hub 1.x+ drops the partial file on a stopped download
        # (older versions keep *.incomplete and resume); either way the
        # model counts as not downloaded and the next run asks again
        self.status_bar.showMessage("Whisper model download cancelled", 6000)
    else:
        self.status_bar.showMessage("Generate Captions cancelled", 4000)


def _finish_generate(self, clip_id, result, error):
    # GUI thread. Re-reads the audio clip: if it was deleted, or its source
    # range (in / length) or track changed, nothing is written. Otherwise:
    # one undo step "Generate captions"; a caption track directly above the
    # clip's audio track (tracks renumbered 0..n-1); one caption per group
    # at clip start + t * fps; captions join the clip's link group (the
    # audio clip's id when it has none, as Link does).
    title = "Generate Captions"
    job = (getattr(self, "_caption_gen_jobs", None) or {}).pop(int(clip_id), None)
    if job is None:
        return
    info = job["info"]
    if job["cancelled"]:
        return    # cancelled: window already closed, nothing is written
    if error:
        _close_progress(job)
        self.status_bar.showMessage("Generate Captions failed", 3000)
        QMessageBox.warning(self, title, error)
        return
    job["dlg"].setRange(0, 0)
    job["dlg"].setLabelText("Adding captions...")
    try:
        now = _audio_clip_info(self, clip_id)
    except Exception:
        now = None
    if (now is None or now["track_id"] != info["track_id"]
            or now["in_frame"] != info["in_frame"]
            or now["length_frames"] != info["length_frames"]):
        _close_progress(job)
        QMessageBox.warning(self, title, "The audio clip was deleted or changed "
                            "while transcribing; no captions were added.")
        self.status_bar.showMessage("Generate Captions: clip changed", 3000)
        return
    import captions_generate as cg
    caps = cg.groups_to_frames(result.get("groups"), self.fps,
                               now["start_frame"], now["length_frames"])
    if not caps:
        _close_progress(job)
        self.status_bar.showMessage("No speech found", 4000)
        return
    from sequence_timeline_link import link_group_sizes, effective_link
    from track_names import next_default_name, sequence_track_names
    from caption_edit import DEFAULT_FONT_FAMILY, DEFAULT_SCALE
    project_db = self._project_db()
    mem = self._clip_by_id(clip_id) or [clip_id] + [None] * 8
    link = effective_link(mem, link_group_sizes(self.clips_by_track))
    if link is None:
        link = int(clip_id)
    name = next_default_name("caption", sequence_track_names(
        project_db, self.sequence_id))
    from undo import push_undo
    push_undo(project_db, "Generate captions")
    try:
        conn = sqlite3.connect(project_db)
        try:
            cur = conn.cursor()
            ids = [r[0] for r in cur.execute(
                "SELECT id FROM tracks WHERE sequence_id = ? "
                "ORDER BY sort_order, id", (self.sequence_id,)).fetchall()]
            cur.execute(
                "INSERT INTO tracks (sequence_id, name, track_type, sort_order, "
                "language) VALUES (?, ?, 'caption', 0, ?)",
                (self.sequence_id, name, result.get("language") or "eng"))
            new_track = cur.lastrowid
            try:
                cur.execute("UPDATE tracks SET caption_type = ? WHERE id = ?",
                            (DEFAULT_CAPTION_TYPE, new_track))
            except sqlite3.OperationalError:
                pass   # old DB without caption_type
            i = ids.index(info["track_id"]) if info["track_id"] in ids else 0
            ids.insert(i, new_track)
            for order, tid in enumerate(ids):
                cur.execute("UPDATE tracks SET sort_order = ? WHERE id = ?",
                            (order, tid))
            cur.execute("UPDATE clips SET link_id = ? WHERE id = ?",
                        (link, int(clip_id)))
            for start, length, text in caps:
                cur.execute(
                    "INSERT INTO captions (sequence_id, track_id, start_frame, "
                    "length_frames, text, font_family, font_size, pos_x, pos_y, "
                    "scale, link_id) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, "
                    "?, ?)",
                    (self.sequence_id, new_track, start, length, text,
                     DEFAULT_FONT_FAMILY, DEFAULT_SCALE, link))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _close_progress(job)
        QMessageBox.warning(self, title, "Could not add the captions: %s" % e)
        return
    _close_progress(job)
    self._after_clip_change("Captions added: %d (%s, %s)" % (
        len(caps), name, result.get("device") or ""))
