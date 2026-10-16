# proxy_manager.py
# Proxies for footage: ProxyPath setting and a read-only Proxies list
# window. The Proxies table and the ProxyPath setting are created only in
# new_project.py _init_db; older project DBs without them read as empty.
# ASCII only.

import os
import sqlite3

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
            try:
                add_proxy_row(db_path, footage_id, int(pct))
            except Exception as e:
                QMessageBox.warning(self, "Proxy Manager", "Could not add proxy: %s" % e)
                return
            self._reload()
            if on_change is not None:
                try:
                    on_change()
                except Exception:
                    pass

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
