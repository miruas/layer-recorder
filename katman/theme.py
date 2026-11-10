# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

THEME_ROOT = Path.home() / ".config" / "theme-manager"


class ThemeManager(QObject):
    changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.name = ""
        self.tokens = {}
        self.override = None
        self.signature = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.refresh()

    def refresh(self, force=False):
        try:
            selected = self.override or (THEME_ROOT / "selected").read_text().strip()
            presets = json.loads((THEME_ROOT / "presets.json").read_text())
            preset = next(p for p in presets if p["id"] == selected)
            data = json.loads((THEME_ROOT / (selected + ".json")).read_text())
            roles = data["light" if preset["light"] else "dark"]
            signature = json.dumps([selected, roles], sort_keys=True)
        except (OSError, ValueError, StopIteration, KeyError):
            selected = "system"
            preset = {"name": "System", "light": False}
            roles = {"background": "#181b1e", "surfaceText": "#eef0f2", "surfaceContainer": "#25292d",
                     "surfaceContainerHigh": "#30363b", "surfaceVariantText": "#bdc4ca",
                     "outline": "#737d85", "primary": "#a5ceda", "primaryText": "#122a31",
                     "primaryContainer": "#28414a"}
            signature = selected
        if not force and signature == self.signature:
            return
        self.signature = signature
        self.name = preset["name"]
        self.tokens = {"background": roles["background"], "text": roles["surfaceText"],
                       "surface": roles["surfaceContainer"], "raised": roles["surfaceContainerHigh"],
                       "muted": roles["surfaceVariantText"], "outline": roles["outline"],
                       "primary": roles["primary"], "on_primary": roles["primaryText"],
                       "selection": roles["primaryContainer"], "light": preset["light"],
                       "error": "#a5272a" if preset["light"] else "#ffb4ab"}
        t = self.tokens
        palette = QPalette()
        mappings = {QPalette.Window: t["background"], QPalette.WindowText: t["text"],
                    QPalette.Base: t["background"], QPalette.AlternateBase: t["surface"],
                    QPalette.Text: t["text"], QPalette.Button: t["raised"], QPalette.ButtonText: t["text"],
                    QPalette.Highlight: t["primary"], QPalette.HighlightedText: t["on_primary"],
                    QPalette.ToolTipBase: t["raised"], QPalette.ToolTipText: t["text"],
                    QPalette.PlaceholderText: t["muted"]}
        for role, color in mappings.items():
            palette.setColor(role, QColor(color))
        app = QApplication.instance()
        app.setPalette(palette)
        app.setFont(QFont("Noto Sans", 10))
        def mix(a, b, ratio):
            a,b=QColor(a),QColor(b)
            return QColor(round(a.red()*(1-ratio)+b.red()*ratio),round(a.green()*(1-ratio)+b.green()*ratio),round(a.blue()*(1-ratio)+b.blue()*ratio)).name()
        self.tokens.update(field=roles.get('surfaceContainerLowest',roles['background']),
                           divider=mix(roles['background'],roles['outline'],.27),
                           panel=mix(roles['background'],roles['surfaceContainer'],.70))
        t=self.tokens
        app.setStyleSheet(f"""
            QWidget {{ color: {t['text']}; font-size: 13px; }}
            QMainWindow, QDialog {{ background: {t['background']}; }}
            QFrame#toolbar, QFrame#panel {{ background: {t['panel']}; }}
            QFrame#toolbar {{ border-bottom: 1px solid {t['divider']}; }}
            QFrame#libraryPanel {{ background: {t['panel']}; border-right: 1px solid {t['divider']}; }}
            QFrame#inspectorPanel {{ background: {t['panel']}; border-left: 1px solid {t['divider']}; }}
            QWidget#sourceContent, QScrollArea#sourceScroll {{ background: {t['panel']}; }}
            QLabel#title {{ font-size: 20px; font-weight: 600; }}
            QLabel#heading {{ font-size: 15px; font-weight: 600; }}
            QLabel#muted {{ color: {t['muted']}; font-size: 12px; }}
            QLabel#count {{ background: {t['raised']}; color: {t['muted']}; border-radius: 5px; padding: 3px 7px; font-size: 11px; }}
            QLabel#error {{ color: {t['error']}; padding: 10px 14px; background: {t['surface']}; border-radius: 6px; }}
            QLabel#status {{ color: {t['muted']}; font-size: 12px; padding: 5px 10px; }}
            QPushButton, QToolButton {{ background: {t['raised']}; border: 2px solid transparent; border-radius: 6px;
                padding: 7px 11px; min-height: 20px; font-weight: 500; }}
            QPushButton:hover, QToolButton:hover {{ background: {t['selection']}; }}
            QPushButton:pressed, QToolButton:pressed {{ background: {t['primary']}; color: {t['on_primary']}; }}
            QPushButton:disabled, QToolButton:disabled {{ color: {t['muted']}; background: transparent; }}
            QPushButton#primary, QToolButton#primary {{ background: {t['primary']}; color: {t['on_primary']}; font-weight: 600; }}
            QPushButton#primary:hover, QToolButton#primary:hover {{ border-color: {t['on_primary']}; }}
            QPushButton#primary:disabled, QToolButton#primary:disabled {{ background: {t['surface']}; color: {t['muted']}; }}
            QPushButton#quiet, QToolButton#quiet {{ background: transparent; }}
            QPushButton#quiet:hover, QToolButton#quiet:hover {{ background: {t['raised']}; }}
            QPushButton#play {{ background: {t['text']}; color: {t['background']}; border-radius: 17px; padding: 5px; min-width: 22px; }}
            QPushButton:focus, QToolButton:focus {{ border-color: {t['primary']}; }}
            QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{ background: {t['field']}; border: 1px solid {t['outline']}; border-radius: 5px;
                padding: 7px 9px; min-height: 20px; selection-background-color: {t['primary']}; selection-color: {t['on_primary']}; }}
            QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 25px; background: {t['raised']}; border-left: 1px solid {t['outline']}; border-top-right-radius: 4px; }}
            QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 25px; background: {t['raised']}; border-left: 1px solid {t['outline']}; border-bottom-right-radius: 4px; }}
            QSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{ background: {t['selection']}; }}
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url(UP_PATH); width: 12px; height: 8px; }}
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url(DOWN_PATH); width: 12px; height: 8px; }}
            QLineEdit {{ placeholder-text-color: {t['muted']}; }}
            QLineEdit#projectTitle {{ background: transparent; border: 1px solid transparent; padding: 4px 0; font-size: 16px; font-weight: 600; }}
            QLineEdit#projectTitle:hover {{ border-bottom-color: {t['outline']}; }}
            QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {t['primary']}; }}
            QListWidget, QTableWidget, QScrollArea, QStackedWidget {{ background: transparent; border: 0; }}
            QListWidget {{ outline: none; }}
            QListWidget:focus {{ border: 1px solid {t['primary']}; }}
            QTableWidget {{ gridline-color: {t['divider']}; outline: none; alternate-background-color: {t['panel']}; }}
            QTableWidget::item {{ padding: 4px 10px; border-bottom: 1px solid {t['divider']}; }}
            QTableWidget::item:selected {{ background: {t['selection']}; color: {t['text']}; }}
            QHeaderView::section {{ background: {t['panel']}; color: {t['muted']}; border: 0; padding: 7px 10px; font-size: 11px; font-weight: 500; }}
            QCheckBox {{ spacing: 10px; padding: 3px 0; font-weight: 500; }}
            QCheckBox::indicator {{ width: 17px; height: 17px; }}
            QCheckBox::indicator:unchecked {{ background: {t['field']}; border: 1px solid {t['outline']}; border-radius: 4px; }}
            QCheckBox::indicator:checked {{ background: {t['primary']}; border: 1px solid {t['primary']}; border-radius: 4px; image: url(CHECK_PATH); }}
            QCheckBox::indicator:focus {{ border: 2px solid {t['text']}; }}
            QSplitter::handle {{ background: {t['divider']}; width: 1px; height: 1px; }}
            QSplitter::handle:hover {{ background: {t['primary']}; }}
            QSlider {{ border: 1px solid transparent; border-radius: 4px; padding: 4px 0; }}
            QSlider:focus {{ border-color: {t['primary']}; }}
            QSlider::groove:horizontal {{ height: 3px; border-radius: 1px; background: {t['raised']}; }}
            QSlider::sub-page:horizontal {{ background: {t['primary']}; }}
            QSlider::handle:horizontal {{ width: 9px; margin: -4px 0; border-radius: 4px; background: {t['primary']}; }}
            QProgressBar {{ border: 1px solid {t['divider']}; border-radius: 5px; text-align: center; min-height: 24px; background: {t['field']}; }}
            QProgressBar::chunk {{ background: {t['selection']}; border-radius: 4px; }}
            QStatusBar {{ color: {t['muted']}; background: {t['panel']}; border-top: 1px solid {t['divider']}; font-size: 11px; }}
            QToolTip {{ background: {t['raised']}; color: {t['text']}; border: 1px solid {t['outline']}; padding: 7px; }}
            QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
            QScrollBar::handle:vertical {{ background: {t['outline']}; min-height: 24px; border-radius: 3px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            QMenu {{ background: {t['surface']}; border: 1px solid {t['outline']}; padding: 5px; }}
            QMenu::item {{ padding: 8px 22px; border-radius: 4px; }}
            QMenu::item:selected {{ background: {t['selection']}; }}
        """)
        # Checkbox ink uses the primary's actual paired foreground in every theme.
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18"><path d="m4 9 3 3 7-7" fill="none" stroke="{t["on_primary"]}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        from .common import CACHE
        check = CACHE / "check.svg"
        check.write_text(svg)
        app.setStyleSheet(app.styleSheet().replace("CHECK_PATH", str(check)))
        for name,points in [('up','2 6 6 2 10 6'),('down','2 2 6 6 10 2')]:
            arrow=CACHE/(name+'.svg')
            arrow.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8" viewBox="0 0 12 8"><polyline points="{points}" fill="none" stroke="{t["text"]}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>')
            app.setStyleSheet(app.styleSheet().replace(name.upper()+'_PATH',str(arrow)))
        self.changed.emit(t)

    def source_color(self, index):
        colors = [self.tokens["primary"], "#7bb7d8", "#c59ad8", "#87bd9d", "#ddb774", "#d68e9f"]
        if self.tokens["light"]:
            colors = [self.tokens["primary"], "#336688", "#79568d", "#397158", "#876b30", "#964e62"]
        return QColor(colors[index % len(colors)])
