# project_manager.py
# Project Manager - main application window for Ladd Video Editor
# Owns application lifetime. Closing exits the program.
# ASCII only.

import sys
import os
import sqlite3
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QMenuBar, QMenu,
    QFileDialog, QMessageBox, QStatusBar
)
from PySide6.QtCore import Qt, QSettings
from PySide6.QtGui import QAction, QKeySequence

# Stub import for the separate settings window
try:
    from global_settings import GlobalSettingsWindow, apply_dark_mode
except ImportError:
    GlobalSettingsWindow = None
    apply_dark_mode = None

# Stub import for the new project window
try:
    from new_project import NewProjectWindow
except ImportError:
    NewProjectWindow = None

# Stub import for the project settings window
try:
    from project_settings import ProjectSettingsWindow
except ImportError:
    ProjectSettingsWindow = None

# Import for the footage manager
try:
    from footage_manager import FootageManager
except ImportError:
    FootageManager = None

# Import for sequence timelines
try:
    from sequence_timeline import (
        open_timeline, get_open_timeline_ids, sequence_exists,
        snapshot_open_timelines, set_app_quitting, get_open_player_ids
    )
except ImportError:
    snapshot_open_timelines = None
    set_app_quitting = None
    get_open_player_ids = None
    open_timeline = None
    get_open_timeline_ids = None
    sequence_exists = None

# Import for the sequence manager
try:
    from sequence_manager import SequenceManager
except ImportError:
    SequenceManager = None


def _global_db_path():
    return r"P:\globalsettings.db"  # Fixed global settings location


def _ensure_global_db():
    path = _global_db_path()
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()
    return path


def _set_global_setting(key, value):
    _ensure_global_db()
    conn = sqlite3.connect(_global_db_path())
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def _get_global_setting(key, default=None):
    _ensure_global_db()
    conn = sqlite3.connect(_global_db_path())
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def _ensure_audiotmp_for(project_db):
    # Create sibling .audiotmp.db if missing and backfill from footage
    if not project_db:
        return
    try:
        from peaks import ensure_audiotmp_for_project
        ensure_audiotmp_for_project(project_db)
    except Exception:
        pass


def _ensure_keyframe_tables_for(project_db):
    # keyframe_clips + keyframes on the project DB (open / create / autoload)
    if not project_db:
        return
    try:
        from keyframe import ensure_keyframe_tables
        conn = sqlite3.connect(project_db)
        try:
            ensure_keyframe_tables(conn)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _ensure_undo_db_for(project_db):
    # <project stem>.undo.db next to the project, created if missing
    try:
        from undo import ensure_undo_db
        ensure_undo_db(project_db)
    except Exception as e:
        print(f"Undo db init failed: {e}")


class ProjectManager(QMainWindow):
    TRACKED_WINDOWS = [
        ("footage", "footage_window"),
        ("sequence", "sequence_window"),
        ("project_settings", "project_settings_window"),
        ("global_settings", "settings_window"),
    ]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ladd Video Editor - Project Manager")
        self.resize(800, 600)

        # Simple central status
        self.status_label = QLabel("No project open")
        self.status_label.setAlignment(Qt.AlignCenter)
        # Selectable / copyable project path (QLabel keeps its right-click Copy)
        self.status_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.status_label.setCursor(Qt.IBeamCursor)
        self.setCentralWidget(self.status_label)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

        self.current_project = None
        self.layout_memory_enabled = True  # default; later from settings

        # Apply saved dark mode if any
        if apply_dark_mode is not None:
            dark = _get_global_setting("dark_mode", "0")
            apply_dark_mode(dark == "1")

        self._create_menus()
        self._restore_geometry()
        self._autoload_project()
        self._restore_open_windows()

        # Track geometry changes for layout memory (no timer)
        self._last_geometry = self.geometry()

    def _create_menus(self):
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("File")

        new_action = QAction("New Project", self)
        new_action.triggered.connect(self._new_project)
        file_menu.addAction(new_action)

        open_action = QAction("Open Project", self)
        open_action.triggered.connect(self._open_project)
        file_menu.addAction(open_action)

        self.close_action = QAction("Close Project", self)
        self.close_action.setEnabled(False)
        self.close_action.triggered.connect(self._close_project)
        file_menu.addAction(self.close_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Edit menu: snapshot undo/redo (undo.py). Ctrl+Z / Ctrl+Y are
        # application-wide shortcuts on these actions, so they fire from
        # every QWidget window; the Sequence Player QWindow forwards them.
        edit_menu = menubar.addMenu("Edit")
        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence("Ctrl+Z"))
        self.undo_action.setShortcutContext(Qt.ApplicationShortcut)
        self.undo_action.triggered.connect(lambda: self._undo_redo("undo"))
        edit_menu.addAction(self.undo_action)
        self.redo_action = QAction("Redo", self)
        self.redo_action.setShortcut(QKeySequence("Ctrl+Y"))
        self.redo_action.setShortcutContext(Qt.ApplicationShortcut)
        self.redo_action.triggered.connect(lambda: self._undo_redo("redo"))
        edit_menu.addAction(self.redo_action)
        try:
            import undo
            undo.add_listener(self._update_undo_actions)
            undo.add_refresher(self._refresh_after_undo)
        except Exception as e:
            print(f"Undo unavailable: {e}")
        self._update_undo_actions()

        # Settings menu
        settings_menu = menubar.addMenu("Settings")

        global_settings_action = QAction("Global Settings", self)
        global_settings_action.triggered.connect(self._open_global_settings)
        settings_menu.addAction(global_settings_action)

        self.project_settings_action = QAction("Project Settings", self)
        self.project_settings_action.setEnabled(False)
        self.project_settings_action.triggered.connect(self._open_project_settings)
        settings_menu.addAction(self.project_settings_action)

        # Windows menu
        self.windows_menu = menubar.addMenu("Windows")

        self.seq_mgr_action = QAction("Sequence Manager", self)
        self.seq_mgr_action.setEnabled(False)
        self.seq_mgr_action.triggered.connect(self._open_sequence_manager)
        self.windows_menu.addAction(self.seq_mgr_action)

        self.footage_mgr_action = QAction("Footage Manager", self)
        self.footage_mgr_action.setEnabled(False)
        self.footage_mgr_action.triggered.connect(self._open_footage_manager)
        self.windows_menu.addAction(self.footage_mgr_action)

    def _undo_redo(self, which):
        if not self.current_project:
            return
        import undo
        if undo.perform(which, self.current_project):
            self.status_bar.showMessage("Undo" if which == "undo" else "Redo", 3000)

    def _update_undo_actions(self):
        try:
            import undo
            n_undo, n_redo = undo.counts(self.current_project)
        except Exception:
            n_undo, n_redo = 0, 0
        self.undo_action.setEnabled(n_undo > 0)
        self.redo_action.setEnabled(n_redo > 0)

    def _refresh_after_undo(self, project_path, message):
        # Footage and sequence managers reload from the restored tables
        for attr in ("footage_window", "sequence_window"):
            win = getattr(self, attr, None)
            if win is not None and win.isVisible() and hasattr(win, "refresh"):
                try:
                    win.refresh()
                except Exception as e:
                    print(f"Undo refresh {attr} failed: {e}")

    def _update_windows_menu(self):
        if hasattr(self, "undo_action"):
            self._update_undo_actions()
        enabled = self.current_project is not None
        self.seq_mgr_action.setEnabled(enabled)
        self.footage_mgr_action.setEnabled(enabled)
        self.close_action.setEnabled(enabled)
        self.project_settings_action.setEnabled(enabled)

    def _autoload_project(self):
        path = _get_global_setting("current_project", "")
        if path and os.path.isfile(path):
            self.current_project = path
            self.status_label.setText(f"Project: {path}")
            self.status_bar.showMessage("Project loaded")
            _ensure_audiotmp_for(path)
            _ensure_keyframe_tables_for(path)
            _ensure_undo_db_for(path)
            self._update_windows_menu()

    def _new_project(self):
        if NewProjectWindow is None:
            QMessageBox.warning(self, "New Project", "new_project.py not found")
            return
        # Close the current project first (same routine as Close Project).
        # Cancelling the New Project window leaves no project open.
        if self.current_project:
            self._close_project()
        self.new_project_window = NewProjectWindow(self)
        self.new_project_window.project_created.connect(self._on_project_created)
        self.new_project_window.show()

    def _on_project_created(self, db_path):
        self.current_project = db_path
        self.status_label.setText(f"Project: {db_path}")
        self.status_bar.showMessage("New project created")
        folder = os.path.dirname(db_path)
        if folder:
            _set_global_setting("last_project_folder", folder)
        _set_global_setting("current_project", db_path)
        _ensure_audiotmp_for(db_path)
        _ensure_keyframe_tables_for(db_path)
        _ensure_undo_db_for(db_path)
        self._update_windows_menu()

    def _open_project(self):
        start_dir = _get_global_setting("last_project_folder", "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", start_dir, "Project Database (*.db)"
        )
        if path:
            self.current_project = path
            self.status_label.setText(f"Project: {path}")
            self.status_bar.showMessage("Project opened")
            folder = os.path.dirname(path)
            if folder:
                _set_global_setting("last_project_folder", folder)
            _set_global_setting("current_project", path)
            _ensure_audiotmp_for(path)
            _ensure_keyframe_tables_for(path)
            _ensure_undo_db_for(path)
            self._update_windows_menu()

    def _close_project(self):
        # Close every project window; only Project Manager (and Global
        # Settings, if open) stays up
        for attr in ("footage_window", "sequence_window", "project_settings_window"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.close()
                except RuntimeError:
                    # Underlying Qt object already deleted
                    pass
                setattr(self, attr, None)
        # BUILD 116: Render Manager windows belong to the project too (closed before
        # the timelines, so a running render is cancelled, not failed)
        try:
            import render_manager
            render_manager.close_all_render_managers()
        except ImportError:
            pass
        try:
            import sequence_timeline
            for timeline in list(sequence_timeline._open_windows.values()):
                try:
                    timeline.close()
                except RuntimeError:
                    pass
            sequence_timeline._open_windows.clear()
        except ImportError:
            pass
        # Proxy Manager windows belong to the project
        try:
            import proxy_manager
            for win in list(proxy_manager._open_windows):
                try:
                    win.close()
                except RuntimeError:
                    pass
            del proxy_manager._open_windows[:]
        except ImportError:
            pass
        # Relaunch must not reopen timelines or windows of a closed project
        _set_global_setting("open_timelines", "")
        _set_global_setting("open_players", "")
        keep = "global_settings" if self._window_is_open("settings_window") else ""
        _set_global_setting("open_windows", keep)

        self.current_project = None
        self.status_label.setText("No project open")
        self.status_bar.showMessage("Project closed")
        _set_global_setting("current_project", "")
        self._update_windows_menu()

    def _open_global_settings(self):
        if GlobalSettingsWindow is None:
            QMessageBox.warning(self, "Settings", "global_settings.py not found")
            return
        self.settings_window = GlobalSettingsWindow(self)
        self.settings_window.show()

    def _open_project_settings(self):
        if ProjectSettingsWindow is None:
            QMessageBox.warning(self, "Settings", "project_settings.py not found")
            return
        if not self.current_project:
            return
        self.project_settings_window = ProjectSettingsWindow(self)
        self.project_settings_window.show()

    def _open_sequence_manager(self):
        if SequenceManager is None:
            QMessageBox.warning(self, "Windows", "sequence_manager.py not found")
            return
        if not self.current_project:
            return
        self.sequence_window = SequenceManager(self)
        self.sequence_window.show()

    def _open_footage_manager(self):
        if FootageManager is None:
            QMessageBox.warning(self, "Windows", "footage_manager.py not found")
            return
        if not self.current_project:
            return
        self.footage_window = FootageManager(self)
        self.footage_window.show()

    def _restore_geometry(self):
        geo = _get_global_setting("project_manager_geometry", "")
        if geo:
            try:
                parts = [int(p) for p in geo.split(",")]
                if len(parts) == 4:
                    self.setGeometry(parts[0], parts[1], parts[2], parts[3])
            except Exception:
                pass

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.layout_memory_enabled:
            self._save_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.layout_memory_enabled:
            self._save_geometry()

    def _save_geometry(self):
        g = self.geometry()
        value = f"{g.x()},{g.y()},{g.width()},{g.height()}"
        _set_global_setting("project_manager_geometry", value)

    def _window_is_open(self, attr):
        w = getattr(self, attr, None)
        return w is not None and w.isVisible()

    def _save_open_windows(self):
        names = []
        for name, attr in self.TRACKED_WINDOWS:
            if self._window_is_open(attr):
                names.append(name)
        _set_global_setting("open_windows", ",".join(names))

    def _restore_open_windows(self):
        value = _get_global_setting("open_windows", "") or ""
        names = [n.strip() for n in value.split(",") if n.strip()]
        openers = {
            "footage": self._open_footage_manager,
            "sequence": self._open_sequence_manager,
            "project_settings": self._open_project_settings,
            "global_settings": self._open_global_settings,
        }
        for name in names:
            opener = openers.get(name)
            if opener is None:
                continue
            if name != "global_settings" and not self.current_project:
                continue
            opener()
        self._restore_open_timelines()

    def _restore_open_timelines(self):
        if not self.current_project or open_timeline is None:
            return
        parent = getattr(self, "sequence_window", None) or self
        player_ids = get_open_player_ids() if get_open_player_ids else []
        for sequence_id in get_open_timeline_ids():
            if sequence_exists(self.current_project, sequence_id):
                timeline = open_timeline(parent, sequence_id)
                # Reopen its Sequence Player if it was open at last close
                if timeline is not None and sequence_id in player_ids:
                    try:
                        from sequence_player import open_player_for
                        open_player_for(timeline)
                    except Exception as e:
                        print("Sequence Player restore failed: %s" % e)

    def closeEvent(self, event):
        self._save_open_windows()
        # Snapshot open timelines before any child closeEvent can remove ids
        if set_app_quitting is not None:
            set_app_quitting(True)
        if snapshot_open_timelines is not None:
            snapshot_open_timelines()
        if self.layout_memory_enabled:
            self._save_geometry()
        # Closing Project Manager exits the application
        event.accept()
        QApplication.quit()


def print_build():
    # Build counter written next to this script (Syncthing delivery check)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "counter.txt")
    try:
        with open(path, "r") as f:
            print(f"BUILD {int(f.read().strip())}")
    except FileNotFoundError:
        print("BUILD missing")


def main():
    print_build()
    # Shared CUDA primary context before any window or preview worker
    try:
        from cuda_context import ensure_cuda
        ensure_cuda(0)
    except Exception as e:
        # print("CUDA launch init failed: %s" % e)
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("LaddVideoEditor")
    # BUILD 114: camera button over every top-level window's title bar
    try:
        from window_snapshot import install_window_snapshots
        install_window_snapshots(app)
    except Exception as e:
        # print("window snapshot install failed: %s" % e)
        pass
    # BUILD 115: immersive dark title bars on Windows in dark mode
    try:
        from dark_titlebar import install_dark_titlebars
        install_dark_titlebars(app)
    except Exception as e:
        # print("dark title bar install failed: %s" % e)
        pass
    window = ProjectManager()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
