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


_open_windows = []   # keeps windows alive (no garbage collection)


def open_proxy_manager(parent, db_path, footage_id, footage_name):
    from PySide6.QtWidgets import (
        QMainWindow, QTableWidget, QTableWidgetItem, QHeaderView,
    )

    class ProxyManagerWindow(QMainWindow):
        def __init__(self):
            super().__init__(parent)
            self.setWindowTitle(footage_name or "Proxies")
            self.resize(600, 300)
            rows = proxy_rows(db_path, footage_id)
            table = QTableWidget(len(rows), 3)
            table.setHorizontalHeaderLabels(["id", "path", "percentage"])
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectRows)
            for i, (pid, path, pct) in enumerate(rows):
                table.setItem(i, 0, QTableWidgetItem(str(pid)))
                table.setItem(i, 1, QTableWidgetItem(path or ""))
                table.setItem(i, 2, QTableWidgetItem(str(pct)))
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            self.setCentralWidget(table)

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
