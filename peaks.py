# peaks.py
# Stored audio waveform peaks for footage. ASCII only.
# data is packed signed 16-bit little-endian (min, max) pairs, one pair per
# bucket, mono sum, PEAKS_PER_SECOND buckets per second.
# Also stores mono 48 kHz 16-bit PCM in a sibling .audiotmp.db (AudioTmp
# table) in the same FFmpeg pass, for later mix. No .wav sidecars.

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

PEAKS_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS peaks (
        footage_id INTEGER PRIMARY KEY,
        peaks_per_second INTEGER NOT NULL,
        duration_seconds REAL NOT NULL,
        data BLOB NOT NULL
    )
"""

AUDIO_TMP_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS AudioTmp (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        originalfilename TEXT NOT NULL,
        audioblob BLOB NOT NULL
    )
"""


def ensure_peaks_table(conn):
    conn.execute(PEAKS_CREATE_SQL)
    conn.commit()


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


def compute_peaks(path):
    # Return (peaks bytes, duration_seconds, pcm_bytes), or None if ffmpeg
    # fails or the file has no audio stream. PCM is mono 48 kHz s16le.
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1",
           "-ar", str(PEAKS_SAMPLE_RATE), "-f", "s16le", "-"]
    flags = 0x08000000 if sys.platform.startswith("win") else 0   # no console window
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=flags)
    except Exception:
        return None
    out = array.array("h")
    pcm = bytearray()
    total = 0
    leftover = b""
    chunk_bytes = SAMPLES_PER_BUCKET * 2 * 1000   # 1000 buckets per read
    try:
        while True:
            buf = proc.stdout.read(chunk_bytes)
            if not buf:
                break
            buf = leftover + buf
            usable = (len(buf) // (SAMPLES_PER_BUCKET * 2)) * SAMPLES_PER_BUCKET * 2
            leftover = buf[usable:]
            if usable:
                # Keep original little-endian PCM for the audiotmp blob
                pcm.extend(buf[:usable])
                s = array.array("h")
                s.frombytes(buf[:usable])
                if sys.byteorder != "little":
                    s.byteswap()
                total += len(s)
                _bucket_peaks(s, out)
        if len(leftover) >= 2:
            raw = leftover[:len(leftover) // 2 * 2]
            pcm.extend(raw)
            s = array.array("h")
            s.frombytes(raw)
            if sys.byteorder != "little":
                s.byteswap()
            total += len(s)
            _bucket_peaks(s, out)
        proc.wait()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return None
    if proc.returncode != 0 or total == 0:
        return None
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes(), total / float(PEAKS_SAMPLE_RATE), bytes(pcm)


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
