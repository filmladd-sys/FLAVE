# proxy_manager.py
# Proxies for footage: ProxyPath setting and a read-only Proxies list
# window. The Proxies table and the ProxyPath setting are created only in
# new_project.py _init_db; older project DBs without them read as empty.
# ASCII only.

import os
import sqlite3
import time

PROXY_PATH_KEY = "ProxyPath"


def _project_folder(db_path):
    return os.path.dirname(os.path.abspath(db_path)) if db_path else ""


def get_proxy_path(db_path):
    # Stored ProxyPath, or the project folder if missing / unreadable
    if not db_path:
        return ""
    value = None
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?",
                               (PROXY_PATH_KEY,)).fetchone()
            value = row[0] if row else None
        finally:
            conn.close()
    except Exception:
        value = None
    return value if value else _project_folder(db_path)


def proxy_counts(db_path):
    # {footage_id: number of Proxies rows}; {} if the table is missing
    if not db_path:
        return {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT footage_id, COUNT(*) FROM Proxies GROUP BY footage_id"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    return {int(fid): int(n) for fid, n in rows}


def proxy_rows(db_path, footage_id):
    # [(id, path, percentage)] for footage_id; [] if the table is missing
    if not db_path:
        return []
    try:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT id, path, percentage FROM Proxies WHERE footage_id = ? "
                "ORDER BY id", (int(footage_id),)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


PROXY_PERCENTAGES = (75, 50, 25, 10)
VIDEO_PROXY_EXT = "mp4"     # video footage proxies
IMAGE_PROXY_EXT = "jpg"     # image footage proxies
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def proxies_table_exists(db_path):
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                               "AND name='Proxies'").fetchone()
        finally:
            conn.close()
        return row is not None
    except Exception:
        return False


def footage_source(db_path, footage_id):
    # (original_path, filename) for footage_id, or ("", "")
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT original_path, filename FROM footage WHERE id = ?",
                               (int(footage_id),)).fetchone()
        finally:
            conn.close()
    except Exception:
        row = None
    if not row:
        return "", ""
    return row[0] or "", row[1] or ""


def proxy_file_path(proxy_dir, source_path, percentage):
    # <ProxyPath>/<footage stem>_proxy<pct>.<mp4 for video | jpg for image>
    base = os.path.basename(source_path or "")
    stem, ext = os.path.splitext(base)
    out_ext = IMAGE_PROXY_EXT if ext.lower() in _IMAGE_EXTS else VIDEO_PROXY_EXT
    return os.path.join(proxy_dir, "%s_proxy%d.%s" % (stem, int(percentage), out_ext))


def available_percentages(db_path, footage_id):
    have = set(int(r[2]) for r in proxy_rows(db_path, footage_id))
    return [p for p in PROXY_PERCENTAGES if p not in have]


def add_proxy_row(db_path, footage_id, percentage):
    # Insert one Proxies row (no file is written). Returns the new id.
    src, name = footage_source(db_path, footage_id)
    path = proxy_file_path(get_proxy_path(db_path), src or name, percentage)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO Proxies (footage_id, path, percentage) VALUES (?, ?, ?)",
            (int(footage_id), path, int(percentage)))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---- video proxies (FFmpeg h264_nvenc, BUILD 84)

_VIDEO_EXTS = (".mov", ".mp4", ".mkv", ".avi", ".m4v")


def is_video_path(path):
    return os.path.splitext(path or "")[1].lower() in _VIDEO_EXTS


def video_proxy_path(proxy_dir, source_path, percentage):
    # <ProxyPath>/<source basename>.<pct>.mp4, e.g. VidTest.mov.50.mp4
    return os.path.join(proxy_dir, os.path.basename(source_path) + "." +
                        str(int(percentage)) + ".mp4")


def scaled_size(width, height, percentage):
    # Percent of each side, rounded to even, minimum 2
    def side(v):
        s = int(round(float(v) * int(percentage) / 100.0 / 2.0)) * 2
        return max(2, s)
    return side(width), side(height)


def _no_window_flags():
    import sys
    return 0x08000000 if sys.platform.startswith("win") else 0   # CREATE_NO_WINDOW


def _run(cmd):
    # (returncode, stdout text, stderr text); raises OSError if not found
    import subprocess
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=_no_window_flags())
    return (proc.returncode, proc.stdout.decode("ascii", "replace"),
            proc.stderr.decode("ascii", "replace"))


def nvenc_available():
    # (ok, message). ffmpeg on PATH and h264_nvenc listed in -encoders
    try:
        code, out, err = _run(["ffmpeg", "-hide_banner", "-encoders"])
    except OSError:
        return False, "ffmpeg was not found on PATH."
    if "h264_nvenc" not in out + err:
        return False, "ffmpeg on PATH has no h264_nvenc encoder (NVIDIA NVENC)."
    return True, ""


def probe_size(src):
    # (width, height) of the first video stream via ffprobe, or None
    try:
        code, out, _err = _run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", src])
    except OSError:
        return None
    if code != 0:
        return None
    try:
        w, h = out.strip().splitlines()[0].split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


def footage_size(db_path, footage_id):
    # Stored footage width / height, or None
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT width, height FROM footage WHERE id = ?",
                               (int(footage_id),)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if row and row[0] and row[1]:
        return int(row[0]), int(row[1])
    return None


def build_proxy_cmd(src, out, w, h):
    # format=yuv420p converts 10-bit sources to 8-bit before nvenc
    return ["ffmpeg", "-y", "-hide_banner", "-i", src, "-map", "0:v:0", "-an",
            "-vf", "scale=%d:%d,format=yuv420p" % (int(w), int(h)),
            "-c:v", "h264_nvenc", "-preset", "p4", "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-g", "15", out]


def save_proxy_row(db_path, footage_id, path, percentage):
    # Update the path of an existing row for footage + pct, else insert
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM Proxies WHERE footage_id = ? AND percentage = ? "
            "ORDER BY id LIMIT 1", (int(footage_id), int(percentage))).fetchone()
        if row:
            conn.execute("UPDATE Proxies SET path = ? WHERE id = ?", (path, row[0]))
            rid = row[0]
        else:
            rid = conn.execute(
                "INSERT INTO Proxies (footage_id, path, percentage) VALUES (?, ?, ?)",
                (int(footage_id), path, int(percentage))).lastrowid
        conn.commit()
        return rid
    finally:
        conn.close()


def _remove_partial(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def generate_video_proxy(db_path, footage_id, percentage, wait=None):
    # Run ffmpeg; on success save the Proxies row. Returns (ok, message).
    # wait(proc) is called while ffmpeg runs (GUI keeps processing events).
    import subprocess
    src, _name = footage_source(db_path, footage_id)
    if not src or not os.path.isfile(src):
        return False, "Source file not found:\n%s" % src
    ok, msg = nvenc_available()
    if not ok:
        return False, msg
    size = probe_size(src) or footage_size(db_path, footage_id)
    if size is None:
        return False, "Could not read the source width and height (ffprobe)."
    w, h = scaled_size(size[0], size[1], percentage)
    proxy_dir = get_proxy_path(db_path)
    try:
        os.makedirs(proxy_dir, exist_ok=True)
    except Exception as e:
        return False, "Could not create ProxyPath %s: %s" % (proxy_dir, e)
    out = video_proxy_path(proxy_dir, src, percentage)
    cmd = build_proxy_cmd(src, out, w, h)
    # print("PROXY cmd: %s" % " ".join(cmd))
    import tempfile
    err_file = tempfile.TemporaryFile()
    try:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err_file,
                                    creationflags=_no_window_flags())
        except OSError as e:
            return False, "Could not start ffmpeg: %s" % e
        if wait is not None:
            wait(proc)
        code = proc.wait()
        err_file.seek(0)
        err = err_file.read().decode("ascii", "replace")
    finally:
        err_file.close()
    if code != 0 or not os.path.isfile(out) or os.path.getsize(out) <= 0:
        _remove_partial(out)
        tail = "\n".join(err.strip().splitlines()[-15:])
        return False, "ffmpeg failed (exit %s):\n%s" % (code, tail)
    try:
        save_proxy_row(db_path, footage_id, out, percentage)
    except Exception as e:
        return False, "Proxy file written but the row could not be saved: %s" % e
    return True, out


_open_windows = []   # keeps windows alive (no garbage collection)


def open_proxy_manager(parent, db_path, footage_id, footage_name, on_change=None):
    from PySide6.QtWidgets import (
        QMainWindow, QTableWidget, QTableWidgetItem, QHeaderView, QWidget,
        QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton, QMessageBox,
    )

    src, fname = footage_source(db_path, footage_id)
    title_name = os.path.basename(src) if src else (footage_name or fname)

    class ProxyManagerWindow(QMainWindow):
        def __init__(self):
            super().__init__(parent)
            self.setWindowTitle("Proxy Manager: %s" % title_name)
            self.resize(600, 340)
            central = QWidget()
            layout = QVBoxLayout(central)
            row = QHBoxLayout()
            row.addWidget(QLabel("Generate"))
            self.combo = QComboBox()
            row.addWidget(self.combo, 1)
            layout.addLayout(row)
            self.gen_btn = QPushButton("Generate")
            self.gen_btn.clicked.connect(self._generate)
            layout.addWidget(self.gen_btn)
            self.combo.currentIndexChanged.connect(self._update_button)
            self.table = QTableWidget(0, 3)
            self.table.setHorizontalHeaderLabels(["id", "path", "percentage"])
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
            self.table.setSelectionBehavior(QTableWidget.SelectRows)
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            layout.addWidget(self.table, 1)
            self.setCentralWidget(central)
            self._reload()

        def _reload(self):
            rows = proxy_rows(db_path, footage_id)
            self.table.setRowCount(len(rows))
            for i, (pid, path, pct) in enumerate(rows):
                self.table.setItem(i, 0, QTableWidgetItem(str(pid)))
                self.table.setItem(i, 1, QTableWidgetItem(path or ""))
                self.table.setItem(i, 2, QTableWidgetItem(str(pct)))
            self.combo.blockSignals(True)
            self.combo.clear()
            for pct in available_percentages(db_path, footage_id):
                self.combo.addItem("%d%%" % pct, pct)
            self.combo.blockSignals(False)
            self._update_button()

        def _update_button(self, *_args):
            self.gen_btn.setEnabled(self.combo.currentData() is not None)

        def _generate(self):
            pct = self.combo.currentData()
            if pct is None:
                return
            if not proxies_table_exists(db_path):
                QMessageBox.information(
                    self, "Proxy Manager",
                    "Proxies are not available in this project because it was "
                    "created with an older version. Create a new project to use "
                    "proxies.")
                return
            src_path, _n = footage_source(db_path, footage_id)
            if is_video_path(src_path):
                self._generate_video(int(pct))
            else:
                # Images: BUILD 82 placeholder row only (no file written)
                try:
                    add_proxy_row(db_path, footage_id, int(pct))
                except Exception as e:
                    QMessageBox.warning(self, "Proxy Manager",
                                        "Could not add proxy: %s" % e)
                    return
            self._reload()
            if on_change is not None:
                try:
                    on_change()
                except Exception:
                    pass

        def _generate_video(self, pct):
            from PySide6.QtWidgets import QApplication
            from PySide6.QtCore import Qt

            def wait(proc):
                while proc.poll() is None:
                    QApplication.processEvents()
                    time.sleep(0.05)

            self.gen_btn.setEnabled(False)
            self.combo.setEnabled(False)
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                ok, msg = generate_video_proxy(db_path, footage_id, pct, wait)
            finally:
                QApplication.restoreOverrideCursor()
                self.combo.setEnabled(True)
                self._update_button()
            if not ok:
                QMessageBox.warning(self, "Proxy Manager", msg)

        def closeEvent(self, event):
            try:
                _open_windows.remove(self)
            except ValueError:
                pass
            event.accept()

    win = ProxyManagerWindow()
    _open_windows.append(win)
    win.show()
    win.raise_()
    win.activateWindow()
    return win
