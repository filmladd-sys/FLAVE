# sequence_preview_audio.py
# Mix sequence audio into {sequence_id}_previewaudio.wav next to the project
# file (same folder as the preview db). Source PCM is AudioTmp in the
# sibling .audiotmp.db. No FFmpeg. ASCII only.

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
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
                             {"gen": 0, "dirty": True, "stale": None, "bar": False})


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
            st["bar"] = False       # BUILD 137: audio bar red at once
            st["dirty_frames"] = None   # BUILD 139: ranges unknown until settled
            return
        db = os.path.normcase(os.path.abspath(project_db))
        for key, st in _STATE.items():
            if key[0] == db:
                st["dirty"] = True
                st["stale"] = None
                st["bar"] = False
                st["dirty_frames"] = None
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
        check_audio_source(project_db, sequence_id)
        if not st["dirty"]:
            return True
        if st["stale"] is None:
            wav = preview_audio_wav_path(project_db, sequence_id)
            have = _read_sig(preview_audio_sig_path(project_db, sequence_id))
            want = audio_timeline_signature(project_db, sequence_id, fps, total_frames)
            st["stale"] = not (os.path.isfile(wav) and want and have == want)
            st["bar"] = not st["stale"]
            if not st["stale"]:
                st["dirty"] = False
        return not st["stale"]
    except Exception:
        return False


def check_audio_source(project_db, sequence_id):
    # BUILD 136/137: the sibling .audiotmp.db changed size / mtime since the
    # last look (source re-import) -> dirty. One os.stat. True if changed.
    try:
        st = _state(project_db, sequence_id)
        src = _audiotmp_stamp(project_db)
        if src == st.get("src_stamp"):
            return False
        seen = "src_stamp" in st
        st["src_stamp"] = src
        if seen:
            st["dirty"] = True
            st["stale"] = None
            st["bar"] = False
            st["dirty_frames"] = None   # BUILD 140: re-import, ranges unknown
        return seen
    except Exception:
        return False


def audio_bar_ok(project_db, sequence_id):
    # BUILD 137, paint path: dict lookups only, never a query. Green only
    # when the last check found the WAV current and no mix is running.
    try:
        key = _state_key(project_db, sequence_id)
        st = _STATE.get(key)
        if st is None or not st.get("bar"):
            return False
        job = _JOBS.get(key)
        return not (job is not None and not job.done)
    except Exception:
        return False


def audio_bar_ranges(project_db, sequence_id):
    # BUILD 139, paint path (dict lookups only): None = the whole bar is
    # red (unknown or full remix), [] = green, else inclusive frame ranges
    # still dirty (the rest of the WAV is current and kept)
    if audio_bar_ok(project_db, sequence_id):
        return []
    try:
        st = _STATE.get(_state_key(project_db, sequence_id))
        frames = None if st is None else st.get("dirty_frames")
        # BUILD 140: [] (nothing to remix, e.g. a pure move: a sample copy)
        # is green, not all red
        return None if frames is None else list(frames)
    except Exception:
        return None


def settle_audio_state(project_db, sequence_id, fps, total_frames):
    # BUILD 137, GUI thread: one signature check (a few small queries) to
    # settle the true state after an edit / at open / after a mix. A
    # running mix stays red. Returns the bar state.
    try:
        st = _state(project_db, sequence_id)
        running = running_mix(project_db, sequence_id) is not None
        check_audio_source(project_db, sequence_id)
        wav, sig, inputs = _current(project_db, sequence_id, fps, total_frames)
        ok = wav is not None
        if running:
            # a running mix stays red; BUILD 139: its ranges still settle
            st["bar"] = False
        else:
            st["bar"] = ok
            st["stale"] = not ok
            st["dirty"] = not ok
        # BUILD 139: which frames the next (partial) mix rewrites, for the
        # bar; None = all (no usable layout: full remix)
        st["dirty_frames"] = None
        if not ok and inputs is not None:
            st["dirty_frames"] = _plan_frames(project_db, sequence_id, inputs,
                                              fps, total_frames, sig)
        elif ok and not running and inputs is not None:
            # BUILD 140: a current WAV without a usable layout (made before
            # BUILD 139, or by the synchronous rebuild) gets its layout now,
            # so the next edit is range-based instead of a full remix
            _ensure_layout(project_db, sequence_id, inputs, fps, total_frames, sig)
        return ok and not running
    except Exception:
        return False


def audio_edit_ranges(project_db, sequence_id, fps, total_frames):
    # BUILD 140, GUI thread, at a completed edit (refresh): the bar's dirty
    # frame ranges at once (one signature check + the layout plan, a few
    # small queries), instead of all red until the settle check. Only
    # dirty_frames changes: the dirty / stale / bar flags, the debounce and
    # the auto mix stay as they were.
    try:
        st = _state(project_db, sequence_id)
        inputs = _mix_inputs(project_db, sequence_id)
        sig = _signature_of(inputs, fps, total_frames)
        have = _read_sig(preview_audio_sig_path(project_db, sequence_id))
        if sig and have == sig and os.path.isfile(
                preview_audio_wav_path(project_db, sequence_id)):
            st["dirty_frames"] = []         # the audio did not change
        else:
            st["dirty_frames"] = _plan_frames(project_db, sequence_id, inputs,
                                              fps, total_frames, sig)
        return st["dirty_frames"]
    except Exception:
        return None


def _plan_frames(project_db, sequence_id, inputs, fps, total_frames, sig):
    # BUILD 140: frames the next mix must remix (red), from the layout plan;
    # copied / zeroed samples of a pure move are not red. None = full mix.
    try:
        plan = plan_partial(_read_layout(project_db, sequence_id),
                            _layout_of(inputs, fps, total_frames, sig))
    except Exception:
        return None
    if plan is None:
        return None
    return list(plan["remix_frames"])


def _ensure_layout(project_db, sequence_id, inputs, fps, total_frames, sig):
    # BUILD 140: write the layout of a current WAV when missing / stale.
    # Under the swap lock, re-checked there (no mix can swap meanwhile).
    try:
        if not _HAS_NP or _read_layout(project_db, sequence_id, check_wav=True):
            return False
        with _SWAP_LOCK:
            if running_mix(project_db, sequence_id) is not None:
                return False
            if _read_sig(preview_audio_sig_path(project_db, sequence_id)) != sig:
                return False
            layout = _layout_of(inputs, fps, total_frames, sig)
            with wave.open(preview_audio_wav_path(project_db, sequence_id), "rb") as w:
                if (w.getnchannels() != 1 or w.getsampwidth() != 2
                        or w.getframerate() != SAMPLE_RATE
                        or w.getnframes() != int(layout["n"])):
                    return False
            _write_layout(project_db, sequence_id, layout)
        return True
    except Exception as e:
        print("Preview audio layout write failed: %s" % e)
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
    return _signature_of(_mix_inputs(project_db, sequence_id), fps, total_frames)


def _mix_inputs(project_db, sequence_id):
    # BUILD 139: everything the mix reads, loaded once (the signature, the
    # layout and the worker share it): rows, track order, volume clips,
    # [(basename, ident)]
    rows = _load_mix_rows(project_db, sequence_id)
    order = _track_order(project_db, sequence_id)
    vols = _load_volume_clips(project_db, sequence_id, order)
    idents = _audiotmp_idents(
        project_db, sorted(set(r["basename"] for r in rows if r["basename"])))
    return {"rows": rows, "order": order, "vols": vols, "idents": idents}


def _signature_of(inputs, fps, total_frames):
    # Same text and hash as before BUILD 139 (existing .sig files stay valid)
    rows = inputs["rows"]
    order = inputs["order"]
    parts = ["fps=%s" % repr(float(fps)), "frames=%d" % int(total_frames),
             "mix=audio-tracks-only"]
    for r in rows:
        parts.append("link=%s order=%s gain=%r" % (
            r.get("link_id"), order.get(r["track_id"]), r.get("gain_db", 0.0)))
    for link_id, in_f, out_f, pts, kf_order in inputs["vols"]:
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
    for name, ident in inputs["idents"]:
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
            # BUILD 138: the newest row per name via a join. GROUP BY with
            # length(audioblob) in the select list made SQLite read every
            # blob into the sorter (~0.5 s for a few long sources, on the GUI
            # thread); this form reads only the record headers (same result).
            rows = conn.execute(
                "SELECT a.originalfilename, a.id, length(a.audioblob) FROM AudioTmp a "
                "JOIN (SELECT MAX(id) AS mid FROM AudioTmp WHERE originalfilename "
                "IN (%s) GROUP BY originalfilename) m ON a.id = m.mid"
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


class _MixStale(Exception):
    pass


# BUILD 138: the background mix works in chunks of MIX_CHUNK samples (~5 s
# of 48 kHz audio, a few ms of numpy each) and yields the GIL between them
# with time.sleep(0), so the GUI thread and the video preview worker get it
# every few ms even on long sequences.
MIX_CHUNK = 262144


def _mix_yield():
    if threading.current_thread() is not threading.main_thread():
        time.sleep(0)


def _mix_pcm(project_db, rows, fps, total_frames, vol_clips=None, track_order=None,
             cancel=None, as_array=False):
    # as_array (BUILD 138, numpy only): return the int16 array, not bytes.
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
            _mix_yield()
            if r["length_frames"] <= 0 or not r["basename"]:
                continue
            pcm = load_audiotmp(project_db, r["basename"])
            if not pcm:
                continue
            # BUILD 138: no whole-source float copy (it was most of the mix
            # time); the used span is converted chunk by chunk below
            src = np.frombuffer(pcm, dtype="<i2", count=len(pcm) // 2)
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
            # BUILD 138: MIX_CHUNK samples at a time, yielding the GIL between
            # chunks (same arithmetic per sample as before: identical output)
            for a in range(0, use, MIX_CHUNK):
                b = min(use, a + MIX_CHUNK)
                seg = src[src0 + a:src0 + b].astype(np.float64)
                if gain is None:
                    if lin == 1.0:
                        acc[dst0 + a:dst0 + b] += seg
                    else:
                        acc[dst0 + a:dst0 + b] += seg * lin
                else:
                    acc[dst0 + a:dst0 + b] += seg * (gain[a:b] * lin)
                if cancel is not None and cancel.is_set():
                    raise MixCancelled()
                _mix_yield()
            src = pcm = gain = None
        out = np.empty(n, dtype="<i2")
        for a in range(0, n, MIX_CHUNK):
            b = min(n, a + MIX_CHUNK)
            out[a:b] = np.clip(acc[a:b], -32768, 32767)
            _mix_yield()
        acc = None
        if as_array:
            return out          # BUILD 138: the worker writes it in chunks
        return out.tobytes()
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
        if isinstance(pcm_bytes, (bytes, bytearray)):
            w.writeframes(pcm_bytes)
            return
        # BUILD 138: an int16 array from the worker, written in chunks (no
        # whole-WAV bytes copy holding the GIL), yielding between them
        flat = pcm_bytes.view(np.uint8)
        step = MIX_CHUNK * 2
        for a in range(0, len(flat), step):
            w.writeframesraw(flat[a:a + step].data)
            _mix_yield()


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
        _drop_layout(project_db, sequence_id)   # BUILD 139: no stale base
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
        self.mode = None            # BUILD 139: 'partial' / 'full'



def preview_audio_current(project_db, sequence_id, fps, total_frames):
    # GUI thread: (wav path or None, signature). Path only when the WAV on
    # disk matches the timeline now. A few small queries, no mixing.
    wav, want, _inputs = _current(project_db, sequence_id, fps, total_frames)
    return wav, want


def _current(project_db, sequence_id, fps, total_frames):
    # (wav or None, signature, mix inputs) -- BUILD 139: the inputs are
    # loaded once and reused for the layout plan
    if not project_db or sequence_id is None:
        return None, "", None
    inputs = _mix_inputs(project_db, sequence_id)
    want = _signature_of(inputs, fps, total_frames)
    wav = preview_audio_wav_path(project_db, sequence_id)
    have = _read_sig(preview_audio_sig_path(project_db, sequence_id))
    ok = bool(want and have == want and os.path.isfile(wav))
    try:
        st = _state(project_db, sequence_id)
        st["bar"] = ok    # BUILD 137
        if ok:
            st["dirty_frames"] = None
    except Exception:
        pass
    if ok:
        return wav, want, inputs
    return None, want, inputs


def any_mix_running(exclude=None):
    # BUILD 138: True if a mix runs for any sequence other than exclude
    # ((project_db, sequence_id) or None). For the global auto-mix cap.
    skip = _state_key(*exclude) if exclude else None
    for key, job in list(_JOBS.items()):
        if key != skip and job is not None and not job.done:
            return True
    return False


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
    # BUILD 138: one mix at a time per sequence: the new worker first waits
    # for the cancelled one (it stops within a chunk)
    job.prev = old if (old is not None and not old.done) else None
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
        prev = getattr(job, "prev", None)
        job.prev = None
        if prev is not None and prev.thread is not None:
            prev.thread.join()
        if job.cancel.is_set():
            raise MixCancelled()
        t0 = time.perf_counter()
        inputs = _mix_inputs(job.project_db, job.sequence_id)
        if _signature_of(inputs, job.fps, job.total_frames) != job.sig:
            # BUILD 139: the timeline moved on before the mix started; the
            # inputs read here are what the layout records, so they must be
            # exactly the state of job.sig
            raise _MixStale()
        rows = inputs["rows"]
        order = inputs["order"]
        vols = inputs["vols"]
        layout = _layout_of(inputs, job.fps, job.total_frames, job.sig)
        plan = None
        pcm = None
        if _HAS_NP:
            try:
                plan = plan_partial(_read_layout(job.project_db, job.sequence_id,
                                                 check_wav=True), layout)
            except Exception:
                plan = None
        if plan is not None:
            try:
                pcm = _partial_pcm(job, inputs, plan)
                job.mode = "partial"
            except MixCancelled:
                raise
            except Exception as e:
                print("Preview audio partial mix failed (%s): full mix" % e)
                pcm = None
        if pcm is None:
            pcm = _mix_pcm(job.project_db, rows, job.fps, job.total_frames, vols,
                           order, cancel=job.cancel, as_array=_HAS_NP)
            job.mode = "full"
        if job.cancel.is_set():
            raise MixCancelled()
        _write_wav_file(tmp, pcm)
        if job.mode == "partial":
            print("WORKER audio mix: partial, %d of %d samples remixed, %d copied, "
                  "%.2fs" % (plan["remix_len"], plan["n_new"], plan["copy_len"],
                             time.perf_counter() - t0))
        else:
            print("WORKER audio mix: full, %d samples, %.2fs" % (
                _frame_to_sample(job.total_frames, job.fps),
                time.perf_counter() - t0))
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
                # BUILD 139: layout out first, so a crash mid-swap can never
                # leave a layout describing a different WAV (next mix: full)
                _drop_layout(job.project_db, job.sequence_id)
                os.replace(tmp, wav)
                _write_sig(preview_audio_sig_path(job.project_db, job.sequence_id),
                           job.sig)
                _write_layout(job.project_db, job.sequence_id, layout)
                job.status = "swapped"
    except _MixStale:
        job.status = "stale"
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
    st = _STATE.setdefault(job.key, {"gen": 0, "dirty": True, "stale": None,
                                     "bar": False})
    if job.status == "swapped":
        st["gen"] += 1
        # BUILD 137: green only if still current now (one check)
        settle_audio_state(job.project_db, job.sequence_id, job.fps,
                           job.total_frames)
    else:
        st["bar"] = False
        if job.status == "failed":
            st["dirty_frames"] = None   # BUILD 140: failed: all red
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


# ---------------------------------------------------------------- BUILD 139
# Range-based mix. Next to the WAV, {sequence_id}_previewaudio.layout (JSON)
# records the mix inputs the WAV was built from (rows, volume clips, track
# order, source idents, fps, frames, sample count) and the signature it
# matches. The next mix diffs that layout with the timeline and rewrites
# only the samples that change:
#   copies -- a clip moved alone into silence (no other audio under its old
#             or new samples, no volume keyframes, not cut by the end):
#             its samples are copied from the old WAV, its old samples zeroed
#   remix  -- every other added / removed / changed clip, changed volume
#             clip and new tail: those sample ranges are mixed again from
#             the sources, with the same per-sample arithmetic as the full
#             mix (_mix_pcm), so the result is byte-identical to a full mix
#             and the .sig signature stays exact.
# No usable layout (missing, other signature, WAV size differs), an fps
# change or a track reorder: full mix. (BUILD 139 also fell back to a full
# mix for more than half the sequence to remix; BUILD 140 does not.)

LAYOUT_VERSION = 1
# PARTIAL_MAX_FRACTION = 0.5
# BUILD 140: no full-mix fallback for a large remix: a partial remix of the
# changed ranges is never slower than a full mix, and the rest of the WAV
# (and of the bar) stays as it was
PARTIAL_MAX_FRACTION = 1.0
_LAYOUT_CACHE = {}          # path -> ((size, mtime_ns), layout)


def preview_audio_layout_path(project_db, sequence_id):
    folder = os.path.dirname(os.path.abspath(project_db))
    return os.path.join(folder, "%d_previewaudio.layout" % int(sequence_id))


def _layout_of(inputs, fps, total_frames, sig):
    # JSON-normal layout of the mix inputs (lists, as read back)
    order = inputs["order"]
    inv = dict((i, tid) for tid, i in order.items())
    idents = dict(inputs["idents"])
    rows = [[int(r["track_id"]), int(r["footage_id"]), r["basename"],
             int(r["start_frame"]), int(r["in_frame"]), int(r["length_frames"]),
             r.get("link_id"), float(r.get("gain_db", 0.0) or 0.0),
             idents.get(r["basename"], "")]
            for r in inputs["rows"]]
    vols = [[link_id, int(in_f), int(out_f),
             [[int(f), float(v)] for f, v in pts], inv.get(kf_order)]
            for link_id, in_f, out_f, pts, kf_order in inputs["vols"]]
    tracks = [tid for tid, _i in sorted(order.items(), key=lambda kv: kv[1])]
    layout = {"v": LAYOUT_VERSION, "sig": sig, "fps": float(fps),
              "total": int(total_frames),
              "n": _frame_to_sample(total_frames, fps),
              "rows": rows, "vols": vols, "tracks": tracks}
    return json.loads(json.dumps(layout))


def _write_layout(project_db, sequence_id, layout):
    path = preview_audio_layout_path(project_db, sequence_id)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(layout, f)
    os.replace(tmp, path)


def _drop_layout(project_db, sequence_id):
    try:
        os.remove(preview_audio_layout_path(project_db, sequence_id))
    except OSError:
        pass


def _read_layout(project_db, sequence_id, check_wav=False):
    # The layout if it describes the WAV on disk now (its signature is the
    # .sig file's; check_wav: the WAV holds exactly n mono 16-bit samples),
    # else None. Parsed files are cached by size / mtime.
    path = preview_audio_layout_path(project_db, sequence_id)
    try:
        stt = os.stat(path)
    except OSError:
        return None
    stamp = (stt.st_size, stt.st_mtime_ns)
    hit = _LAYOUT_CACHE.get(path)
    if hit is not None and hit[0] == stamp:
        layout = hit[1]
    else:
        try:
            with open(path, "r") as f:
                layout = json.load(f)
        except Exception:
            return None
        _LAYOUT_CACHE[path] = (stamp, layout)
    if not isinstance(layout, dict) or layout.get("v") != LAYOUT_VERSION:
        return None
    if layout.get("sig") != _read_sig(preview_audio_sig_path(project_db, sequence_id)):
        return None
    if check_wav:
        try:
            with wave.open(preview_audio_wav_path(project_db, sequence_id), "rb") as w:
                if (w.getnchannels() != 1 or w.getsampwidth() != 2
                        or w.getframerate() != SAMPLE_RATE
                        or w.getnframes() != int(layout.get("n", -1))):
                    return None
        except Exception:
            return None
    return layout


def _ident_samples(ident):
    # "rowid:bytes" -> source length in samples, else None
    try:
        return int(str(ident).split(":")[1]) // 2
    except Exception:
        return None


def _footprint(row, fps, n):
    # (lo, hi, exact_hi, whole): lo..hi = samples the row may write
    # (superset, for remix); exact_hi = last written sample + 1 when the
    # source length is known (else None); whole = not cut by the end
    start, in_f, length, ident = row[3], row[4], row[5], row[8]
    dst0 = _frame_to_sample(start, fps)
    count = _frame_to_sample(length, fps)
    lo, hi = dst0, min(n, dst0 + max(0, count))
    slen = _ident_samples(ident)
    if slen is None:
        return lo, hi, None, False
    take = min(count, slen - _frame_to_sample(in_f, fps))
    if take <= 0:
        return lo, hi, lo, True
    return lo, hi, min(n, dst0 + take), dst0 + take <= n


def _merge_ranges(ranges, n):
    out = []
    for a, b in sorted((max(0, int(a)), min(int(n), int(b))) for a, b in ranges):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def plan_partial(old, new):
    # Sample-range plan turning the WAV of layout old into the WAV of layout
    # new, or None when a full mix is needed. See the section comment.
    if not old or not new or old.get("v") != LAYOUT_VERSION:
        return None
    if float(old["fps"]) != float(new["fps"]):
        return None
    fps = float(new["fps"])
    n_old, n_new = int(old["n"]), int(new["n"])
    common = set(old["tracks"]) & set(new["tracks"])
    if ([t for t in old["tracks"] if t in common]
            != [t for t in new["tracks"] if t in common]):
        return None         # reorder: the per-sample sum order may change
    old_rows = [tuple(r) for r in old["rows"]]
    new_rows = [tuple(r) for r in new["rows"]]
    removed = _multiset_minus(old_rows, new_rows)
    added = _multiset_minus(new_rows, old_rows)
    old_v = [json.dumps(v) for v in old["vols"]]
    new_v = [json.dumps(v) for v in new["vols"]]
    vol_changed = ([json.loads(v) for v in _multiset_minus(old_v, new_v)]
                   + [json.loads(v) for v in _multiset_minus(new_v, old_v)])
    vol_links = set(v[0] for v in old["vols"]) | set(v[0] for v in new["vols"])

    def alone(row, rows, n, lo, hi):
        # no other row of rows (one instance of row skipped) touches lo..hi
        skipped = False
        for r in rows:
            if r == row and not skipped:
                skipped = True
                continue
            a, b, _e, _w = _footprint(r, fps, n)
            if a < hi and b > lo:
                return False
        return True

    copies, zero, remix = [], [], []
    used = set()
    for r in removed:
        o_lo, o_hi, o_ex, o_whole = _footprint(r, fps, n_old)
        match = None
        for j, a in enumerate(added):
            if j in used:
                continue
            # same source, in point, length, link, gain, ident; start (and
            # track) may differ
            if (a[1], a[2], a[4], a[5], a[6], a[7], a[8]) == (
                    r[1], r[2], r[4], r[5], r[6], r[7], r[8]):
                match = j
                break
        if match is not None:
            a = added[match]
            n_lo, n_hi, n_ex, n_whole = _footprint(a, fps, n_new)
            if (o_ex is not None and n_ex is not None and o_whole and n_whole
                    and o_ex - o_lo == n_ex - n_lo and o_ex > o_lo
                    and (r[6] is None or r[6] not in vol_links)
                    and alone(r, old_rows, n_old, o_lo, o_ex)
                    and alone(a, new_rows, n_new, n_lo, n_ex)):
                used.add(match)
                copies.append((o_lo, n_lo, o_ex - o_lo))
                zero.append((o_lo, o_ex))
                continue
        remix.append((o_lo, o_hi))
    for j, a in enumerate(added):
        if j in used:
            continue
        n_lo, n_hi, _e, _w = _footprint(a, fps, n_new)
        remix.append((n_lo, n_hi))
    for v in vol_changed:
        remix.append((_frame_to_sample(v[1], fps) - 2,
                      _frame_to_sample(v[2], fps) + 2))
    if n_new > n_old:
        remix.append((n_old, n_new))
    remix = _merge_ranges(remix, n_new)
    zero = _merge_ranges(zero, n_new)
    remix_len = sum(b - a for a, b in remix)
    if remix_len > PARTIAL_MAX_FRACTION * n_new:
        return None
    touched = _merge_ranges(remix + zero + [(d, d + ln) for _s, d, ln in copies],
                            n_new)
    return {"n_old": n_old, "n_new": n_new, "copies": copies, "zero": zero,
            "remix": remix, "remix_len": remix_len,
            "copy_len": sum(ln for _s, _d, ln in copies),
            "frames": _sample_frames(touched, fps),
            # BUILD 140: the bar's red = remixed ranges only
            "remix_frames": _sample_frames(remix, fps)}


def _sample_frames(ranges, fps):
    # [a, b) sample ranges -> inclusive frame ranges covering them
    per = fps / float(SAMPLE_RATE)
    frames = []
    for a, b in ranges:
        f0 = int(a * per)
        f1 = int(math.ceil(b * per)) - 1
        frames.append((f0, max(f0, f1)))
    return frames


def _multiset_minus(a, b):
    # Items of list a not matched one-for-one in list b (a's order)
    left = {}
    for x in b:
        left[x] = left.get(x, 0) + 1
    out = []
    for x in a:
        if left.get(x, 0) > 0:
            left[x] -= 1
        else:
            out.append(x)
    return out


def _read_wav_array(path, n):
    # The WAV's n samples as int16, read in chunks (yields between them)
    out = np.empty(n, dtype="<i2")
    pos = 0
    with wave.open(path, "rb") as w:
        if w.getnframes() != n:
            raise ValueError("WAV length changed")
        while pos < n:
            data = w.readframes(min(MIX_CHUNK, n - pos))
            if not data:
                break
            k = len(data) // 2
            out[pos:pos + k] = np.frombuffer(data, dtype="<i2", count=k)
            pos += k
            _mix_yield()
    if pos != n:
        raise ValueError("WAV short read")
    return out


def _partial_pcm(job, inputs, plan):
    # Old WAV + plan -> new samples (int16 array), see plan_partial
    old = _read_wav_array(preview_audio_wav_path(job.project_db, job.sequence_id),
                          plan["n_old"])
    n_new = plan["n_new"]
    if n_new == plan["n_old"]:
        out = old
    else:
        out = np.zeros(n_new, dtype="<i2")
        m = min(n_new, plan["n_old"])
        out[:m] = old[:m]
    segs = [(d, old[s:s + ln].copy()) for s, d, ln in plan["copies"]]
    for a, b in plan["zero"]:
        out[a:b] = 0
    for d, seg in segs:
        out[d:d + len(seg)] = seg
    if job.cancel.is_set():
        raise MixCancelled()
    _mix_ranges(job.project_db, inputs["rows"], job.fps, n_new, inputs["vols"],
                inputs["order"], plan["remix"], out, cancel=job.cancel)
    return out


def _mix_ranges(project_db, rows, fps, n, vol_clips, track_order, ranges, out,
                cancel=None):
    # Mix only sample ranges [a, b) into out (int16, n samples). Same
    # per-sample arithmetic and row order as _mix_pcm (numpy path), so each
    # sample equals the full mix. A row's source is read only if it
    # reaches a range.
    if not ranges:
        return
    vol_clips = vol_clips or []
    track_order = track_order or {}
    accs = [np.zeros(b - a, dtype=np.float64) for a, b in ranges]
    for r in rows:
        if cancel is not None and cancel.is_set():
            raise MixCancelled()
        _mix_yield()
        if r["length_frames"] <= 0 or not r["basename"]:
            continue
        dst0 = _frame_to_sample(r["start_frame"], fps)
        count = _frame_to_sample(r["length_frames"], fps)
        if count <= 0:
            continue
        reach = min(n, dst0 + count)
        hits = [i for i, (a, b) in enumerate(ranges) if a < reach and b > dst0]
        if not hits:
            continue
        pcm = load_audiotmp(project_db, r["basename"])
        if not pcm:
            continue
        src = np.frombuffer(pcm, dtype="<i2", count=len(pcm) // 2)
        src0 = _frame_to_sample(r["in_frame"], fps)
        if src0 >= len(src):
            continue
        take = min(count, len(src) - src0)
        if take <= 0:
            continue
        dst1 = min(n, dst0 + take)
        if dst0 >= n or dst1 <= dst0:
            continue
        lin = db_to_linear(r.get("gain_db", 0.0))
        for i in hits:
            a, b = ranges[i]
            p, q = max(a, dst0), min(b, dst1)
            if q <= p:
                continue
            gain = volume_gain(vol_clips, r.get("link_id"),
                               track_order.get(r["track_id"]), p, q - p, fps)
            acc = accs[i]
            s0 = src0 + (p - dst0)
            for c0 in range(0, q - p, MIX_CHUNK):
                c1 = min(q - p, c0 + MIX_CHUNK)
                seg = src[s0 + c0:s0 + c1].astype(np.float64)
                tgt = acc[p - a + c0:p - a + c1]
                if gain is None:
                    if lin == 1.0:
                        tgt += seg
                    else:
                        tgt += seg * lin
                else:
                    tgt += seg * (gain[c0:c1] * lin)
                if cancel is not None and cancel.is_set():
                    raise MixCancelled()
                _mix_yield()
        src = pcm = None
    for i, (a, b) in enumerate(ranges):
        acc = accs[i]
        for c0 in range(0, b - a, MIX_CHUNK):
            c1 = min(b - a, c0 + MIX_CHUNK)
            out[a + c0:a + c1] = np.clip(acc[c0:c1], -32768, 32767)
            _mix_yield()
        accs[i] = None
