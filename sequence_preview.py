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


# Preview file layout (BUILD 251):
#   preview (frame INTEGER PRIMARY KEY, jpeg BLOB): one JPEG per frame
#   DirtyImage (frame INTEGER PRIMARY KEY): frames whose JPEG is stale
#   PreviewInfo (key TEXT PRIMARY KEY, value TEXT): "ImageSize" (BUILD 255,
#     see sync_image_size)
# A frame is clean when its preview row has a JPEG and it is not in
# DirtyImage. The dirty list is its own small table so marking frames dirty
# never rewrites the rows that hold the JPEGs. (A future DirtyAudio table
# would follow the same pattern.) Files from before BUILD 251 keep an unused
# preview.dirty column; _connect copies its marks into DirtyImage once.
CLEAN_SQL = ("NOT EXISTS (SELECT 1 FROM DirtyImage d "
             "WHERE d.frame = preview.frame)")


# Journal (BUILD 257): the preview file is a rebuildable cache, so it is
# written for the fewest bytes, not for crash safety. journal_mode=MEMORY
# keeps the rollback journal in RAM (a failed transaction still rolls
# back) and every JPEG page goes to the file once, at commit. WAL wrote
# each page twice (the -wal log, then the checkpoint into the file), and a
# DELETE / TRUNCATE journal copies the old pages of every overwritten frame
# to disk first. synchronous=OFF: no flush waits (the OS writes the pages
# out). The cost: a crash or power loss in the middle of a commit can leave
# a damaged file; _connect then discards it and every frame is rebuilt.
# Without WAL a commit briefly locks readers out; every connection waits
# up to its busy timeout (short on the GUI thread) and treats a timeout as
# "not available now". A commit only locks at its end: the writer's page
# cache (cache_size) is large enough that a card pass's frames do not
# spill to the file early. The default 4096-byte page is kept: a larger
# page wastes more of each JPEG's last page than it saves in page links.
PREVIEW_BUSY_SEC = 10.0         # worker / writer / player reader threads
PREVIEW_GUI_BUSY_SEC = 1.0      # GUI thread: a short wait, then a miss
PREVIEW_CACHE_KB = 131072       # page cache per connection, at most (KiB)


def _busy_sec():
    # Busy timeout for a connection opened on the calling thread
    if threading.current_thread() is threading.main_thread():
        return PREVIEW_GUI_BUSY_SEC
    return PREVIEW_BUSY_SEC


def _is_damaged(e):
    # sqlite3 error that means the file itself is unreadable (not a
    # database, malformed), as opposed to busy / locked / cannot open
    return (isinstance(e, sqlite3.DatabaseError)
            and not isinstance(e, sqlite3.OperationalError))


def _discard(project_path, sequence_id, why):
    # Delete a damaged preview file (and its -wal / -shm / -journal) so the
    # next open starts an empty one: every frame is then missing (not
    # clean) and is rebuilt. Raises sqlite3.DatabaseError when the file
    # cannot be removed now (still open elsewhere); callers treat that as
    # "no preview" and the next open tries again.
    path = preview_db_path(project_path, sequence_id)
    print("Preview file %s unreadable (%s); discarding it, every frame "
          "will be rebuilt" % (path, why))
    for ext in ("", "-wal", "-shm", "-journal"):
        try:
            if os.path.exists(path + ext):
                os.remove(path + ext)
        except OSError as e:
            raise sqlite3.DatabaseError("cannot discard %s: %s" % (path + ext, e))
    key = (os.path.abspath(project_path), int(sequence_id))
    with _CLEAN_LOCK:
        s = _CLEAN.get(key)
        if s is not None:
            s.clear()
    _IMAGE_SIZE.pop(key, None)


def _open(path, timeout):
    # One connection: journal mode, pragmas, tables (see _connect)
    conn = sqlite3.connect(path, timeout=timeout)
    try:
        # A file still in WAL (written before BUILD 257) leaves WAL here:
        # SQLite checkpoints the -wal into the file and deletes it. That
        # needs no other connection on the file, so it is tried without
        # waiting; when busy, this connection simply works in WAL and a
        # later open switches. On a file already out of WAL the mode is per
        # connection, so every writer sets it.
        conn.execute("PRAGMA busy_timeout=0")
        try:
            conn.execute("PRAGMA journal_mode=MEMORY").fetchone()
        except sqlite3.OperationalError:
            pass
        conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-%d" % PREVIEW_CACHE_KB)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS preview (
                frame INTEGER PRIMARY KEY,
                jpeg BLOB
            )
        """)
        has_dirty_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'DirtyImage'").fetchone() is not None
        if not has_dirty_table:
            conn.execute("CREATE TABLE IF NOT EXISTS DirtyImage "
                         "(frame INTEGER PRIMARY KEY)")
            cols = [r[1] for r in conn.execute("PRAGMA table_info(preview)")]
            if "dirty" in cols:
                # Older file: its dirty marks move to DirtyImage once
                conn.execute("INSERT OR IGNORE INTO DirtyImage (frame) "
                             "SELECT frame FROM preview WHERE dirty != 0")
            conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def _connect(project_path, sequence_id, timeout=None):
    # Open the preview file (created if missing) with the tables in place.
    # timeout: busy wait in seconds (default by thread, see _busy_sec). A
    # damaged file is discarded and a new one opened; busy / locked errors
    # go to the caller.
    path = preview_db_path(project_path, sequence_id)
    timeout = _busy_sec() if timeout is None else timeout
    try:
        return _open(path, timeout)
    except sqlite3.DatabaseError as e:
        if not _is_damaged(e):
            raise
        _discard(project_path, sequence_id, e)
        return _open(path, timeout)


# Preview Size (Project Settings > Project): the preview JPEGs are stored
# at this percent of the sequence size, so a project can trade picture
# detail for smaller files and faster preview writes. Kept in the project
# db settings table; a missing or bad value is 100 (full size).
PREVIEW_SIZE_KEY = "PreviewSize"
PREVIEW_SIZE_CHOICES = (100, 75, 50, 25)
_PREVIEW_PCT = {}       # project path (normcase abspath) -> percent, read
                        # once, kept current


def _clean_percent(value):
    # One of PREVIEW_SIZE_CHOICES, else 100
    try:
        pct = int(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 100
    return pct if pct in PREVIEW_SIZE_CHOICES else 100


def preview_size_percent(project_path):
    # The project's Preview Size percent (cached per project; the Project
    # Settings save updates the cache through set_preview_size_percent)
    if not project_path:
        return 100
    key = os.path.normcase(os.path.abspath(project_path))
    pct = _PREVIEW_PCT.get(key)
    if pct is None:
        value = None
        try:
            conn = sqlite3.connect(project_path)
            try:
                row = conn.execute("SELECT value FROM settings WHERE key = ?",
                                   (PREVIEW_SIZE_KEY,)).fetchone()
                value = row[0] if row else None
            finally:
                conn.close()
        except sqlite3.Error:
            value = None
        pct = _clean_percent(value) if value is not None else 100
        _PREVIEW_PCT[key] = pct
    return pct


def set_preview_size_percent(project_path, pct):
    # Store the Preview Size percent in the project db, then the cache.
    # Returns the percent stored.
    pct = _clean_percent(pct)
    conn = sqlite3.connect(project_path)
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                     (PREVIEW_SIZE_KEY, str(pct)))
        conn.commit()
    finally:
        conn.close()
    _PREVIEW_PCT[os.path.normcase(os.path.abspath(project_path))] = pct
    return pct


def preview_frame_size(seq_w, seq_h, pct):
    # Stored preview frame size for a sequence of seq_w x seq_h at pct
    # percent: the sequence size itself at 100, else each side scaled and
    # rounded to an even number (at least 2, never above the sequence)
    seq_w, seq_h = int(seq_w), int(seq_h)
    pct = _clean_percent(pct)
    if pct >= 100:
        return seq_w, seq_h

    def side(v):
        return max(2, min(v, int(round(v * pct / 200.0)) * 2))
    return side(seq_w), side(seq_h)


def jpeg_size(data):
    # (width, height) from a JPEG's frame header (SOFn marker), or None
    # when the bytes are not a readable JPEG header
    try:
        b = bytes(data[:65536])
        if b[:2] != b"\xff\xd8":
            return None
        i = 2
        n = len(b)
        while i + 4 <= n:
            if b[i] != 0xFF:
                return None
            marker = b[i + 1]
            if marker == 0xFF:          # fill byte
                i += 1
                continue
            if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
                i += 2                  # standalone marker, no length
                continue
            seg = (b[i + 2] << 8) | b[i + 3]
            if (0xC0 <= marker <= 0xCF
                    and marker not in (0xC4, 0xC8, 0xCC)):
                if i + 9 > n:
                    return None
                h = (b[i + 5] << 8) | b[i + 6]
                w = (b[i + 7] << 8) | b[i + 8]
                return (w, h) if w and h else None
            if marker in (0xD9, 0xDA) or seg < 2:
                return None             # end or scan before any frame header
            i += 2 + seg
    except Exception:
        return None
    return None


def _mark_dirty_sql(conn, first, last):
    # Every frame with a preview row in first..last joins DirtyImage (a
    # frame with no row is not clean anyway). Returns the rows added.
    cur = conn.execute("INSERT OR IGNORE INTO DirtyImage (frame) "
                       "SELECT frame FROM preview WHERE frame BETWEEN ? AND ?",
                       (int(first), int(last)))
    return cur.rowcount


def _save_clean_sql(conn, frame, jpeg):
    # One clean frame: its JPEG row, and out of DirtyImage
    conn.execute("INSERT OR REPLACE INTO preview (frame, jpeg) VALUES (?, ?)",
                 (int(frame), jpeg))
    conn.execute("DELETE FROM DirtyImage WHERE frame = ?", (int(frame),))


# PreviewInfo (key TEXT PRIMARY KEY, value TEXT) in the preview file:
# "ImageSize" = "WxH", the size the stored JPEGs were built at. Files
# without it were written before Preview Size existed, at the full
# sequence size.
_IMAGE_SIZE = {}        # (project abspath, sequence_id) -> (w, h) on disk


def sync_image_size(project_path, sequence_id, size, legacy_size):
    # Preview worker thread, at the start of each pass (no write of this
    # sequence in flight). size: the (w, h) frames are built at now;
    # legacy_size: what a file with no ImageSize holds (the full sequence
    # size). When the file holds another size, every stored frame joins
    # DirtyImage and leaves the clean set, ImageSize becomes size, and
    # True is returned (the frames are then rebuilt like any dirty frame;
    # until then a player still shows the old ones, scaled to fit).
    key = (os.path.abspath(project_path), int(sequence_id))
    size = (int(size[0]), int(size[1]))
    if _IMAGE_SIZE.get(key) == size:
        return False
    conn = _connect(project_path, sequence_id)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS PreviewInfo "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM PreviewInfo "
                           "WHERE key = 'ImageSize'").fetchone()
        stored = (int(legacy_size[0]), int(legacy_size[1]))
        if row and row[0]:
            try:
                w, h = str(row[0]).lower().split("x")
                stored = (int(w), int(h))
            except ValueError:
                stored = None
        changed = stored != size
        if changed:
            _mark_dirty_sql(conn, 0, 2 ** 62)
        if changed or not row:
            conn.execute("INSERT OR REPLACE INTO PreviewInfo (key, value) "
                         "VALUES ('ImageSize', ?)", ("%dx%d" % size,))
        conn.commit()
    finally:
        conn.close()
    if changed:
        s = clean_set(project_path, sequence_id)
        with _CLEAN_LOCK:
            s.clear()
            # logged like an edit's mark, so nothing built before it can
            # come back clean
            _DIRTY_SEQ[0] += 1
            _DIRTY_LOG.append((_DIRTY_SEQ[0], key, 0, 2 ** 62))
            del _DIRTY_LOG[:-256]
    _IMAGE_SIZE[key] = size
    return changed


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
        try:
            s = set(_clean_rows(project_path, sequence_id, 0, 2 ** 62))
        except sqlite3.OperationalError:
            # Busy (a commit in progress) past the timeout: nothing is
            # cached, the next call reads again
            return set()
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
                "AND jpeg IS NOT NULL AND " + CLEAN_SQL, (a, b)).fetchall()
            got.append((d, rows))
        for a, b, d in relocs:
            _mark_dirty_sql(conn, a + d, b + d)
        for d, rows in got:
            for frame, jpeg in rows:
                nf = int(frame) + d
                if nf < 0 or jpeg is None:
                    continue
                _save_clean_sql(conn, nf, jpeg)
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
            "AND jpeg IS NOT NULL AND " + CLEAN_SQL, (first, last)).fetchall()
        _mark_dirty_sql(conn, first, last)
        _mark_dirty_sql(conn, first + delta, last + delta)
        moved = []
        for frame, jpeg in rows:
            nf = int(frame) + delta
            if nf < 0 or jpeg is None:
                continue
            _save_clean_sql(conn, nf, jpeg)
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
    # A mark adds frames to DirtyImage (small rows): the JPEG rows are not
    # touched, so a whole-sequence mark stays cheap.
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
            _mark_dirty_sql(conn, first, last)
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


class PreviewBusy(Exception):
    # The preview file is locked by a commit (FrameRetrieve)
    pass


def FrameRetrieve(project_path, sequence_id, frame_num):
    # JPEG bytes if the row exists and is clean, else None. Raises
    # PreviewBusy when the file stays locked by a commit past the busy
    # timeout (the caller keeps its picture and asks again later).
    try:
        conn = _connect(project_path, sequence_id)
    except sqlite3.OperationalError as e:
        raise PreviewBusy(str(e))
    try:
        row = conn.execute("SELECT jpeg FROM preview WHERE frame = ? AND "
                           + CLEAN_SQL, (int(frame_num),)).fetchone()
    except sqlite3.OperationalError as e:
        raise PreviewBusy(str(e))
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    return bytes(row[0])


def FrameSave(project_path, sequence_id, frame_num, jpeg_blob, dirty):
    # Insert or replace one frame; dirty 0 = clean, 1 = dirty
    conn = _connect(project_path, sequence_id)
    try:
        _save_clean_sql(conn, frame_num,
                        sqlite3.Binary(jpeg_blob) if jpeg_blob is not None else None)
        if dirty:
            conn.execute("INSERT OR IGNORE INTO DirtyImage (frame) VALUES (?)",
                         (int(frame_num),))
        conn.commit()
    finally:
        conn.close()
    s = clean_set(project_path, sequence_id)
    with _CLEAN_LOCK:
        if dirty:
            s.discard(int(frame_num))
        else:
            s.add(int(frame_num))


def _clean_rows(project_path, sequence_id, first, last):
    # Sorted list of clean frames in first..last, one query. A missing
    # preview file means no clean frames (not created). An existing file is
    # opened with _connect, so an older file gets its DirtyImage table here
    # (clean_set reads this first, before any other reader). A damaged
    # file is discarded (no clean frames); busy raises
    # sqlite3.OperationalError.
    path = preview_db_path(project_path, sequence_id)
    if not os.path.isfile(path):
        return []
    try:
        conn = _connect(project_path, sequence_id)
    except sqlite3.OperationalError:
        raise
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT frame FROM preview WHERE frame BETWEEN ? AND ? "
            "AND jpeg IS NOT NULL AND " + CLEAN_SQL + " ORDER BY frame",
            (int(first), int(last))).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        raise
    except sqlite3.Error as e:
        conn.close()
        conn = None
        if _is_damaged(e):
            try:
                _discard(project_path, sequence_id, e)
            except sqlite3.Error:
                pass
        return []
    finally:
        if conn is not None:
            conn.close()
    return [r[0] for r in rows]


def FrameCleanRange(project_path, sequence_id, first, last):
    # Sorted list of clean frames in first..last ([] when the file is
    # missing, damaged or busy past the timeout)
    try:
        return _clean_rows(project_path, sequence_id, first, last)
    except sqlite3.Error:
        return []


# Card-pass writes handed to the writer thread that may wait before the
# worker blocks (each is one batch of frames, kept in memory)
WRITE_QUEUE_MAX = 4


class PreviewBatch(object):
    # Many clean frames written at once: add() keeps them in memory; a
    # commit writes them in one transaction (one bulk JPEG insert, one bulk
    # DirtyImage delete) and only then adds them to the in-memory clean set
    # (bar moves). Used by the preview worker on its own thread.
    # BUILD 254: commit_async() hands the frames to a writer thread (its own
    # connection) and returns, so the card builds the next frames while they
    # are written. commit() and close() first wait for every handed-off
    # write, so their callers still see everything on disk.

    def __init__(self, project_path, sequence_id):
        self.project_path = project_path
        self.sequence_id = sequence_id
        self.conn = None
        self.pending = []
        self.blobs = []      # JPEG of each pending frame (same order)
        self.seqs = []       # dirty mark number when each pending frame was built
        self._jobs = None    # writer thread queue, started on first use
        self._writer = None
        self.write_s = 0.0   # seconds the writer thread spent writing

    def add(self, frame_num, jpeg_blob, seq=None):
        # seq: dirty mark number from before the frame was built (see
        # dirty_seq); default is the current number
        self.pending.append(int(frame_num))
        self.blobs.append(sqlite3.Binary(jpeg_blob))
        with _CLEAN_LOCK:
            self.seqs.append(_DIRTY_SEQ[0] if seq is None else int(seq))

    def _take(self):
        # The pending frames as one job; the batch is empty after
        job = (self.pending, self.blobs, self.seqs)
        self.pending = []
        self.blobs = []
        self.seqs = []
        return job

    def _write(self, conn, job):
        # One transaction for a job; seconds spent. Frames marked dirty
        # after they were built stay dirty.
        import time
        frames, blobs, seqs = job
        key = (os.path.abspath(self.project_path), int(self.sequence_id))
        t0 = time.perf_counter()
        conn.executemany(
            "INSERT OR REPLACE INTO preview (frame, jpeg) VALUES (?, ?)",
            zip(frames, blobs))
        conn.executemany("DELETE FROM DirtyImage WHERE frame = ?",
                         ((f,) for f in frames))
        with _CLEAN_LOCK:
            stale = [f for f, q in zip(frames, seqs)
                     if _dirty_since(key, q, f)]
        # Built before an edit marked them dirty: keep them dirty
        for f in stale:
            conn.execute("INSERT OR IGNORE INTO DirtyImage (frame) VALUES (?)",
                         (f,))
        conn.commit()
        dt = time.perf_counter() - t0
        s = clean_set(self.project_path, self.sequence_id)
        stale = set(stale)
        with _CLEAN_LOCK:
            s.update(f for f in frames if f not in stale)
        apply_dirty_queue()
        return dt

    def _writer_run(self):
        # Writer thread: jobs in order until None
        conn = None
        try:
            while True:
                job = self._jobs.get()
                try:
                    if job is None:
                        return
                    if conn is None:
                        conn = _connect(self.project_path, self.sequence_id)
                    self.write_s += self._write(conn, job)
                except Exception as e:
                    # The frames stay not clean: the next pass rebuilds them
                    print(f"Preview write failed: {e}")
                    try:
                        if conn is not None:
                            conn.rollback()
                    except Exception:
                        pass
                    if _is_damaged(e) and conn is not None:
                        # damaged file: closed and discarded; the next job
                        # opens a new one
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None
                        try:
                            _discard(self.project_path, self.sequence_id, e)
                        except sqlite3.Error as e2:
                            print(f"Preview discard failed: {e2}")
                finally:
                    self._jobs.task_done()
        finally:
            if conn is not None:
                conn.close()

    def commit_async(self):
        # Hand the pending frames to the writer thread. Returns the seconds
        # spent waiting for room in its queue (0.0 if nothing pending).
        if not self.pending:
            return 0.0
        import queue
        import time
        if self._writer is None:
            self._jobs = queue.Queue(maxsize=WRITE_QUEUE_MAX)
            self._writer = threading.Thread(target=self._writer_run,
                                            name="PreviewWriter", daemon=True)
            self._writer.start()
        t0 = time.perf_counter()
        self._jobs.put(self._take())
        return time.perf_counter() - t0

    def flush(self):
        # Wait until every handed-off write is on disk
        if self._jobs is not None:
            self._jobs.join()

    def commit(self):
        # Wait for the writer thread, then write the pending frames here.
        # Seconds spent (0.0 if nothing to do).
        import time
        t0 = time.perf_counter()
        self.flush()
        if self.pending:
            if self.conn is None:
                self.conn = _connect(self.project_path, self.sequence_id)
            self._write(self.conn, self._take())
        return time.perf_counter() - t0

    def close(self):
        try:
            self.commit()
        finally:
            if self._writer is not None:
                self._jobs.put(None)
                self._writer.join()
                self._writer = None
                self._jobs = None
            if self.conn is not None:
                self.conn.close()
                self.conn = None