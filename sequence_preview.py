# sequence_preview.py
# Preview cache database for Ladd Video Editor: one JPEG per sequence frame.
# File lives next to the project file as {ProjectName}_{sequence_id}_Preview.db
# (BUILD 223, temp_file_path).
# No decoder, no worker, no UI. ASCII only.

import os
import sqlite3
import threading


def project_name(project_path):
    # ProjectName: the project db file name without its extension
    # (C:\Videos\test7.db -> test7)
    return os.path.splitext(os.path.basename(os.path.abspath(project_path)))[0]


def temp_file_path(project_path, kind, ext, sequence_id=None):
    # BUILD 223: the one naming rule for FLAVE's temp / cache files next to
    # the project file. ProjectName = the project db file name without its
    # extension. Per sequence: {ProjectName}_{sequence_id}_{Kind}.{ext};
    # per project (sequence_id None): {ProjectName}_{Kind}.{ext}.
    # C:\Videos\test7.db, 3, "Preview", "db" -> C:\Videos\test7_3_Preview.db
    folder = os.path.dirname(os.path.abspath(project_path))
    name = project_name(project_path)
    if sequence_id is not None:
        name = "%s_%d" % (name, int(sequence_id))
    return os.path.join(folder, "%s_%s.%s" % (name, kind, ext))


def preview_db_path(project_path, sequence_id):
    # C:\Videos\test7.db + sequence 3 -> C:\Videos\test7_3_Preview.db
    return temp_file_path(project_path, "Preview", "db", sequence_id)


def _connect(project_path, sequence_id):
    # Open WAL; create the table if the file or table is missing
    conn = sqlite3.connect(preview_db_path(project_path, sequence_id))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS preview (
            frame INTEGER PRIMARY KEY,
            dirty INTEGER NOT NULL DEFAULT 1,
            jpeg BLOB
        )
    """)
    return conn


# In-memory clean frames per preview file, so the ruler paint never opens
# the database. Loaded once (one query) from the existing file, then kept
# current by mark_clean. Shared by the GUI and the preview worker.
_CLEAN = {}
_CLEAN_LOCK = threading.Lock()


def clean_set(project_path, sequence_id):
    # The cached set of clean frames (do not modify the returned set)
    key = (os.path.abspath(project_path), int(sequence_id))
    with _CLEAN_LOCK:
        s = _CLEAN.get(key)
    if s is None:
        s = set(FrameCleanRange(project_path, sequence_id, 0, 2 ** 62))
        with _CLEAN_LOCK:
            s = _CLEAN.setdefault(key, s)
    return s


def mark_clean(project_path, sequence_id, frame_num):
    s = clean_set(project_path, sequence_id)
    with _CLEAN_LOCK:
        s.add(int(frame_num))


# Dirty marks from the GUI: the clean set changes at once; the database
# UPDATE is queued and run on the preview worker thread (apply_dirty_queue),
# so the GUI never waits on the worker's open write. Each mark gets a
# sequence number so a batch can drop frames it built before the mark.
_DIRTY_SEQ = [0]
_DIRTY_LOG = []     # (seq, key, first, last), newest last, kept short
_DIRTY_QUEUE = []   # (project_path, sequence_id, first, last[, delta, seq])


def FramesMarkDirty(project_path, sequence_id, ranges):
    # Mark each (first, last) frame range dirty (inclusive). GUI thread.
    key = (os.path.abspath(project_path), int(sequence_id))
    s = clean_set(project_path, sequence_id)
    with _CLEAN_LOCK:
        for first, last in ranges:
            first, last = int(first), int(last)
            if last < first:
                continue
            for f in [f for f in s if first <= f <= last]:
                s.discard(f)
            _DIRTY_SEQ[0] += 1
            _DIRTY_LOG.append((_DIRTY_SEQ[0], key, first, last))
            _DIRTY_QUEUE.append((project_path, int(sequence_id), first, last))
        del _DIRTY_LOG[:-256]


def FramesShift(project_path, sequence_id, first, last, delta):
    # Linked group moved by delta with nothing else under it: the clean
    # frames of first..last move to first+delta..last+delta. GUI thread.
    # Both ranges leave the clean set now (and are logged dirty so an
    # in-flight worker batch keeps them dirty); the worker thread moves the
    # rows (apply_dirty_queue) and re-adds the moved frames that are still
    # clean.
    key = (os.path.abspath(project_path), int(sequence_id))
    first, last, delta = int(first), int(last), int(delta)
    if last < first:
        return
    s = clean_set(project_path, sequence_id)
    with _CLEAN_LOCK:
        for a, b in ((first, last), (first + delta, last + delta)):
            for f in [f for f in s if a <= f <= b]:
                s.discard(f)
            _DIRTY_SEQ[0] += 1
            _DIRTY_LOG.append((_DIRTY_SEQ[0], key, a, b))
        _DIRTY_QUEUE.append((project_path, int(sequence_id), first, last,
                             delta, _DIRTY_SEQ[0]))
        del _DIRTY_LOG[:-256]


def FramesRelocate(project_path, sequence_id, relocs):
    # BUILD 146: mile-marker relocations [(first, last, delta)] (a pure move
    # proved span by span): the clean rows of first..last are copied to
    # first+delta..last+delta. GUI thread. Only the targets leave the clean
    # set now (logged dirty, so an in-flight worker batch keeps them dirty);
    # the sources keep their rows (the marker diff marks them if their own
    # picture changed, queued after this). The worker reads every source
    # before writing any target (one transaction), so swapped blocks copy
    # correctly.
    key = (os.path.abspath(project_path), int(sequence_id))
    relocs = [(int(a), int(b), int(d)) for a, b, d in relocs if int(b) >= int(a)]
    if not relocs:
        return
    s = clean_set(project_path, sequence_id)
    with _CLEAN_LOCK:
        for a, b, d in relocs:
            for f in [f for f in s if a + d <= f <= b + d]:
                s.discard(f)
            _DIRTY_SEQ[0] += 1
            _DIRTY_LOG.append((_DIRTY_SEQ[0], key, a + d, b + d))
        _DIRTY_QUEUE.append((project_path, int(sequence_id), None, None,
                             ("relocate", relocs), _DIRTY_SEQ[0]))
        del _DIRTY_LOG[:-256]


def _apply_relocate(project_path, sequence_id, relocs, seq):
    # Worker thread: FramesRelocate's copies in one transaction
    key = (os.path.abspath(project_path), int(sequence_id))
    conn = _connect(project_path, sequence_id)
    moved = []
    try:
        got = []
        for a, b, d in relocs:
            rows = conn.execute(
                "SELECT frame, jpeg FROM preview WHERE frame BETWEEN ? AND ? "
                "AND dirty = 0 AND jpeg IS NOT NULL", (a, b)).fetchall()
            got.append((d, rows))
        for a, b, d in relocs:
            conn.execute("UPDATE preview SET dirty = 1 WHERE frame BETWEEN ? AND ?",
                         (a + d, b + d))
        for d, rows in got:
            for frame, jpeg in rows:
                nf = int(frame) + d
                if nf < 0 or jpeg is None:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO preview (frame, dirty, jpeg) VALUES (?, 0, ?)",
                    (nf, jpeg))
                moved.append(nf)
        conn.commit()
    finally:
        conn.close()
    s = clean_set(project_path, sequence_id)
    with _CLEAN_LOCK:
        for a, b, d in relocs:
            for f in [f for f in s if a + d <= f <= b + d]:
                s.discard(f)
        s.update(f for f in moved if not _dirty_since(key, seq, f))


def _apply_shift(project_path, sequence_id, first, last, delta, seq):
    # Worker thread: move clean rows first..last by delta in one transaction
    key = (os.path.abspath(project_path), int(sequence_id))
    conn = _connect(project_path, sequence_id)
    try:
        rows = conn.execute(
            "SELECT frame, jpeg FROM preview WHERE frame BETWEEN ? AND ? "
            "AND dirty = 0 AND jpeg IS NOT NULL", (first, last)).fetchall()
        conn.execute("UPDATE preview SET dirty = 1 WHERE frame BETWEEN ? AND ?",
                     (first, last))
        conn.execute("UPDATE preview SET dirty = 1 WHERE frame BETWEEN ? AND ?",
                     (first + delta, last + delta))
        moved = []
        for frame, jpeg in rows:
            nf = int(frame) + delta
            if nf < 0 or jpeg is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO preview (frame, dirty, jpeg) VALUES (?, 0, ?)",
                (nf, jpeg))
            moved.append(nf)
        conn.commit()
    finally:
        conn.close()
    s = clean_set(project_path, sequence_id)
    with _CLEAN_LOCK:
        # Match the database: both ranges dirty except the moved frames.
        # A frame marked dirty after the shift stays out (its queued mark
        # writes the database next).
        for a, b in ((first, last), (first + delta, last + delta)):
            for f in [f for f in s if a <= f <= b]:
                s.discard(f)
        s.update(f for f in moved if not _dirty_since(key, seq, f))


def apply_dirty_queue():
    # Write queued dirty marks (preview worker thread). A missing preview
    # file is not created: it has no clean rows anyway.
    with _CLEAN_LOCK:
        items = list(_DIRTY_QUEUE)
        del _DIRTY_QUEUE[:]
    for item in items:
        project_path, sequence_id, first, last = item[:4]
        if not os.path.isfile(preview_db_path(project_path, sequence_id)):
            continue
        if len(item) > 4 and isinstance(item[4], tuple) and item[4][0] == "relocate":
            # BUILD 146: (project, sequence, None, None, ("relocate", relocs),
            # seq). On a failure the targets are already out of the clean
            # set, so the worker rebuilds them.
            try:
                _apply_relocate(project_path, sequence_id, item[4][1], item[5])
            except Exception as e:
                print(f"Preview relocate failed: {e}")
            continue
        if len(item) > 4:
            # (project, sequence, first, last, delta, seq): row shift. On a
            # failure both ranges are already out of the clean set, so the
            # worker rebuilds them.
            try:
                _apply_shift(project_path, sequence_id, first, last,
                             item[4], item[5])
            except Exception as e:
                print(f"Preview shift failed: {e}")
            continue
        conn = _connect(project_path, sequence_id)
        try:
            conn.execute("UPDATE preview SET dirty = 1 WHERE frame BETWEEN ? AND ?",
                         (first, last))
            conn.commit()
        finally:
            conn.close()


def dirty_queue_pending():
    # True if GUI dirty marks or shifts wait for apply_dirty_queue
    with _CLEAN_LOCK:
        return bool(_DIRTY_QUEUE)


def dirty_seq():
    # Current dirty mark number. Read it before building a frame so a mark
    # made while the frame was built keeps that frame dirty.
    with _CLEAN_LOCK:
        return _DIRTY_SEQ[0]


def _dirty_since(key, seq, frame):
    # True if frame was marked dirty after mark number seq (lock held)
    for s_, k, first, last in reversed(_DIRTY_LOG):
        if s_ <= seq:
            break
        if k == key and first <= frame <= last:
            return True
    return False


def FrameRetrieve(project_path, sequence_id, frame_num):
    # JPEG bytes if the row exists and is clean, else None
    conn = _connect(project_path, sequence_id)
    try:
        row = conn.execute("SELECT dirty, jpeg FROM preview WHERE frame = ?",
                           (int(frame_num),)).fetchone()
    finally:
        conn.close()
    if row is None or row[0] != 0 or row[1] is None:
        return None
    return bytes(row[1])


def FrameSave(project_path, sequence_id, frame_num, jpeg_blob, dirty):
    # Insert or replace one frame; dirty 0 = clean, 1 = dirty
    conn = _connect(project_path, sequence_id)
    try:
        conn.execute("INSERT OR REPLACE INTO preview (frame, dirty, jpeg) VALUES (?, ?, ?)",
                     (int(frame_num), 1 if dirty else 0,
                      sqlite3.Binary(jpeg_blob) if jpeg_blob is not None else None))
        conn.commit()
    finally:
        conn.close()
    s = clean_set(project_path, sequence_id)
    with _CLEAN_LOCK:
        if dirty:
            s.discard(int(frame_num))
        else:
            s.add(int(frame_num))


def FrameCleanRange(project_path, sequence_id, first, last):
    # Sorted list of clean (dirty = 0) frames in first..last, one query.
    # Read only: a missing preview file means no clean frames (not created).
    path = preview_db_path(project_path, sequence_id)
    if not os.path.isfile(path):
        return []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT frame FROM preview WHERE frame BETWEEN ? AND ? AND dirty = 0 "
            "AND jpeg IS NOT NULL ORDER BY frame", (int(first), int(last))).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [r[0] for r in rows]


class PreviewBatch(object):
    # One open transaction for many clean frames; commit() writes them at
    # once and only then adds them to the in-memory clean set (bar moves).
    # Used by the preview worker on its own thread.

    def __init__(self, project_path, sequence_id):
        self.project_path = project_path
        self.sequence_id = sequence_id
        self.conn = None
        self.pending = []
        self.seqs = []       # dirty mark number when each pending frame was built

    def add(self, frame_num, jpeg_blob, seq=None):
        # seq: dirty mark number from before the frame was built (see
        # dirty_seq); default is the current number
        if self.conn is None:
            self.conn = _connect(self.project_path, self.sequence_id)
        self.conn.execute(
            "INSERT OR REPLACE INTO preview (frame, dirty, jpeg) VALUES (?, 0, ?)",
            (int(frame_num), sqlite3.Binary(jpeg_blob)))
        self.pending.append(int(frame_num))
        with _CLEAN_LOCK:
            self.seqs.append(_DIRTY_SEQ[0] if seq is None else int(seq))

    def commit(self):
        # Seconds spent committing (0.0 if nothing pending)
        if self.conn is None or not self.pending:
            return 0.0
        import time
        key = (os.path.abspath(self.project_path), int(self.sequence_id))
        t0 = time.perf_counter()
        with _CLEAN_LOCK:
            stale = [f for f, q in zip(self.pending, self.seqs)
                     if _dirty_since(key, q, f)]
        # Built before an edit marked them dirty: keep them dirty
        for f in stale:
            self.conn.execute("UPDATE preview SET dirty = 1 WHERE frame = ?", (f,))
        self.conn.commit()
        dt = time.perf_counter() - t0
        s = clean_set(self.project_path, self.sequence_id)
        stale = set(stale)
        with _CLEAN_LOCK:
            s.update(f for f in self.pending if f not in stale)
        self.pending = []
        self.seqs = []
        apply_dirty_queue()
        return dt

    def close(self):
        try:
            self.commit()
        finally:
            if self.conn is not None:
                self.conn.close()
                self.conn = None
