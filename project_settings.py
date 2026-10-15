# project_settings.py
# Project Settings window stub for Ladd Video Editor
# Separate file so Project Manager stays focused.
# ASCII only.

from PySide6.QtWidgets import (
    QMainWindow, QLabel, QVBoxLayout, QWidget, QPushButton
)
from PySide6.QtCore import Qt


class ProjectSettingsWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Project Settings")
        self.resize(500, 400)

        central = QWidget()
        layout = QVBoxLayout(central)

        label = QLabel("Project Settings stub\n\nPer-project defaults\n(sequence size, fps, track height, etc.)\nwill be edited here.")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

        self.setCentralWidget(central)

    def closeEvent(self, event):
        # Does not exit the application
        event.accept()
