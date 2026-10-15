# keyframe.py
# Keyframe clip / keyframe table helpers for the project DB. ASCII only.

# Stored on keyframe_clips.type. Not enforced beyond storing the string.
KEYFRAME_CLIP_TYPES = ("position X", "position Y", "scale")


def ensure_keyframe_clips_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS keyframe_clips ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "sequence_id INTEGER NOT NULL, "
        "track_id INTEGER NOT NULL, "
        "type TEXT NOT NULL, "
        "link_id INTEGER, "
        "in_frame INTEGER NOT NULL, "
        "out_frame INTEGER NOT NULL)"
    )


def ensure_keyframes_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS keyframes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "keyframe_clip_id INTEGER NOT NULL, "
        "frame INTEGER NOT NULL, "
        "value REAL NOT NULL)"
    )


def ensure_keyframe_tables(conn):
    ensure_keyframe_clips_table(conn)
    ensure_keyframes_table(conn)
