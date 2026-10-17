# peaks.py
# Stored audio waveform peaks for footage. ASCII only.
# data is packed signed 16-bit little-endian (min, max) pairs, one pair per
# bucket, mono sum, PEAKS_PER_SECOND buckets per second.
# Also stores mono 48 kHz 16-bit PCM in a sibling .audiotmp.db (AudioTmp
# table) in the same FFmpeg pass, for later mix. No .wav sidecars.
# On project load, if the .audiotmp.db is missing, it is created and filled
# from existing footage (same mono 48 kHz s16le pass).

import array
import os
import sqlite3
import subprocess
import sys

PEAKS_PER_SECOND = 100
PEAKS_SAMPLE_RATE = 48000
SAMPLES_PER_BUCKET = PEAKS_SAMPLE_RATE // PEAKS_PER_SECOND   # 480

PEAK_AUDIO_EXTS = {"mp3", "wav", "aac", "flac", "m4a", "ogg"}
PEAK_VIDEO_EXTS = {"mov", "mp4", "mkv", "avi", "m4v"}

# peaks table (project db): defined in new_project.py

AUDIO_TMP_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS AudioTmp (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        originalfilename TEXT NOT NULL,
        audioblob BLOB NOT NULL
    )
"""


def ensure_peaks_table(conn):
    # Schema lives in new_project.py (BUILD 78); kept as a no-op for callers
    return None


def audiotmp_db_path(project_db):
    # LaddTest.db -> LaddTest.audiotmp.db in the same folder
    folder = os.path.dirname(os.path.abspath(project_db))
    stem = os.path.splitext(os.path.basename(project_db))[0]
    return os.path.join(folder, stem + ".audiotmp.db")


def ensure_audiotmp_db(project_db):
    # Create the sibling .audiotmp.db and AudioTmp table if missing.
    # Returns the path, or None if project_db is empty.
    if not project_db:
        return None
    path = audiotmp_db_path(project_db)
    conn = sqlite3.connect(path)
    try:
        conn.execute(AUDIO_TMP_CREATE_SQL)
        conn.commit()
    finally:
        conn.close()
    return path


def store_audiotmp(project_db, original_filename, pcm_bytes):
    # One row per original basename; replace any prior row for that name.
    # Never raises; returns True if stored.
    try:
        if not project_db or not original_filename or not pcm_bytes:
            return False
        path = ensure_audiotmp_db(project_db)
        if path is None:
            return False
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "DELETE FROM AudioTmp WHERE originalfilename = ?",
                (original_filename,)
            )
            conn.execute(
                "INSERT INTO AudioTmp (originalfilename, audioblob) VALUES (?, ?)",
                (original_filename, sqlite3.Binary(pcm_bytes))
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def load_audiotmp(project_db, original_filename):
    # Raw mono 48 kHz s16le PCM bytes for basename, or None
    try:
        if not project_db or not original_filename:
            return None
        path = audiotmp_db_path(project_db)
        if not os.path.isfile(path):
            return None
        conn = sqlite3.connect(path)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='AudioTmp'"
            )
            if cur.fetchone() is None:
                return None
            cur.execute(
                "SELECT audioblob FROM AudioTmp WHERE originalfilename = ?",
                (original_filename,)
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    return bytes(row[0])


def wants_peaks(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return ext in PEAK_AUDIO_EXTS or ext in PEAK_VIDEO_EXTS


def _bucket_peaks(samples, out):
    # Append (min, max) per SAMPLES_PER_BUCKET samples of array('h') samples
    n = SAMPLES_PER_BUCKET
    try:
        import numpy as np
        a = np.frombuffer(samples.tobytes(), dtype="<i2")
        full = (len(a) // n) * n
        if full:
            b = a[:full].reshape(-1, n)
            pairs = np.empty((b.shape[0], 2), dtype="<i2")
            pairs[:, 0] = b.min(axis=1)
            pairs[:, 1] = b.max(axis=1)
            out.frombytes(pairs.tobytes())
        if full < len(a):
            rest = a[full:]
            out.append(int(rest.min()))
            out.append(int(rest.max()))
        return
    except ImportError:
        pass
    for i in range(0, len(samples), n):
        chunk = samples[i:i + n]
        out.append(min(chunk))
        out.append(max(chunk))


def extract_mono_pcm(path):
    # Shared import/backfill FFmpeg pass: mono 48 kHz s16le PCM bytes, or None
    # if ffmpeg fails or the file has no audio stream. Never raises.
    # BUILD 118: first audio stream only (-map 0:a:0); ffmpeg's default
    # pick is the "best" stream (most channels), e.g. a second MKV track.
    # No video / subtitle / data streams are read.
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0",
           "-vn", "-sn", "-dn", "-ac", "1",
           "-ar", str(PEAKS_SAMPLE_RATE), "-f", "s16le", "-"]
    flags = 0x08000000 if sys.platform.startswith("win") else 0   # no console window
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=flags)
    except Exception:
        return None
    pcm = bytearray()
    try:
        while True:
            buf = proc.stdout.read(SAMPLES_PER_BUCKET * 2 * 1000)
            if not buf:
                break
            pcm.extend(buf)
        proc.wait()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return None
    if proc.returncode != 0:
        return None
    # Drop a trailing odd byte so the blob is whole samples
    if len(pcm) < 2:
        return None
    if len(pcm) % 2:
        del pcm[-1:]
    return bytes(pcm)


def compute_peaks(path):
    # Return (peaks bytes, duration_seconds, pcm_bytes), or None if ffmpeg
    # fails or the file has no audio stream. PCM is mono 48 kHz s16le.
    pcm = extract_mono_pcm(path)
    if not pcm:
        return None
    s = array.array("h")
    s.frombytes(pcm)
    if sys.byteorder != "little":
        s.byteswap()
    total = len(s)
    if total == 0:
        return None
    out = array.array("h")
    _bucket_peaks(s, out)
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes(), total / float(PEAKS_SAMPLE_RATE), pcm


def store_peaks_for_footage(project_db, footage_id, path):
    # Compute and INSERT OR REPLACE peaks. Never raises; returns True if stored.
    # Mono PCM is stored in the sibling .audiotmp.db as a side effect.
    try:
        if not wants_peaks(path):
            return False
        result = compute_peaks(path)
        if result is None:
            return False
        data, duration, pcm = result
        conn = sqlite3.connect(project_db)
        try:
            ensure_peaks_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO peaks (footage_id, peaks_per_second, "
                "duration_seconds, data) VALUES (?, ?, ?, ?)",
                (footage_id, PEAKS_PER_SECOND, duration, data)
            )
            conn.commit()
        finally:
            conn.close()
        store_audiotmp(project_db, os.path.basename(path), pcm)
        return True
    except Exception:
        return False


def backfill_audiotmp(project_db):
    # Scan project footage and store mono PCM for each audio-capable file.
    # Skips missing paths and files with no audio. Never raises.
    try:
        if not project_db or not os.path.isfile(project_db):
            return
        conn = sqlite3.connect(project_db)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='footage'"
            )
            if cur.fetchone() is None:
                return
            cur.execute("SELECT original_path FROM footage")
            rows = cur.fetchall()
        finally:
            conn.close()
        for (original_path,) in rows:
            try:
                if not original_path or not os.path.isfile(original_path):
                    continue
                if not wants_peaks(original_path):
                    continue
                pcm = extract_mono_pcm(original_path)
                if not pcm:
                    continue
                store_audiotmp(project_db, os.path.basename(original_path), pcm)
            except Exception:
                # print("audiotmp backfill skip: %s" % original_path)
                continue
    except Exception:
        return


def ensure_audiotmp_for_project(project_db):
    # Project load/open: if .audiotmp.db is missing, create it and backfill
    # from existing footage. If it already exists, only ensure the table.
    # Synchronous. Never raises. Returns the audiotmp path or None.
    try:
        if not project_db:
            return None
        path = audiotmp_db_path(project_db)
        missing = not os.path.isfile(path)
        ensure_audiotmp_db(project_db)
        if missing:
            backfill_audiotmp(project_db)
        return path
    except Exception:
        return None


def load_peaks(project_db, footage_id):
    # (peaks_per_second, duration_seconds, array('h') of min,max pairs) or None
    try:
        conn = sqlite3.connect(project_db)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='peaks'")
            if cur.fetchone() is None:
                return None
            cur.execute("SELECT peaks_per_second, duration_seconds, data FROM peaks "
                        "WHERE footage_id = ?", (footage_id,))
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row or not row[2]:
        return None
    values = array.array("h")
    values.frombytes(bytes(row[2])[:len(row[2]) // 2 * 2])
    if sys.byteorder != "little":
        values.byteswap()
    return int(row[0]), float(row[1]), values
