# undo.py
# Snapshot undo/redo for Ladd Video Editor. ASCII only.
# {ProjectName}_Undo.db next to the project (Test9.db -> Test9_Undo.db,
# BUILD 223, sequence_preview.temp_file_path).
# Each snapshot copies whole tables (ATTACH + INSERT ... SELECT), ids kept.
# Snapshot copy tables are named snap_<table>_<hash of column list>, so a
# later schema change gets a new copy table instead of an ALTER.

import hashlib
import os
import sqlite3
import time

SNAP_TABLES = ("clips", "keyframe_clips", "keyframes", "tracks", "footage",
               "sequences", "captions", "Marker",
               "sequence_children",   # BUILD 106: follows sequence clips
               "keyframe_border")     # BUILD 222: border clip keys
DEFAULT_STACK_MAX = 50
WHOLE_RANGE = (0, 2 ** 40)

_listeners = []      # callables run after every push / undo / redo
_refreshers = []     # callables(project_path, label) run after a restore


def undo_db_path(project_path):
    from sequence_preview import temp_file_path
    return temp_file_path(project_path, "Undo", "db")


def ensure_undo_db(project_path):
    # Create the undo file and its index tables if missing
    if not project_path:
        return
    conn = sqlite3.connect(undo_db_path(project_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS snapshots ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "stack TEXT NOT NULL, seq INTEGER NOT NULL, "
            "label TEXT, created REAL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS snapshot_tables ("
            "snap_id INTEGER NOT NULL, tbl TEXT NOT NULL, "
            "copy_table TEXT, cols TEXT)")
        conn.commit()
    finally:
        conn.close()


def stack_max():
    try:
        from sequence_timeline_sql import _get_global_setting
        v = int(str(_get_global_setting("UndoStackMax",
                                        str(DEFAULT_STACK_MAX))).strip())
    except Exception:
        return DEFAULT_STACK_MAX
    return max(1, v)


def add_listener(fn):
    if fn not in _listeners:
        _listeners.append(fn)


def add_refresher(fn):
    if fn not in _refreshers:
        _refreshers.append(fn)


def _notify():
    for fn in list(_listeners):
        try:
            fn()
        except Exception as e:
            print(f"Undo listener failed: {e}")


def _open(project_path):
    ensure_undo_db(project_path)
    conn = sqlite3.connect(project_path, isolation_level=None)
    conn.execute("ATTACH DATABASE ? AS undo", (undo_db_path(project_path),))
    return conn


def _main_cols(conn, table):
    rows = conn.execute("PRAGMA main.table_info(%s)" % table).fetchall()
    return [r[1] for r in rows]


def _copy_name(table, cols):
    h = hashlib.sha1(",".join(cols).encode("ascii", "replace")).hexdigest()[:10]
    return "snap_%s_%s" % (table, h)


def _q(name):
    return '"%s"' % name.replace('"', '""')


def _capture(conn, stack, label):
    # Insert one snapshot of every table; returns the new snapshot id
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM undo.snapshots WHERE stack = ?",
        (stack,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO undo.snapshots (stack, seq, label, created) VALUES (?, ?, ?, ?)",
        (stack, seq, label or "", time.time()))
    snap_id = cur.lastrowid
    for table in SNAP_TABLES:
        cols = _main_cols(conn, table)
        if not cols:
            conn.execute(
                "INSERT INTO undo.snapshot_tables (snap_id, tbl, copy_table, cols) "
                "VALUES (?, ?, NULL, NULL)", (snap_id, table))
            continue
        copy = _copy_name(table, cols)
        col_sql = ", ".join(_q(c) for c in cols)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS undo.%s AS SELECT 0 AS snap_id, %s "
            "FROM main.%s WHERE 0" % (_q(copy), col_sql, _q(table)))
        conn.execute(
            "INSERT INTO undo.%s (snap_id, %s) SELECT ?, %s FROM main.%s"
            % (_q(copy), col_sql, col_sql, _q(table)), (snap_id,))
        conn.execute(
            "INSERT INTO undo.snapshot_tables (snap_id, tbl, copy_table, cols) "
            "VALUES (?, ?, ?, ?)", (snap_id, table, copy, ",".join(cols)))
    return snap_id


def _drop_snapshot(conn, snap_id):
    for copy, in conn.execute(
            "SELECT copy_table FROM undo.snapshot_tables WHERE snap_id = ?",
            (snap_id,)).fetchall():
        if copy:
            conn.execute("DELETE FROM undo.%s WHERE snap_id = ?" % _q(copy),
                         (snap_id,))
    conn.execute("DELETE FROM undo.snapshot_tables WHERE snap_id = ?", (snap_id,))
    conn.execute("DELETE FROM undo.snapshots WHERE id = ?", (snap_id,))


def _clear_stack(conn, stack):
    for sid, in conn.execute("SELECT id FROM undo.snapshots WHERE stack = ?",
                             (stack,)).fetchall():
        _drop_snapshot(conn, sid)


def _trim(conn, stack, limit):
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM undo.snapshots WHERE stack = ? ORDER BY seq DESC, id DESC",
        (stack,)).fetchall()]
    for sid in ids[limit:]:
        _drop_snapshot(conn, sid)


def _restore(conn, snap_id):
    # Replace every table with the snapshot rows (ids preserved)
    for table, copy, cols in conn.execute(
            "SELECT tbl, copy_table, cols FROM undo.snapshot_tables WHERE snap_id = ?",
            (snap_id,)).fetchall():
        now_cols = _main_cols(conn, table)
        if not now_cols:
            continue
        conn.execute("DELETE FROM main.%s" % _q(table))
        if not copy:
            continue
        use = [c for c in (cols or "").split(",") if c and c in now_cols]
        if not use:
            continue
        col_sql = ", ".join(_q(c) for c in use)
        conn.execute(
            "INSERT INTO main.%s (%s) SELECT %s FROM undo.%s WHERE snap_id = ?"
            % (_q(table), col_sql, col_sql, _q(copy)), (snap_id,))
        if "id" in now_cols:
            has_seq = conn.execute(
                "SELECT 1 FROM main.sqlite_master WHERE name = 'sqlite_sequence'"
            ).fetchone()
            if has_seq:
                conn.execute(
                    "UPDATE main.sqlite_sequence SET seq = MAX(seq, "
                    "(SELECT COALESCE(MAX(id), 0) FROM main.%s)) WHERE name = ?"
                    % _q(table), (table,))


def push_undo(project_path, label=""):
    # Before an undoable edit: snapshot onto undo, clear redo, trim to max
    if not project_path or not os.path.isfile(project_path):
        return
    try:
        conn = _open(project_path)
        try:
            conn.execute("BEGIN")
            _capture(conn, "undo", label)
            _clear_stack(conn, "redo")
            _trim(conn, "undo", stack_max())
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    except Exception as e:
        print(f"Undo snapshot failed: {e}")
        return
    _notify()


def _top(conn, stack):
    return conn.execute(
        "SELECT id, label FROM undo.snapshots WHERE stack = ? "
        "ORDER BY seq DESC, id DESC LIMIT 1", (stack,)).fetchone()


def _step(project_path, src, dst):
    # Undo (src undo, dst redo) or redo (src redo, dst undo). Returns label
    # or None when src is empty.
    if not project_path or not os.path.isfile(project_path):
        return None
    conn = _open(project_path)
    try:
        top = _top(conn, src)
        if top is None:
            return None
        snap_id, label = top
        conn.execute("BEGIN")
        try:
            _capture(conn, dst, label)
            _trim(conn, dst, stack_max())
            _restore(conn, snap_id)
            _drop_snapshot(conn, snap_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return label or ""


def undo(project_path):
    return _step(project_path, "undo", "redo")


def redo(project_path):
    return _step(project_path, "redo", "undo")


def counts(project_path):
    # (undo count, redo count); (0, 0) without a project
    if not project_path or not os.path.isfile(undo_db_path(project_path)):
        return 0, 0
    try:
        conn = sqlite3.connect(undo_db_path(project_path))
        try:
            rows = dict(conn.execute(
                "SELECT stack, COUNT(*) FROM snapshots GROUP BY stack").fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return 0, 0
    return int(rows.get("undo", 0)), int(rows.get("redo", 0))


def current_project():
    try:
        from sequence_timeline_sql import _get_global_setting
        return _get_global_setting("current_project", "") or ""
    except Exception:
        return ""


def _refresh_after_restore(project_path, message):
    # Whole sequence dirty, then reload every open window
    try:
        # BUILD 136: every sequence's audio mix may differ now, including
        # sequences with no timeline open (cheap flags, no mixing)
        from sequence_preview_audio import mark_preview_audio_dirty
        mark_preview_audio_dirty(project_path)
    except Exception:
        pass
    try:
        import sequence_timeline
        for tl in list(sequence_timeline._open_windows.values()):
            try:
                if os.path.abspath(tl._project_db()) != os.path.abspath(project_path):
                    continue
                tl._selected_clip_ids = set()
                tl._selection_order = []
                tl._selection_solo = False
                # BUILD 146: no whole-sequence mark: the reload's mile-marker
                # diff (footage paths included) marks what the undo changed
                # tl._mark_preview_dirty([WHOLE_RANGE])
                tl._after_clip_change(message)
            except Exception as e:
                print(f"Undo timeline refresh failed: {e}")
    except Exception:
        pass
    for fn in list(_refreshers):
        try:
            fn(project_path, message)
        except Exception as e:
            print(f"Undo refresh failed: {e}")


def perform(which, project_path=None):
    # GUI entry: "undo" or "redo" on the current project
    project_path = project_path or current_project()
    try:
        label = undo(project_path) if which == "undo" else redo(project_path)
    except Exception as e:
        print(f"Undo {which} failed: {e}")
        label = None
    if label is not None:
        word = "Undo" if which == "undo" else "Redo"
        _refresh_after_restore(project_path, ("%s %s" % (word, label)).strip())
    _notify()
    return label is not None


def handle_key_event(event):
    # Ctrl+Z / Ctrl+Y for windows that are not QWidgets (Sequence Player).
    # True when handled.
    from PySide6.QtCore import Qt
    if event.modifiers() & Qt.ControlModifier:
        if event.key() == Qt.Key_Z:
            perform("undo")
            return True
        if event.key() == Qt.Key_Y:
            perform("redo")
            return True
    return False
