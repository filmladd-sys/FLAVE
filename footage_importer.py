# footage_importer.py
# Imports a media file into the current project's footage table.
# ASCII only.

import os
import sqlite3
import subprocess


def import_footage(project_db, media_path):
    if not project_db or not os.path.isfile(media_path):
        return None

    filename = os.path.basename(media_path)
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    size_bytes = os.path.getsize(media_path)
    duration = _probe_duration(media_path)

    conn = sqlite3.connect(project_db)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO footage (original_path, filename, size_bytes, file_type, duration)
        VALUES (?, ?, ?, ?, ?)
    """, (media_path, filename, size_bytes, ext, duration))

    new_id = cur.lastrowid
    conn.commit()
    conn.close()

    # Ensure sibling audio cache DB exists before peaks/PCM store
    try:
        from peaks import ensure_audiotmp_db
        ensure_audiotmp_db(project_db)
    except Exception:
        pass

    # Waveform peaks for audio and video files; any failure (no ffmpeg, no
    # audio stream) just skips peaks and never fails the import.
    # store_peaks_for_footage also writes mono PCM into .audiotmp.db.
    try:
        from peaks import store_peaks_for_footage
        store_peaks_for_footage(project_db, new_id, media_path)
    except Exception:
        pass
    return new_id


def _probe_duration(path):
    # Images have no duration
    ext = os.path.splitext(path)[1].lower()
    if ext in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}:
        return 0.0
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return 0.0
