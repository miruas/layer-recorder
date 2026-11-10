# SPDX-License-Identifier: GPL-3.0-only
"""Native presentation primitives and read-only media derivatives."""
import array
import hashlib
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap, QFont
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QStyledItemDelegate, QStyle

from .common import CACHE

PATHS = {
    "pen": '<path d="m4 16 12-12 4 4L8 20l-5 1 1-5Z"/><path d="m14 6 4 4"/>',
    "crop": '<path d="M6 3v15h15M3 6h15v15"/>',
    "camera": '<path d="M3 7h5l2-3h4l2 3h5v14H3V7Z"/><circle cx="12" cy="13" r="4"/>',
    "join": '<path d="M3 5h6v14H3V5Zm12 0h6v14h-6V5ZM9 12h6"/>',
    "up": '<path d="m6 12 6-6 6 6M12 6v15"/>',
    "down": '<path d="m6 12 6 6 6-6M12 3v15"/>',
    "plus": '<path d="M12 4v16M4 12h16"/>',
    "layers": '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
    "monitor": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
    "audio": '<path d="M3 10v4m4-7v10m5-14v18m5-14v10m4-7v4"/>',
    "mic": '<rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/>',
    "record": '<circle cx="12" cy="12" r="6"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "replay": '<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6m3-3v5l3 2"/>',
    "save": '<path d="M12 3v12m-4-4 4 4 4-4M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
    "settings": '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    "refresh": '<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1"/>',
    "export": '<path d="M14 3h7v7m0-7L10 14M10 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-5"/>',
    "play": '<path d="m8 4 12 8-12 8V4Z"/>',
    "pause": '<path d="M8 4v16M16 4v16"/>',
    "previous": '<path d="m17 5-9 7 9 7V5ZM5 5v14"/>',
    "next": '<path d="m7 5 9 7-9 7V5Zm12 0v14"/>',
    "split": '<path d="M12 3v18M3 6h5v12H3M21 6h-5v12h5"/>',
    "remove": '<path d="M4 7h16M9 7V4h6v3M6 7l1 14h10l1-14M10 11v6m4-6v6"/>',
    "undo": '<path d="M3 4v6h6M3 10a8 8 0 1 1 2 8"/>',
    "redo": '<path d="M21 4v6h-6m6 0a8 8 0 1 0-2 8"/>',
    "shield": '<path d="m12 3 8 3v6c0 4-5 7-8 9-3-2-8-5-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "close": '<path d="m6 6 12 12M18 6 6 18"/>',
}


def icon(name, color, size=20):
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><g fill="none" stroke="{color}" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</g></svg>'
    pix = QPixmap(size*2, size*2)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    QSvgRenderer(svg.encode()).render(painter)
    painter.end()
    pix.setDevicePixelRatio(2)
    return QIcon(pix)


class LibraryDelegate(QStyledItemDelegate):
    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = theme

    def sizeHint(self, option, index):
        return QSize(210, 88)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        t = self.theme.tokens
        data = index.data(Qt.UserRole+1) or {}
        rect = QRectF(option.rect).adjusted(4, 4, -4, -4)
        if option.state & QStyle.State_Selected:
            painter.setBrush(QColor(t["selection"]));painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 7, 7)
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(rect, QColor(t["raised"]))
        thumb_rect = QRectF(rect.left()+10, rect.top()+15, 66, 44)
        thumb = data.get("thumbnail")
        path = QPainterPath();path.addRoundedRect(thumb_rect, 4, 4)
        painter.save();painter.setClipPath(path)
        painter.fillRect(thumb_rect,QColor(t["field"]))
        if thumb:
            pix = QPixmap(thumb)
            if not pix.isNull():painter.drawPixmap(thumb_rect.toRect(),pix)
        else:
            icon("monitor",t["muted"],24).paint(painter,thumb_rect.adjusted(21,10,-21,-10).toRect())
        painter.restore()
        left = thumb_rect.right()+12
        width = max(25,rect.right()-left-10)
        painter.setPen(QColor(t["text"]))
        font=QFont(option.font);font.setPixelSize(13);font.setWeight(QFont.DemiBold);painter.setFont(font)
        title=painter.fontMetrics().elidedText(data.get("title",index.data() or "Recording"),Qt.ElideRight,int(width))
        painter.drawText(QRectF(left,rect.top()+13,width,22),Qt.AlignVCenter,title)
        font.setPixelSize(11);font.setWeight(QFont.Normal);painter.setFont(font);painter.setPen(QColor(t["muted"]))
        painter.drawText(QRectF(left,rect.top()+36,width,18),Qt.AlignVCenter,data.get("detail",""))
        painter.drawText(QRectF(left,rect.top()+54,width,16),Qt.AlignVCenter,data.get("kind","Recording"))
        painter.restore()


def derivatives(folder, project):
    """Decode real media only; never draw decorative or invented waveform data."""
    result = {"waves": {}, "thumbnail": None}
    cache = CACHE / "previews";cache.mkdir(exist_ok=True)
    for source in project["sources"]:
        file = folder / source["file"]
        key = hashlib.sha256((str(file.resolve())+str(file.stat().st_size)).encode()).hexdigest()[:24]
        if source["type"] == "video" and not result["thumbnail"]:
            image = cache / (key+".jpg")
            if not image.exists():
                proc = subprocess.run(["ffmpeg","-v","error","-y","-ss","0.1","-i",str(file),"-frames:v","1","-vf","scale=192:108:force_original_aspect_ratio=decrease,pad=192:108:(ow-iw)/2:(oh-ih)/2","-threads","1",str(image)],capture_output=True,timeout=30)
                if proc.returncode:image.unlink(missing_ok=True)
            if image.exists():result["thumbnail"] = str(image)
        elif source["type"] == "audio":
            raw = subprocess.run(["ffmpeg","-v","error","-i",str(file),"-vn","-f","f32le","-ac","1","-ar","1000","-"],capture_output=True,timeout=45)
            if raw.returncode:continue
            samples = array.array('f');samples.frombytes(raw.stdout)
            stride=max(1,len(samples)//1200)
            peaks=[min(1,max((abs(x) for x in samples[i:i+stride]),default=0)) for i in range(0,len(samples),stride)]
            result["waves"][source["id"]] = peaks
    return result
