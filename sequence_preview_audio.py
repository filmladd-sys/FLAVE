# sequence_preview_audio.py
# Mix sequence audio into {sequence_id}_previewaudio.wav next to the project
# file (same folder as the preview db). Source PCM is AudioTmp in the
# sibling .audiotmp.db. No FFmpeg. ASCII only.

import hashlib
import os
import sqlite3
import wave

try:
    import numpy as np
    _HAS_NP = True
except ImportError:
    np = None
    _HAS_NP = False

from peaks import PEAKS_SAMPLE_RATE, audiotmp_db_path, load_audiotmp

SAMPLE_RATE = PEAKS_SAMPLE_RATE  # 48000


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
    # Rows that can contribute audio: enabled clips on audio or video tracks.
    # Solo: if any of those tracks is solo, only solo tracks.
    conn = sqlite3.connect(project_db)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT t.id, LOWER(COALESCE(t.track_type, '')), "
            "COALESCE(t.enabled, 1), COALESCE(t.solo, 0), "
            "c.footage_id, c.start_frame, c.length_frames, "
            "COALESCE(c.in_frame, 0), COALESCE(c.enabled, 1), "
            "COALESCE(c.solo, 0), f.original_path, f.filename, c.link_id "
            "FROM clips c "
            "JOIN tracks t ON t.id = c.track_id "
            "LEFT JOIN footage f ON f.id = c.footage_id "
            "WHERE t.sequence_id = ? "
            "AND LOWER(COALESCE(t.track_type, '')) IN ('audio', 'video') "
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
         original_path, filename, link_id) in rows:
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
    parts = ["fps=%s" % repr(float(fps)), "frames=%d" % int(total_frames)]
    order = _track_order(project_db, sequence_id)
    for r in rows:
        parts.append("link=%s order=%s" % (r.get("link_id"), order.get(r["track_id"])))
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
    blob = "\n".join(parts).encode("ascii", "replace")
    return hashlib.sha256(blob).hexdigest()


def _read_sig(path):
    try:
        with open(path, "r") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""


def _write_sig(path, sig):
    with open(path, "w") as f:
        f.write(sig + "\n")


def _mix_pcm(project_db, rows, fps, total_frames, vol_clips=None, track_order=None):
    # Return mono s16le bytes for the full sequence length (silence where empty).
    # vol_clips / track_order: linked volume keyframe gain (volume_gain).
    n = max(0, _frame_to_sample(total_frames, fps))
    if n <= 0:
        return b""
    vol_clips = vol_clips or []
    track_order = track_order or {}
    if _HAS_NP:
        acc = np.zeros(n, dtype=np.float64)
        for r in rows:
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
            if gain is None:
                acc[dst0:dst1] += src[src0:src0 + use]
            else:
                acc[dst0:dst1] += src[src0:src0 + use] * gain
        acc = np.clip(acc, -32768, 32767).astype("<i2")
        return acc.tobytes()
    # array-module fallback
    import array
    acc = [0.0] * n
    for r in rows:
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
        for i in range(take):
            d = dst0 + i
            if d >= n:
                break
            g = 1.0 if gain is None else gain[i]
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
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm_bytes)
    os.replace(tmp, path)


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
        return wav_path
    except Exception:
        # print("build_preview_audio_wav failed")
        return None


def ensure_preview_audio_wav(project_db, sequence_id, fps, total_frames):
    # Rebuild only when the WAV or signature is missing or the timeline changed.
    try:
        if not project_db or sequence_id is None:
            return None
        wav_path = preview_audio_wav_path(project_db, sequence_id)
        sig_path = preview_audio_sig_path(project_db, sequence_id)
        want = audio_timeline_signature(project_db, sequence_id, fps, total_frames)
        have = _read_sig(sig_path)
        if os.path.isfile(wav_path) and have == want and want:
            return wav_path
        return build_preview_audio_wav(project_db, sequence_id, fps, total_frames)
    except Exception:
        # print("ensure_preview_audio_wav failed")
        return None
