# sequence_preview_audio.py
# Mix sequence audio into {sequence_id}_previewaudio.wav next to the project
# file (same folder as the preview db). Source PCM is AudioTmp in the
# sibling .audiotmp.db. No FFmpeg. ASCII only.

import hashlib
import os
import sqlite3
import threading
import wave

try:
    import numpy as np
    _HAS_NP = True
except ImportError:
    np = None
    _HAS_NP = False

from peaks import PEAKS_SAMPLE_RATE, audiotmp_db_path, load_audiotmp

SAMPLE_RATE = PEAKS_SAMPLE_RATE  # 48000

# BUILD 135: per (project, sequence) mix state in this process.
#   gen   -- bumped on every rebuild; players reload their in-memory PCM
#            when it changes (the file stamp alone can repeat: a move keeps
#            the size, and a network share may report a cached / coarse
#            mtime right after os.replace)
#   dirty -- an edit happened (or nothing checked yet this session); the
#            scrub burst compares the signature once before it sounds
#   stale -- signature compared and differs: scrub stays silent until the
#            next Play rebuilds the WAV
_STATE = {}


def _state_key(project_db, sequence_id):
    return (os.path.normcase(os.path.abspath(project_db)), int(sequence_id))


def _state(project_db, sequence_id):
    return _STATE.setdefault(_state_key(project_db, sequence_id),
                             {"gen": 0, "dirty": True, "stale": None})


def mark_preview_audio_dirty(project_db, sequence_id=None):
    # Completed edit. sequence_id None = every sequence of the project
    # (e.g. source audio re-imported into AudioTmp). Cheap, never raises.
    try:
        if not project_db:
            return
        if sequence_id is not None:
            st = _state(project_db, sequence_id)
            st["dirty"] = True
            st["stale"] = None
            return
        db = os.path.normcase(os.path.abspath(project_db))
        for key, st in _STATE.items():
            if key[0] == db:
                st["dirty"] = True
                st["stale"] = None
    except Exception:
        pass


def preview_audio_generation(project_db, sequence_id):
    try:
        return _state(project_db, sequence_id)["gen"]
    except Exception:
        return None


def preview_audio_scrub_ok(project_db, sequence_id, fps, total_frames):
    # BUILD 136: silent while a background mix runs for this sequence
    try:
        job = _JOBS.get(_state_key(project_db, sequence_id))
        if job is not None and not job.done:
            return False
    except Exception:
        pass
    return _scrub_ok_sig(project_db, sequence_id, fps, total_frames)


def _scrub_ok_sig(project_db, sequence_id, fps, total_frames):
    # Scrub burst gate: True when the WAV matches the timeline. While dirty
    # the signature is compared once; a mismatch keeps scrub silent (no
    # rebuild here, Play rebuilds) until the next edit / rebuild.
    try:
        st = _state(project_db, sequence_id)
        # BUILD 136: a source re-import rewrites the sibling .audiotmp.db
        # (no timeline edit): its size / mtime change re-dirties the mix
        # (stands in for a peaks.py hook)
        src = _audiotmp_stamp(project_db)
        if src != st.get("src_stamp"):
            if "src_stamp" in st:
                st["dirty"] = True
                st["stale"] = None
            st["src_stamp"] = src
        if not st["dirty"]:
            return True
        if st["stale"] is None:
            wav = preview_audio_wav_path(project_db, sequence_id)
            have = _read_sig(preview_audio_sig_path(project_db, sequence_id))
            want = audio_timeline_signature(project_db, sequence_id, fps, total_frames)
            st["stale"] = not (os.path.isfile(wav) and want and have == want)
            if not st["stale"]:
                st["dirty"] = False
        return not st["stale"]
    except Exception:
        return False


def _audiotmp_stamp(project_db):
    try:
        st = os.stat(audiotmp_db_path(project_db))
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def preview_audio_wav_path(project_db, sequence_id):
    folder = os.path.dirname(os.path.abspath(project_db))
    return os.path.join(folder, "%d_previewaudio.wav" % int(sequence_id))


def preview_audio_sig_path(project_db, sequence_id):
    folder = os.path.dirname(os.path.abspath(project_db))
    return os.path.join(folder, "%d_previewaudio.sig" % int(sequence_id))


def _frame_to_sample(frame, fps):
    if not fps or fps <= 0:
        fps = 29.97
    return int(round(float(frame) * SAMPLE_RATE / float(fps)))


def _basename_for_row(original_path, filename):
    # Match AudioTmp keys used at import (basename of the source path)
    if original_path:
        return os.path.basename(original_path)
    if filename:
        return os.path.basename(filename)
    return ""


def _load_mix_rows(project_db, sequence_id):
    # Rows that contribute audio: enabled clips on AUDIO tracks only (what
    # is visible as audio on the timeline). Video-track rows add nothing;
    # a video's sound is its linked audio-track clip.
    # Solo: if any audio track is solo, only solo audio tracks.
    conn = sqlite3.connect(project_db)
    try:
        cur = conn.cursor()
        # gain_db is new in BUILD 74; an older clips table reads it as 0
        cur.execute("PRAGMA table_info(clips)")
        gain_sql = ("COALESCE(c.gain_db, 0)"
                    if "gain_db" in [r[1] for r in cur.fetchall()] else "0")
        cur.execute(
            "SELECT t.id, LOWER(COALESCE(t.track_type, '')), "
            "COALESCE(t.enabled, 1), COALESCE(t.solo, 0), "
            "c.footage_id, c.start_frame, c.length_frames, "
            "COALESCE(c.in_frame, 0), COALESCE(c.enabled, 1), "
            "COALESCE(c.solo, 0), f.original_path, f.filename, c.link_id, "
            + gain_sql + " "
            "FROM clips c "
            "JOIN tracks t ON t.id = c.track_id "
            "LEFT JOIN footage f ON f.id = c.footage_id "
            "WHERE t.sequence_id = ? "
            "AND LOWER(COALESCE(t.track_type, '')) = 'audio' "
            "ORDER BY t.sort_order, t.id, c.start_frame, c.id",
            (int(sequence_id),)
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    any_solo = any(int(r[3]) for r in rows)
    out = []
    for (track_id, track_type, track_en, track_solo, footage_id,
         start_frame, length_frames, in_frame, clip_en, clip_solo,
         original_path, filename, link_id, gain_db) in rows:
        if not int(track_en) or not int(clip_en):
            continue
        if any_solo and not int(track_solo):
            continue
        out.append({
            "track_id": int(track_id),
            "track_type": track_type or "",
            "footage_id": int(footage_id or 0),
            "start_frame": max(0, int(start_frame or 0)),
            "length_frames": max(0, int(length_frames or 0)),
            "in_frame": max(0, int(in_frame or 0)),
            "basename": _basename_for_row(original_path, filename),
            "link_id": link_id,
            "gain_db": float(gain_db or 0.0),
        })
    return out


def _track_order(project_db, sequence_id):
    # track_id -> row index in the timeline (ORDER BY sort_order, id, the
    # same order the timeline table uses; 0 = top)
    conn = sqlite3.connect(project_db)
    try:
        rows = conn.execute(
            "SELECT id FROM tracks WHERE sequence_id = ? ORDER BY sort_order, id",
            (int(sequence_id),)).fetchall()
    finally:
        conn.close()
    return {int(r[0]): i for i, r in enumerate(rows)}


def _load_volume_clips(project_db, sequence_id, track_order):
    # Linked 'volume' keyframe clips: [(link_id, in, out, [(frame, value)],
    # order)], keys sorted by frame and clamped 0..100. Missing tables = [].
    conn = sqlite3.connect(project_db)
    try:
        try:
            clips = conn.execute(
                "SELECT id, track_id, link_id, in_frame, out_frame FROM keyframe_clips "
                "WHERE sequence_id = ? AND type = 'volume' AND link_id IS NOT NULL "
                "ORDER BY id", (int(sequence_id),)).fetchall()
            keys = conn.execute(
                "SELECT k.keyframe_clip_id, k.frame, k.value FROM keyframes k "
                "JOIN keyframe_clips c ON c.id = k.keyframe_clip_id "
                "WHERE c.sequence_id = ? AND c.type = 'volume' "
                "ORDER BY k.frame, k.id", (int(sequence_id),)).fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()
    by_id = {}
    for kid, frame, value in keys:
        v = max(0.0, min(100.0, float(value)))
        by_id.setdefault(int(kid), []).append((int(frame), v))
    out = []
    for db_id, track_id, link_id, in_f, out_f in clips:
        order = track_order.get(int(track_id))
        if order is None:
            continue
        out.append((link_id, int(in_f), int(out_f), by_id.get(int(db_id), []),
                    int(order)))
    return out


def db_to_linear(gain_db):
    # Clip gain in dB -> linear factor (0 dB = 1.0, +6 dB ~ 2.0)
    return 10.0 ** (float(gain_db or 0.0) / 20.0)


def _interp(points, f):
    # Same rule as keyframe.evaluate_keyframes, for a float frame
    if len(points) == 1 or f <= points[0][0]:
        return points[0][1]
    if f >= points[-1][0]:
        return points[-1][1]
    for i in range(len(points) - 1):
        f0, v0 = points[i]
        f1, v1 = points[i + 1]
        if f0 <= f <= f1:
            if f1 == f0:
                return v1
            return v0 + (v1 - v0) * (f - f0) / float(f1 - f0)
    return points[-1][1]


def volume_gain(vol_clips, link_id, clip_order, dst0, count, fps):
    # Per-sample gain (value/100) for count samples starting at sequence
    # sample dst0, or None when no linked volume clip above applies. Rule
    # as scale / position: same link_id, keyframe track above the clip
    # (smaller order), covering the frame; nearest above wins. Keys are
    # interpolated per sample (fractional frame), held flat outside them.
    # A frame not covered by any volume clip keeps gain 1.
    if link_id is None or clip_order is None or count <= 0 or not vol_clips:
        return None
    cands = [v for v in vol_clips
             if v[0] == link_id and v[4] < int(clip_order) and v[3]]
    if not cands:
        return None
    if not fps or fps <= 0:
        fps = 29.97
    per = float(fps) / float(SAMPLE_RATE)
    # Farthest above first, so the nearest above is applied last and wins
    cands.sort(key=lambda v: v[4])
    if _HAS_NP:
        frame_f = (np.arange(dst0, dst0 + count, dtype=np.float64)) * per
        frame_i = np.floor(frame_f + 1e-9)
        gain = np.ones(count, dtype=np.float64)
        hit = False
        for _link, in_f, out_f, pts, _order in cands:
            mask = (frame_i >= in_f) & (frame_i < out_f)
            if not mask.any():
                continue
            hit = True
            xs = np.array([p[0] for p in pts], dtype=np.float64)
            ys = np.array([p[1] for p in pts], dtype=np.float64)
            gain[mask] = np.interp(frame_f[mask], xs, ys) / 100.0
        return gain if hit else None
    gain = [1.0] * count
    hit = False
    for i in range(count):
        f = (dst0 + i) * per
        fi = int(f + 1e-9)
        for _link, in_f, out_f, pts, _order in cands:
            if in_f <= fi < out_f:
                gain[i] = _interp(pts, f) / 100.0
                hit = True
    return gain if hit else None


def audio_timeline_signature(project_db, sequence_id, fps, total_frames):
    # Stable text signature of the mix inputs; hashed for the .sig file.
    rows = _load_mix_rows(project_db, sequence_id)
    parts = ["fps=%s" % repr(float(fps)), "frames=%d" % int(total_frames),
             "mix=audio-tracks-only"]
    order = _track_order(project_db, sequence_id)
    for r in rows:
        parts.append("link=%s order=%s gain=%r" % (
            r.get("link_id"), order.get(r["track_id"]), r.get("gain_db", 0.0)))
    for link_id, in_f, out_f, pts, kf_order in _load_volume_clips(
            project_db, sequence_id, order):
        parts.append("vol link=%s in=%d out=%d order=%d keys=%s" % (
            link_id, in_f, out_f, kf_order,
            ",".join("%d:%r" % (f, v) for f, v in pts)))
    for r in rows:
        parts.append(
            "t=%d:%s f=%d start=%d in=%d len=%d name=%s" % (
                r["track_id"], r["track_type"], r["footage_id"],
                r["start_frame"], r["in_frame"], r["length_frames"],
                r["basename"],
            )
        )
    # BUILD 135: the source PCM each row reads (AudioTmp row id + byte
    # length per basename: a re-import replaces the row) and the rate
    parts.append("rate=%d" % SAMPLE_RATE)
    for name, ident in _audiotmp_idents(
            project_db, sorted(set(r["basename"] for r in rows if r["basename"]))):
        parts.append("src=%s %s" % (name, ident))
    blob = "\n".join(parts).encode("ascii", "replace")
    return hashlib.sha256(blob).hexdigest()


def _audiotmp_idents(project_db, names):
    # [(basename, "id:len")] from the sibling .audiotmp.db, one query; the
    # blob length comes from the record header (no blob read)
    if not names:
        return []
    path = audiotmp_db_path(project_db)
    if not os.path.isfile(path):
        return [(n, "none") for n in names]
    try:
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT originalfilename, MAX(id), length(audioblob) FROM AudioTmp "
                "WHERE originalfilename IN (%s) GROUP BY originalfilename"
                % ",".join("?" * len(names)), list(names)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return [(n, "err") for n in names]
    got = dict((r[0], "%s:%s" % (r[1], r[2])) for r in rows)
    return [(n, got.get(n, "none")) for n in names]


def _read_sig(path):
    try:
        with open(path, "r") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""


def _write_sig(path, sig):
    with open(path, "w") as f:
        f.write(sig + "\n")


class MixCancelled(Exception):
    pass


def _mix_pcm(project_db, rows, fps, total_frames, vol_clips=None, track_order=None,
             cancel=None):
    # Return mono s16le bytes for the full sequence length (silence where empty).
    # vol_clips / track_order: linked volume keyframe gain (volume_gain).
    # cancel (BUILD 136): a threading.Event checked once per clip; set =
    # raise MixCancelled
    n = max(0, _frame_to_sample(total_frames, fps))
    if n <= 0:
        return b""
    vol_clips = vol_clips or []
    track_order = track_order or {}
    if _HAS_NP:
        acc = np.zeros(n, dtype=np.float64)
        for r in rows:
            if cancel is not None and cancel.is_set():
                raise MixCancelled()
            if r["length_frames"] <= 0 or not r["basename"]:
                continue
            pcm = load_audiotmp(project_db, r["basename"])
            if not pcm:
                continue
            src = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
            dst0 = _frame_to_sample(r["start_frame"], fps)
            src0 = _frame_to_sample(r["in_frame"], fps)
            count = _frame_to_sample(r["length_frames"], fps)
            if count <= 0:
                continue
            if src0 >= len(src):
                continue
            take = min(count, len(src) - src0)
            if take <= 0:
                continue
            dst1 = min(n, dst0 + take)
            if dst0 >= n or dst1 <= dst0:
                continue
            use = dst1 - dst0
            gain = volume_gain(vol_clips, r.get("link_id"),
                               track_order.get(r["track_id"]), dst0, use, fps)
            lin = db_to_linear(r.get("gain_db", 0.0))
            if gain is None:
                if lin == 1.0:
                    acc[dst0:dst1] += src[src0:src0 + use]
                else:
                    acc[dst0:dst1] += src[src0:src0 + use] * lin
            else:
                acc[dst0:dst1] += src[src0:src0 + use] * (gain * lin)
        acc = np.clip(acc, -32768, 32767).astype("<i2")
        return acc.tobytes()
    # array-module fallback
    import array
    acc = [0.0] * n
    for r in rows:
        if cancel is not None and cancel.is_set():
            raise MixCancelled()
        if r["length_frames"] <= 0 or not r["basename"]:
            continue
        pcm = load_audiotmp(project_db, r["basename"])
        if not pcm:
            continue
        src = array.array("h")
        raw = pcm[:len(pcm) // 2 * 2]
        src.frombytes(raw)
        dst0 = _frame_to_sample(r["start_frame"], fps)
        src0 = _frame_to_sample(r["in_frame"], fps)
        count = _frame_to_sample(r["length_frames"], fps)
        if count <= 0 or src0 >= len(src):
            continue
        take = min(count, len(src) - src0)
        gain = volume_gain(vol_clips, r.get("link_id"),
                           track_order.get(r["track_id"]), dst0, take, fps)
        lin = db_to_linear(r.get("gain_db", 0.0))
        for i in range(take):
            d = dst0 + i
            if d >= n:
                break
            g = lin if gain is None else gain[i] * lin
            acc[d] += float(src[src0 + i]) * g
    out = array.array("h")
    for v in acc:
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out.append(int(v))
    return out.tobytes()


def _write_wav(path, pcm_bytes):
    # Mono 48 kHz 16-bit little-endian WAV
    tmp = path + ".tmp"
    _write_wav_file(tmp, pcm_bytes)
    os.replace(tmp, path)


def _write_wav_file(path, pcm_bytes):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm_bytes)


def build_preview_audio_wav(project_db, sequence_id, fps, total_frames):
    # Always rebuild the WAV and write its signature. Returns wav path or None.
    try:
        if not project_db or sequence_id is None:
            return None
        rows = _load_mix_rows(project_db, sequence_id)
        order = _track_order(project_db, sequence_id)
        vols = _load_volume_clips(project_db, sequence_id, order)
        pcm = _mix_pcm(project_db, rows, fps, total_frames, vols, order)
        wav_path = preview_audio_wav_path(project_db, sequence_id)
        sig_path = preview_audio_sig_path(project_db, sequence_id)
        _write_wav(wav_path, pcm)
        _write_sig(sig_path, audio_timeline_signature(
            project_db, sequence_id, fps, total_frames))
        st = _state(project_db, sequence_id)
        st["gen"] += 1
        st["dirty"] = False
        st["stale"] = None
        return wav_path
    except Exception as e:
        # print("build_preview_audio_wav failed")
        # BUILD 135: no longer silent (a locked WAV on Windows / a share
        # makes os.replace fail and Play would run without the new mix)
        print("Preview audio rebuild failed: %s" % e)
        return None


def ensure_preview_audio_wav(project_db, sequence_id, fps, total_frames):
    # Rebuild only when the WAV or signature is missing or the timeline changed.
    return ensure_preview_audio_wav_status(
        project_db, sequence_id, fps, total_frames)[0]


def ensure_preview_audio_wav_status(project_db, sequence_id, fps, total_frames,
                                    on_rebuild=None):
    # BUILD 135: (wav path or None, rebuilt). on_rebuild() runs just before
    # a rebuild (status / wait cursor). The signature is compared on every
    # call (Play), whatever the dirty flag says.
    try:
        if not project_db or sequence_id is None:
            return None, False
        wav_path = preview_audio_wav_path(project_db, sequence_id)
        sig_path = preview_audio_sig_path(project_db, sequence_id)
        want = audio_timeline_signature(project_db, sequence_id, fps, total_frames)
        have = _read_sig(sig_path)
        if os.path.isfile(wav_path) and have == want and want:
            st = _state(project_db, sequence_id)
            st["dirty"] = False
            st["stale"] = None
            return wav_path, False
        if on_rebuild is not None:
            try:
                on_rebuild()
            except Exception:
                pass
        return build_preview_audio_wav(project_db, sequence_id, fps, total_frames), True
    except Exception as e:
        # print("ensure_preview_audio_wav failed")
        print("Preview audio check failed: %s" % e)
        return None, False


# ---------------------------------------------------------------- BUILD 136
# Background mix. A worker thread (no Qt) mixes into a per-job temp file and
# swaps it in only if the job is still the newest one for the sequence, not
# cancelled, and the timeline's signature (re-read on the worker's own
# sqlite connections) still equals the one it mixed. The GUI thread polls
# job.done and calls collect_mix(job), which bumps the reload generation.

_JOBS = {}                     # state key -> newest MixJob
_SWAP_LOCK = threading.Lock()
_TOKENS = [0]


class MixJob(object):
    def __init__(self, project_db, sequence_id, fps, total_frames, sig):
        _TOKENS[0] += 1
        self.token = _TOKENS[0]
        self.key = _state_key(project_db, sequence_id)
        self.project_db = project_db
        self.sequence_id = int(sequence_id)
        self.fps = float(fps)
        self.total_frames = int(total_frames)
        self.sig = sig              # signature this job mixes
        self.cancel = threading.Event()
        self.done = False
        self.status = None          # 'swapped' / 'stale' / 'cancelled' / 'failed'
        self.error = None
        self.collected = False
        self.thread = None


def preview_audio_current(project_db, sequence_id, fps, total_frames):
    # GUI thread: (wav path or None, signature). Path only when the WAV on
    # disk matches the timeline now. A few small queries, no mixing.
    if not project_db or sequence_id is None:
        return None, ""
    want = audio_timeline_signature(project_db, sequence_id, fps, total_frames)
    wav = preview_audio_wav_path(project_db, sequence_id)
    have = _read_sig(preview_audio_sig_path(project_db, sequence_id))
    if want and have == want and os.path.isfile(wav):
        return wav, want
    return None, want


def running_mix(project_db, sequence_id):
    job = _JOBS.get(_state_key(project_db, sequence_id))
    if job is not None and not job.done:
        return job
    return None


def start_mix(project_db, sequence_id, fps, total_frames, sig):
    # GUI thread: reuse a running job that mixes this signature, else cancel
    # it (cooperatively) and start a new one. Returns the job.
    key = _state_key(project_db, sequence_id)
    old = _JOBS.get(key)
    if old is not None and not old.done and old.sig == sig:
        return old
    if old is not None and not old.done:
        old.cancel.set()
    job = MixJob(project_db, sequence_id, fps, total_frames, sig)
    _JOBS[key] = job
    t = threading.Thread(target=_mix_worker, args=(job,),
                         name="PreviewAudioMix-%d" % job.token)
    t.daemon = True             # never blocks app exit
    job.thread = t
    t.start()
    return job


def _mix_worker(job):
    # Worker thread. Only sqlite (own connections, opened here), numpy and
    # files; never Qt.
    tmp = preview_audio_wav_path(job.project_db, job.sequence_id) + ".%d.tmp" % job.token
    try:
        rows = _load_mix_rows(job.project_db, job.sequence_id)
        order = _track_order(job.project_db, job.sequence_id)
        vols = _load_volume_clips(job.project_db, job.sequence_id, order)
        pcm = _mix_pcm(job.project_db, rows, job.fps, job.total_frames, vols,
                       order, cancel=job.cancel)
        if job.cancel.is_set():
            raise MixCancelled()
        _write_wav_file(tmp, pcm)
        with _SWAP_LOCK:
            now = audio_timeline_signature(
                job.project_db, job.sequence_id, job.fps, job.total_frames)
            newest = _JOBS.get(job.key)
            if job.cancel.is_set():
                job.status = "cancelled"
            elif newest is not job and newest is not None and newest.token > job.token:
                job.status = "stale"
            elif now != job.sig:
                job.status = "stale"        # an edit landed during the mix
            else:
                wav = preview_audio_wav_path(job.project_db, job.sequence_id)
                os.replace(tmp, wav)
                _write_sig(preview_audio_sig_path(job.project_db, job.sequence_id),
                           job.sig)
                job.status = "swapped"
    except MixCancelled:
        job.status = "cancelled"
    except Exception as e:
        job.status = "failed"
        job.error = str(e)
    finally:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        job.done = True


def collect_mix(job):
    # GUI thread, once per finished job: a swapped WAV bumps the reload
    # generation (players drop their in-memory PCM) and clears dirty when
    # no edit came after the job's signature. Returns job.status.
    if job is None or not job.done:
        return None
    if job.collected:
        return job.status
    job.collected = True
    if job.status == "swapped":
        st = _STATE.setdefault(job.key, {"gen": 0, "dirty": True, "stale": None})
        st["gen"] += 1
        st["stale"] = None
        st["dirty"] = True      # scrub re-checks the signature once
    elif job.status == "failed":
        print("Preview audio mix failed: %s" % job.error)
    return job.status


def collect_finished_mixes(project_db, sequence_id):
    # GUI thread: collect a finished, uncollected job (e.g. its timeline
    # closed before the poll saw it)
    job = _JOBS.get(_state_key(project_db, sequence_id))
    if job is not None and job.done and not job.collected:
        collect_mix(job)


def cancel_mix(project_db, sequence_id):
    job = running_mix(project_db, sequence_id)
    if job is not None:
        job.cancel.set()
