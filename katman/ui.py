import copy
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QRectF, QUrl, QSize, QLocale
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QFont, QIcon, QAction, QKeySequence, QDesktopServices
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QListWidget, QListWidgetItem, QSplitter, QScrollArea,
    QCheckBox, QSlider, QDoubleSpinBox, QSpinBox, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QFileDialog, QDialog, QFormLayout, QDialogButtonBox,
    QMessageBox, QProgressBar, QSystemTrayIcon, QMenu, QStackedWidget, QColorDialog)

from .common import APP, CACHE, CONFIG, RUNTIME, atomic_json, load_project, request, settings
from .export import export_project, layout, sections_for, validate
from .playback import Playback
from .canvas import Preview
from .editor_tools import EditorTools
from .editing import scene_layout, scene_size, crop_geometry, join_next, import_recording
from .theme import ThemeManager
from .presentation import icon, LibraryDelegate, derivatives


def timecode(seconds):
    seconds = max(0, seconds)
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return (f"{h:02d}:" if h else "") + f"{m:02d}:{s:02d}"


def label(text, role=None):
    value = QLabel(text)
    if role:
        value.setObjectName(role)
    return value


def button(text, callback, tooltip=None):
    value = QPushButton(text)
    value.clicked.connect(callback)
    if tooltip:
        value.setToolTip(tooltip)
    return value


def clear_layout(layout):
    while layout.count():
        item=layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout());item.layout().deleteLater()


class Task(QThread):
    success = Signal(object)
    failure = Signal(str)
    progress = Signal(float)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.cancel = threading.Event()

    def run(self):
        try:
            self.success.emit(self.function(self))
        except Exception as e:
            self.failure.emit(str(e))




class Timeline(QWidget):
    seek = Signal(float)

    def __init__(self, playback, theme, parent=None):
        super().__init__(parent)
        self.playback, self.theme = playback, theme
        self.waves = {}
        self.setMinimumHeight(90)
        self.setAccessibleName("Source timeline. Use the transport slider to change playback position.")
        playback.position.connect(lambda _: self.update())
        theme.changed.connect(lambda _: self.update())

    def paintEvent(self, event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        t=self.theme.tokens;p.fillRect(self.rect(),QColor(t['background']))
        project=self.playback.project
        if not project:
            p.setPen(QColor(t['muted']));p.drawText(self.rect(),Qt.AlignCenter,'Your display and audio tracks will appear here.');return
        duration=max(.1,project['duration']);sources=project['sources']
        self.setMinimumHeight(32+len(sources)*34)
        label_width=min(155,self.width()*.25);start=label_width+12;width=max(10,self.width()-label_width-26)
        small=QFont(self.font());small.setPixelSize(11);p.setFont(small)
        for i in range(7):
            x=start+width*i/6
            tick_rect=QRectF(x if i==0 else x-48 if i==6 else x-24,0,48,21)
            alignment=Qt.AlignLeft if i==0 else Qt.AlignRight if i==6 else Qt.AlignCenter
            p.setPen(QColor(t['muted']));p.drawText(tick_rect,alignment|Qt.AlignVCenter,timecode(duration*i/6))
            p.setPen(QColor(t['divider']));p.drawLine(int(x),25,int(x),self.height())
        for index,source in enumerate(sources):
            y=31+index*34;color=self.theme.source_color(index)
            p.setPen(QColor(t['text']));small.setPixelSize(12);p.setFont(small)
            glyph='monitor' if source['type']=='video' else ('mic' if source.get('kind')=='microphone' else 'audio')
            icon(glyph,color.name(),14).paint(p,QRectF(1,y+4,14,14).toRect())
            text=p.fontMetrics().elidedText(source['name'],Qt.ElideRight,int(label_width-23))
            p.drawText(QRectF(23,y,label_width-23,23),Qt.AlignVCenter,text)
            for sec in sections_for(project):
                a=max(sec['start'],source.get('timeline_start',0));b=min(sec['end'],duration)
                if b<=a:continue
                rect=QRectF(start+a/duration*width,y,max(1,(b-a)/duration*width),25)
                active=source['id'] in sec['enabled_ids']
                fill=QColor(color if active and source['type']=='video' else t['raised'])
                if source['type']=='audio' and active:fill=QColor(color);fill.setAlpha(34)
                p.setPen(Qt.NoPen);p.setBrush(fill);p.drawRoundedRect(rect,4,4)
                if source['type']=='audio' and active:
                    peaks=self.waves.get(source['id'],[]);path=QPainterPath();mid=rect.center().y()
                    # The line is derived from decoded samples; a silent track is flat.
                    for pixel in range(2,max(3,int(rect.width())-2),3):
                        project_time=a+pixel/max(1,rect.width())*(b-a)
                        local=project_time-source.get('timeline_start',0)+source.get('trim_in',0)
                        sample=int(local/max(.01,source.get('media_duration',duration))*len(peaks))
                        amplitude=peaks[sample] if 0<=sample<len(peaks) else 0
                        height=max(.5,min(10,amplitude*16))
                        x=rect.left()+pixel;path.moveTo(x,mid-height);path.lineTo(x,mid+height)
                    p.setPen(QPen(color,1.3));p.drawPath(path)
                elif source['type']=='video' and active and rect.width()>85:
                    p.setPen(QColor('#111418' if color.lightness()>140 else '#ffffff'))
                    small.setPixelSize(10);p.setFont(small)
                    p.drawText(rect.adjusted(8,0,-6,0),Qt.AlignVCenter,p.fontMetrics().elidedText(source['name'],Qt.ElideRight,int(rect.width()-14)))
        x=start+self.playback.time/duration*width
        p.setPen(QPen(QColor(t['text']),1.5));p.drawLine(int(x),24,int(x),self.height())
        p.setBrush(QColor(t['text']));p.setPen(Qt.NoPen);p.drawRoundedRect(QRectF(x-3,22,6,8),2,2)

    def mousePressEvent(self, event):
        if self.playback.project:
            label_width = min(160, self.width()*.26)
            fraction = (event.position().x()-label_width-12)/max(10,self.width()-label_width-28)
            self.seek.emit(max(0,min(1,fraction))*self.playback.project["duration"])


class Window(EditorTools, QMainWindow):
    def __init__(self, open_path=None, test_mode=False):
        super().__init__()
        self.test_mode=test_mode
        self.setWindowTitle("Layer — Recording library")
        self.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "assets/katman.svg")))
        self.resize(1440, 920)
        self.setMinimumSize(1100, 740)
        self.theme = ThemeManager(self)
        self.playback = Playback(self)
        self.path = None
        self.project = None
        self.section_index = 0
        self.studio_window = None
        self.studio_returning = False
        self.quitting = False
        self.undo_stack = []
        self.redo_stack = []
        self.tasks = []
        self.status_data = {}
        self.capture_busy = False
        self.poll_pending = False
        self.loading = False
        self.export_task = None
        self.test_mode = test_mode
        self.archive_paths = []
        self.thumbnail_cache = {}
        self.library_assets_busy = False
        self.build()
        self.theme.changed.connect(self.theme_changed)
        self.theme_changed(self.theme.tokens)
        self.playback.position.connect(self.position_changed)
        self.playback.playing_changed.connect(self.playback_state_changed)
        self.playback.source_error.connect(self.show_error)
        self.refresh_archive()
        self.render_sources()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll)
        if not test_mode:
            self.setup_tray()
            self.start_task(lambda task: self.start_service(), lambda _: self.poll_timer.start(1000))
        if open_path:
            QTimer.singleShot(0, lambda: self.open_project(open_path))

    def tool(self, text, glyph, callback, tooltip=None, quiet=False, role='text'):
        value=button(text,callback,tooltip)
        value.setIconSize(QSize(17,17))
        if quiet:value.setObjectName('quiet')
        if not text:value.setAccessibleName(tooltip or glyph.title())
        self.icon_bindings.append((value,glyph,role))
        return value

    def build(self):
        self.icon_bindings=[]
        root=QWidget();self.setCentralWidget(root)
        vertical=QVBoxLayout(root);vertical.setContentsMargins(0,0,0,0);vertical.setSpacing(0)
        toolbar=QFrame();toolbar.setObjectName('toolbar')
        row=QHBoxLayout(toolbar);row.setContentsMargins(22,13,22,13);row.setSpacing(10)
        self.brand_icon=QLabel();self.brand_icon.setFixedSize(27,27);row.addWidget(self.brand_icon)
        row.addWidget(label('Layer','title'));row.addSpacing(24)
        self.record_button=self.tool('Record','record',lambda:self.capture_action('toggle-record'),'Start / stop recording · Super+Alt+F9')
        self.replay_button=self.tool('Enable replay','replay',lambda:self.capture_action('toggle-replay'),'Enable / disable replay · Super+Alt+F10')
        self.save1_button=self.tool('1 min','save',lambda:self.capture_action('save-replay',seconds=60),'Save the last minute · Super+Alt+F7',True)
        self.save5_button=self.tool('5 min','save',lambda:self.capture_action('save-replay',seconds=300),'Save the last five minutes · Super+Alt+F8',True)
        for b in (self.record_button,self.replay_button,self.save1_button,self.save5_button):row.addWidget(b)
        row.addStretch()
        self.record_state=label('Ready to capture','status');row.addWidget(self.record_state)
        row.addWidget(self.tool('','settings',self.show_settings,'Settings',True))
        vertical.addWidget(toolbar)
        self.error_bar=QFrame();el=QHBoxLayout(self.error_bar);el.setContentsMargins(16,8,16,8)
        self.error_text=label('','error');self.error_text.setWordWrap(True);self.error_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        el.addWidget(self.error_text,1);el.addWidget(self.tool('','close',self.error_bar.hide,'Dismiss message',True));self.error_bar.hide();vertical.addWidget(self.error_bar)
        self.splitter=QSplitter(Qt.Horizontal);self.splitter.setHandleWidth(1);vertical.addWidget(self.splitter,1)
        archive=QFrame();archive.setObjectName('libraryPanel');archive.setMinimumWidth(230)
        av=QVBoxLayout(archive);av.setContentsMargins(16,22,16,16);av.setSpacing(14)
        ah=QHBoxLayout();ah.addWidget(label('Library','heading'));self.library_count=label('0','count');ah.addWidget(self.library_count);ah.addStretch()
        ah.addWidget(self.tool('','refresh',self.refresh_archive,'Refresh library',True));av.addLayout(ah)
        self.search=QLineEdit();self.search.setPlaceholderText('Search recordings');self.search.setAccessibleName('Search recordings')
        self.search.textChanged.connect(self.filter_archive);av.addWidget(self.search)
        self.kind_filter=QComboBox();self.kind_filter.addItems(['All recordings','Recordings','Replays']);self.kind_filter.setAccessibleName('Filter recording type')
        self.kind_filter.currentIndexChanged.connect(lambda _:self.filter_archive(self.search.text()));av.addWidget(self.kind_filter)
        self.archive=QListWidget();self.archive.setWordWrap(False);self.archive.setMouseTracking(True)
        self.archive.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.archive.setAccessibleName('Recording library')
        self.archive.setItemDelegate(LibraryDelegate(self.theme,self.archive));self.archive.currentItemChanged.connect(self.archive_selected)
        self.library_stack=QStackedWidget();self.library_stack.addWidget(self.archive)
        empty=QWidget();ev=QVBoxLayout(empty);ev.setContentsMargins(8,35,8,20);ev.setSpacing(10)
        em=label('Your library starts here','heading');self.library_empty_title=em;em.setWordWrap(True);ev.addWidget(em)
        hint=label('Record your desktop or save a replay. Every source stays available for your edit.','muted');self.library_empty_hint=hint;hint.setWordWrap(True);ev.addWidget(hint);ev.addStretch()
        self.library_stack.addWidget(empty);av.addWidget(self.library_stack,1)
        self.archive_hint=label('Projects stay editable.','muted');self.archive_hint.setWordWrap(True);av.addWidget(self.archive_hint)
        av.addWidget(self.tool('Open project','folder',self.choose_project,'Open a Layer project · Ctrl+O'))
        self.splitter.addWidget(archive)
        center=QWidget();cv=QVBoxLayout(center);cv.setContentsMargins(22,20,22,14);cv.setSpacing(12)
        header=QHBoxLayout();self.project_title=QLineEdit();self.project_title.setObjectName('projectTitle');self.project_title.setPlaceholderText('Editing workspace');self.project_title.setAccessibleName('Project name')
        self.project_title.editingFinished.connect(self.rename_project);header.addWidget(self.project_title,1)
        self.studio_button=self.tool('Studio','layers',self.open_studio,'Independent multi-track editor');header.addWidget(self.studio_button)
        self.export_button=self.tool('Export video','export',self.start_export,'Export the selected edit · Ctrl+E',role='on_primary');self.export_button.setObjectName('primary');header.addWidget(self.export_button);cv.addLayout(header)
        self.editor_split=QSplitter(Qt.Vertical);self.editor_split.setHandleWidth(1);cv.addWidget(self.editor_split,1)
        pv=QWidget();pl=QVBoxLayout(pv);pl.setContentsMargins(0,0,0,10);pl.setSpacing(10)
        ph=QHBoxLayout();ph.addWidget(label('Viewer','muted'));ph.addStretch();self.viewer_meta=label('Fit to view','muted');ph.addWidget(self.viewer_meta);pl.addLayout(ph)
        self.preview=Preview(self.playback,self.theme);pl.addWidget(self.preview,1)
        self.preview.stroke_added.connect(self.add_stroke);self.preview.stroke_removed.connect(self.remove_stroke);self.preview.source_moved.connect(self.move_display)
        editbar=QHBoxLayout();editbar.setSpacing(7)
        self.canvas_tool=QComboBox();self.canvas_tool.setAccessibleName('Canvas tool')
        for title,value in [('View','view'),('Move display','move'),('Pen','pen'),('Line','line'),('Arrow','arrow'),('Rectangle','rectangle'),('Privacy blackout','redact'),('Eraser','eraser')]:self.canvas_tool.addItem(title,value)
        self.canvas_tool.currentIndexChanged.connect(self.set_canvas_tool);editbar.addWidget(self.canvas_tool)
        self.ink_button=self.tool('Ink','pen',self.choose_ink,'Choose drawing color',True);editbar.addWidget(self.ink_button)
        self.pen_width=QSpinBox();self.pen_width.setRange(1,32);self.pen_width.setValue(4);self.pen_width.setSuffix(' px');self.pen_width.setAccessibleName('Drawing width');self.pen_width.setMaximumWidth(78);self.pen_width.valueChanged.connect(lambda n:setattr(self.preview,'pen_width',n));editbar.addWidget(self.pen_width)
        editbar.addStretch()
        self.crop_button=self.tool('Crop','crop',self.crop_frame,'Crop the composed video frame',True);editbar.addWidget(self.crop_button)
        self.privacy_button=self.tool('Privacy','shield',self.privacy_region,'Add an opaque privacy region',True);editbar.addWidget(self.privacy_button)
        self.snapshot_button=self.tool('','camera',self.save_frame,'Save the current edited frame as PNG',True);editbar.addWidget(self.snapshot_button);pl.addLayout(editbar)
        tr=QHBoxLayout();tr.setSpacing(10)
        tr.addWidget(self.tool('','previous',lambda:self.jump_clip(-1),'Previous clip',True))
        self.play_button=self.tool('','play',self.playback.toggle,'Play / pause · Space',role='background');self.play_button.setObjectName('play');self.play_button.setFixedSize(36,36);tr.addWidget(self.play_button)
        tr.addWidget(self.tool('','next',lambda:self.jump_clip(1),'Next clip',True))
        self.seek_slider=QSlider(Qt.Horizontal);self.seek_slider.setRange(0,0);self.seek_slider.setSingleStep(1000);self.seek_slider.setPageStep(5000)
        self.seek_slider.setAccessibleName('Playback position');self.seek_slider.valueChanged.connect(lambda value:self.playback.seek(value/1000));tr.addWidget(self.seek_slider,1)
        self.clock_label=label('00:00 / 00:00','muted');self.clock_label.setMinimumWidth(107);tr.addWidget(self.clock_label);pl.addLayout(tr)
        self.editor_split.addWidget(pv)
        lower=QWidget();lv=QVBoxLayout(lower);lv.setContentsMargins(0,14,0,0);lv.setSpacing(10)
        th=QHBoxLayout();th.addWidget(label('Timeline','heading'));th.addStretch()
        self.undo_button=self.tool('','undo',self.undo,'Undo · Ctrl+Z',True);self.redo_button=self.tool('','redo',self.redo,'Redo · Ctrl+Shift+Z',True)
        th.addWidget(self.undo_button);th.addWidget(self.redo_button)
        self.split_button=self.tool('Split','split',self.split_section,'Split at playhead · Ctrl+B',True)
        self.remove_button=self.tool('','remove',self.remove_section,'Remove selected clip',True);th.addWidget(self.split_button)
        self.join_button=self.tool('','join',self.join_clip,'Join selected clip with the next matching clip',True);th.addWidget(self.join_button)
        self.up_button=self.tool('','up',lambda:self.move_clip(-1),'Move selected clip earlier',True);self.down_button=self.tool('','down',lambda:self.move_clip(1),'Move selected clip later',True);th.addWidget(self.up_button);th.addWidget(self.down_button)
        self.import_button=self.tool('Add','plus',lambda:None,'Add another recording',True)
        import_menu=QMenu(self.import_button);import_menu.addAction('Append recording…',lambda:self.choose_import(False));import_menu.addAction('Overlay recording…',lambda:self.choose_import(True));self.import_button.setMenu(import_menu);th.addWidget(self.import_button)
        th.addWidget(self.remove_button);lv.addLayout(th)
        timeline_scroll=QScrollArea();self.timeline_scroll=timeline_scroll;timeline_scroll.setWidgetResizable(True);timeline_scroll.setMinimumHeight(120);timeline_scroll.setMaximumHeight(235)
        self.timeline=Timeline(self.playback,self.theme);self.timeline.seek.connect(self.playback.seek);timeline_scroll.setWidget(self.timeline);lv.addWidget(timeline_scroll,1)
        self.sections=QTableWidget(0,4);self.sections.setHorizontalHeaderLabels(['Clip','In','Out','Included'])
        self.sections.verticalHeader().hide();self.sections.setShowGrid(False);self.sections.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sections.setSelectionMode(QAbstractItemView.SingleSelection);self.sections.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sections.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch);self.sections.setMinimumHeight(68);self.sections.setMaximumHeight(103)
        self.sections.itemSelectionChanged.connect(self.section_selected);lv.addWidget(self.sections)
        trim=QHBoxLayout();trim.setSpacing(8);self.trim_start=QDoubleSpinBox();self.trim_end=QDoubleSpinBox()
        for name,spin in [('In point',self.trim_start),('Out point',self.trim_end)]:
            spin.setDecimals(2);spin.setSingleStep(.1);spin.setSuffix(' s');spin.setMaximum(86400);spin.setMaximumWidth(110);spin.setAccessibleName(name);spin.editingFinished.connect(self.apply_trim)
            l=label(name,'muted');l.setBuddy(spin);trim.addWidget(l);trim.addWidget(spin)
        self.speed=QComboBox();self.speed.setAccessibleName('Clip playback speed')
        for value in [.25,.5,.75,1,1.5,2,4]:self.speed.addItem(f'{value:g}×',value)
        self.speed.currentIndexChanged.connect(self.change_speed);trim.addWidget(self.speed)
        trim.addStretch();self.save_state=label('Originals preserved','muted');trim.addWidget(self.save_state);lv.addLayout(trim)
        self.editor_split.addWidget(lower);self.editor_split.setSizes([445,320])
        self.export_row=QWidget();ex=QHBoxLayout(self.export_row);ex.setContentsMargins(0,0,0,0)
        self.export_progress=QProgressBar();self.export_progress.setRange(0,100);ex.addWidget(self.export_progress,1);ex.addWidget(self.tool('Cancel','close',self.cancel_export));self.export_row.hide();cv.addWidget(self.export_row)
        self.splitter.addWidget(center)
        panel=QFrame();panel.setObjectName('inspectorPanel');panel.setMinimumWidth(245)
        sv=QVBoxLayout(panel);sv.setContentsMargins(18,22,18,16);sv.setSpacing(14)
        sv.addWidget(label('Sources','heading'));self.scope_label=label('Choose what goes into your export.','muted');self.scope_label.setWordWrap(True);sv.addWidget(self.scope_label)
        self.sources_scroll=QScrollArea();self.sources_scroll.setObjectName('sourceScroll');self.sources_scroll.setWidgetResizable(True);self.sources_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);sv.addWidget(self.sources_scroll,1)
        self.source_content=QWidget();self.source_content.setObjectName('sourceContent');self.source_layout=QVBoxLayout(self.source_content);self.source_layout.setContentsMargins(0,4,0,0);self.source_layout.setSpacing(16);self.sources_scroll.setWidget(self.source_content);self.source_content.setAutoFillBackground(False);self.sources_scroll.viewport().setAutoFillBackground(False)
        self.source_note=label('Your originals are always preserved.','muted');self.source_note.setWordWrap(True);sv.addWidget(self.source_note)
        self.theme_label=label('','muted');sv.addWidget(self.theme_label)
        self.splitter.addWidget(panel);self.splitter.setSizes([270,880,280]);self.splitter.setChildrenCollapsible(False)
        self.statusBar().showMessage('Ready to capture')
        self.source_controls={};self.shortcuts();self.set_editor_enabled(False);self.save1_button.setEnabled(False);self.save5_button.setEnabled(False)

    def jump_clip(self, delta):
        if self.project:
            sections=self.project['sections'];index=max(0,min(len(sections)-1,self.section_index+delta))
            self.playback.seek(sections[index]['start'])

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.adapt_timeline()

    def adapt_timeline(self):
        if hasattr(self,'timeline_scroll'):
            rows=len(self.project['sources']) if self.project else 0
            self.timeline_scroll.setMinimumHeight(min(210,32+rows*34) if self.height()>=860 and rows else 110)

    def playback_state_changed(self, playing):
        self.play_button.setIcon(icon('pause' if playing else 'play',self.theme.tokens['background'],18))
        self.play_button.setAccessibleName('Pause' if playing else 'Play')

    def shortcuts(self):
        for key,callback in [("Space",self.toggle_with_keyboard),("Ctrl+B",self.split_section),
                             ("Ctrl+Z",self.undo),("Ctrl+Shift+Z",self.redo),("Ctrl+O",self.choose_project),
                             ("Ctrl+S",self.save_project),("Ctrl+E",self.start_export)]:
            action=QAction(self);action.setShortcut(QKeySequence(key));action.triggered.connect(callback);self.addAction(action)

    def toggle_with_keyboard(self):
        focus=QApplication.focusWidget()
        if not isinstance(focus,(QLineEdit,QDoubleSpinBox,QSpinBox)):
            self.playback.toggle()

    def set_editor_enabled(self,enabled):
        self.studio_button.setEnabled(bool(self.path))
        for widget in [self.project_title,self.export_button,self.play_button,self.seek_slider,self.split_button,
                       self.remove_button,self.trim_start,self.trim_end,self.sections,self.sources_scroll,self.canvas_tool,self.ink_button,self.pen_width,self.crop_button,self.snapshot_button,self.join_button,self.up_button,self.down_button,self.import_button,self.speed,self.privacy_button]:
            widget.setEnabled(enabled)
        self.undo_button.setEnabled(enabled and bool(self.undo_stack));self.redo_button.setEnabled(enabled and bool(self.redo_stack))
        self.remove_button.setEnabled(enabled and bool(self.project) and len(self.project["sections"])>1)

    def theme_changed(self,tokens):
        self.theme_label.setText('System theme · '+self.theme.name)
        self.brand_icon.setPixmap(icon('layers',tokens['primary'],27).pixmap(27,27))
        self.search.addAction(icon('search',tokens['muted'],16),QLineEdit.LeadingPosition) if not self.search.actions() else self.search.actions()[0].setIcon(icon('search',tokens['muted'],16))
        for control,glyph,role in self.icon_bindings:
            control.setIcon(icon(glyph,tokens[role],18))
        self.playback_state_changed(self.playback.playing)
        self.preview.update();self.timeline.update();self.archive.viewport().update()
        if hasattr(self,'tray'):self.tray.setIcon(icon('layers',tokens['primary'],24))

    def start_task(self,function,success=None,failure=None):
        task=Task(function,self);self.tasks.append(task)
        task.success.connect(success or (lambda _:None));task.failure.connect(failure or self.show_error)
        task.finished.connect(lambda:self.task_done(task));task.start();return task

    def task_done(self,task):
        if task in self.tasks:self.tasks.remove(task)
        task.deleteLater()

    def start_service(self):
        from .__main__ import ensure_daemon
        ensure_daemon()

    def poll(self):
        if self.poll_pending:return
        self.poll_pending=True
        def done(data):
            self.poll_pending=False;self.status_data=data;self.update_recording_state()
            last=data.get("last_project")
            if last and last not in self.archive_paths:self.refresh_archive()
        def failed(error):
            self.poll_pending=False;self.record_state.setText("Capture service disconnected");self.show_error(error)
        self.start_task(lambda task:request("status"),done,failed)

    def update_recording_state(self):
        data=self.status_data
        recording=data.get("recording",False);replay=data.get("replay",False)
        self.record_button.setText("Stop recording" if recording else "Record")
        self.record_button.setIcon(icon("stop" if recording else "record",self.theme.tokens["error"] if recording else self.theme.tokens["text"],18))
        self.replay_button.setText("Disable replay" if replay else "Enable replay")
        self.save1_button.setEnabled(replay and not self.capture_busy);self.save5_button.setEnabled(replay and not self.capture_busy)
        self.record_button.setEnabled(not self.capture_busy);self.replay_button.setEnabled(not self.capture_busy)
        parts=[]
        if recording:parts.append("Recording · "+timecode(data.get("record_seconds",0)))
        if replay:parts.append("Buffer · "+timecode(data.get("buffer_seconds",0))+" / 05:00")
        self.record_state.setText("  ·  ".join(parts) or "Ready to capture")
        if hasattr(self,"tray"):self.tray.setToolTip("Layer · "+self.record_state.text())
        if data.get("errors"):
            self.show_error(data["errors"][-1])

    def capture_action(self,action,**kwargs):
        if self.capture_busy:return
        self.capture_busy=True;self.update_recording_state();self.record_state.setText("Saving…")
        def done(data):
            self.capture_busy=False
            self.poll()
            saved=data if isinstance(data,str) else data.get("saved")
            if saved:
                self.refresh_archive();self.open_project(saved)
                self.statusBar().showMessage("Project saved. Choose the sources to include in your edit.",8000)
            else:self.update_recording_state()
        def failed(error):
            self.capture_busy=False;self.poll();self.update_recording_state();self.show_error(error)
        self.start_task(lambda task:request(action,**kwargs),done,failed)

    def refresh_archive(self):
        current=str(self.path) if self.path else None
        root=Path(settings()['library']).expanduser()
        paths=sorted(root.rglob('project.katman'),key=lambda p:p.parent.name,reverse=True) if root.exists() else []
        for old in getattr(self,'archive_paths',[]):
            if Path(old).exists() and Path(old) not in paths:paths.append(Path(old))
        self.archive.blockSignals(True);self.archive.clear();self.archive_paths=[];jobs=[]
        for path in paths:
            try:
                parsed,project=load_project(path);path=parsed
                screens=sum(s['type']=='video' for s in project['sources']);sounds=sum(s['type']=='audio' for s in project['sources'])
                kind='Replay' if project.get('kind')=='replay' else 'Recording'
                title=project.get('name',path.parent.name)
                data={'title':title,'detail':f"{timecode(project['duration'])} · {screens} displays · {sounds} audio",'kind':kind,'thumbnail':self.thumbnail_cache.get(str(path))}
                item=QListWidgetItem(title);item.setData(Qt.UserRole,str(path));item.setData(Qt.UserRole+1,data)
                item.setToolTip(title+'\n'+data['detail']+'\n'+str(path.parent));self.archive.addItem(item);self.archive_paths.append(str(path))
                if str(path)==current:self.archive.setCurrentItem(item)
                if not data['thumbnail'] and len(jobs)<24:jobs.append((path,project))
            except (OSError,ValueError,KeyError):continue
        self.archive.blockSignals(False);self.library_count.setText(str(self.archive.count()))
        self.archive_hint.setText('Editable projects · original media preserved' if self.archive.count() else 'Ready when you are.')
        self.filter_archive(self.search.text())
        if jobs and not self.library_assets_busy:
            self.library_assets_busy=True
            def work(task):
                result={}
                for path,project in jobs:
                    if task.cancel.is_set():break
                    try:
                        result[str(path)]=derivatives(path.parent,{'sources':[s for s in project['sources'] if s['type']=='video'][:1]})['thumbnail']
                    except Exception:continue
                return result
            def done(result):
                self.library_assets_busy=False;self.thumbnail_cache.update(result)
                for i in range(self.archive.count()):
                    item=self.archive.item(i);data=item.data(Qt.UserRole+1);data['thumbnail']=self.thumbnail_cache.get(item.data(Qt.UserRole));item.setData(Qt.UserRole+1,data)
                self.archive.viewport().update()
            self.start_task(work,done,lambda _:setattr(self,'library_assets_busy',False))

    def filter_archive(self,text):
        visible=0;kind=self.kind_filter.currentIndex()
        for i in range(self.archive.count()):
            item=self.archive.item(i);meta=item.data(Qt.UserRole+1) or {}
            match=text.casefold() in item.text().casefold() and (not kind or meta.get('kind')==('Recording' if kind==1 else 'Replay'))
            item.setHidden(not match);visible+=bool(match)
        self.library_stack.setCurrentIndex(0 if visible else 1)
        self.library_empty_title.setText('No matching recordings' if self.archive.count() else 'Your library starts here')
        self.library_empty_hint.setText('Try another search or recording type.' if self.archive.count() else 'Record your desktop or save a replay. Every source stays available for your edit.')

    def archive_selected(self,item,previous):
        if item:self.open_project(item.data(Qt.UserRole))

    def choose_project(self):
        path,_=QFileDialog.getOpenFileName(self,"Open a Layer project",settings()["library"],"Layer project (*.katman)")
        if path:self.open_project(path)

    def open_project(self,path):
        try:
            parsed,project=load_project(path)
            for source in project["sources"]:
                if not (parsed.parent/source["file"]).is_file():
                    raise ValueError(source["name"]+": media is missing. Keep the project and its media folder together.")
            if self.export_task:
                self.show_error("Wait for the export to finish before opening another project.");return
            project["sections"]=sections_for(project)
            validate(project)
            self.path,self.project=parsed,project
            self.undo_stack=[];self.redo_stack=[];self.section_index=0
            self.loading=True;self.project_title.setText(project.get("name",parsed.parent.name));self.loading=False
            self.preview.frames.clear();self.timeline.waves.clear();self.playback.load(parsed.parent,project)
            def assets_ready(result):
                if self.path==parsed:
                    self.timeline.waves=result["waves"];self.timeline.update()
            self.start_task(lambda task:derivatives(parsed.parent,copy.deepcopy(project)),assets_ready,lambda _:None)
            self.seek_slider.setRange(0,round(project["duration"]*1000))
            self.set_editor_enabled(True);self.render_sections();self.render_sources()
            if not self.test_mode and not self.studio_returning:
                QTimer.singleShot(0,self.open_studio)
            self.setWindowTitle(project.get("name","Recording")+" — Layer")
            if str(parsed) not in self.archive_paths:self.archive_paths.append(str(parsed));self.refresh_archive()
            self.error_bar.hide()
            if project.get("warnings"):self.show_error("Capture warning: "+"\n".join(project["warnings"])[-2200:])
        except Exception as e:self.show_error(str(e))

    def open_studio(self):
        if not self.path:
            self.show_error("Open a recording before entering Studio.");return
        if self.export_task:
            self.show_error("Wait for the export before entering Studio.");return
        if self.studio_window:
            self.studio_window.show();self.studio_window.raise_();self.studio_window.activateWindow();return
        self.save_project();self.playback.pause()
        from .studio import StudioWindow
        self.studio_window=StudioWindow(self.path,self)
        self.set_editor_enabled(False);self.archive.setEnabled(False)
        def returned():
            self.studio_window.deleteLater();self.studio_window=None;self.archive.setEnabled(True)
            if self.quitting:return
            self.studio_returning=True
            self.open_project(self.path);self.studio_returning=False;self.show_window()
            if self.project.get("studio"):self.set_editor_enabled(False)
        self.studio_window.closed.connect(returned);self.studio_window.show();self.studio_window.raise_()

    def checkpoint(self):
        self.undo_stack.append(copy.deepcopy(self.project));self.undo_stack=self.undo_stack[-80:];self.redo_stack=[]

    def save_project(self):
        if not self.project or self.loading:return
        try:
            validate(self.project);atomic_json(self.path,self.project)
            self.undo_button.setEnabled(bool(self.undo_stack));self.redo_button.setEnabled(bool(self.redo_stack))
            self.save_state.setText("All changes saved")
            self.playback.sync();self.preview.update();self.timeline.update()
        except Exception as e:self.show_error("Could not save the project: "+str(e))

    def rename_project(self):
        if not self.project or self.loading:return
        name=self.project_title.text().strip()
        if name and name!=self.project.get("name"):
            self.checkpoint();self.project["name"]=name;self.save_project();self.refresh_archive()

    def render_sections(self):
        if not self.project:return
        self.loading=True;self.sections.blockSignals(True)
        values=self.project["sections"];self.sections.setRowCount(len(values))
        self.section_index=min(self.section_index,len(values)-1)
        for i,sec in enumerate(values):
            for col,text in enumerate([str(i+1),f"{sec['start']:.2f} s",f"{sec['end']:.2f} s",f"{len(sec['enabled_ids'])} sources"]):
                self.sections.setItem(i,col,QTableWidgetItem(text))
        self.sections.selectRow(self.section_index);self.sections.blockSignals(False)
        sec=values[self.section_index]
        self.trim_start.setMaximum(self.project["duration"]);self.trim_end.setMaximum(self.project["duration"])
        self.trim_start.setValue(sec["start"]);self.trim_end.setValue(sec["end"])
        self.remove_button.setEnabled(len(values)>1)
        videos=[source for source in self.project["sources"] if source["type"]=="video" and source["id"] in sec["enabled_ids"]]
        w,h=scene_size(self.project,sec)
        self.speed.setCurrentIndex(max(0,self.speed.findData(sec.get("speed",1))))
        self.join_button.setEnabled(self.section_index+1<len(values))
        self.up_button.setEnabled(self.section_index>0);self.down_button.setEnabled(self.section_index+1<len(values))
        self.viewer_meta.setText(f"{w} × {h} · Fit" if videos else "Audio only · Fit")
        self.scope_label.setText(f"Clip {self.section_index+1} · {timecode(sec['start'])}–{timecode(sec['end'])}\nSelected sources appear in your export.")
        self.loading=False

    def section_selected(self):
        if self.loading or not self.project:return
        row=self.sections.currentRow()
        if row<0:return
        self.section_index=row;self.render_sections();self.render_sources()
        self.playback.seek(self.project["sections"][row]["start"])

    def render_sources(self):
        clear_layout(self.source_layout)
        self.source_controls={}
        if not self.project:
            hint=label('Open a recording to choose its displays and audio tracks.','muted');hint.setWordWrap(True)
            self.source_layout.addWidget(hint);self.source_layout.addStretch();return
        self.adapt_timeline()
        sec=self.project['sections'][self.section_index]
        for kind,title in [('video','Displays'),('audio','Audio')]:
            heading=QHBoxLayout();heading.addWidget(label(title,'heading'));heading.addStretch()
            count=sum(s['type']==kind for s in self.project['sources']);heading.addWidget(label(str(count),'count'));self.source_layout.addLayout(heading)
            for index,source in enumerate(self.project['sources']):
                if source['type']!=kind:continue
                wrap=QWidget();vl=QVBoxLayout(wrap);vl.setContentsMargins(0,0,0,6);vl.setSpacing(5)
                check=QCheckBox();check.setText(check.fontMetrics().elidedText(source['name'],Qt.ElideRight,150))
                check.setAccessibleName(source['name']);check.setToolTip(source.get('label',source['name']));check.setChecked(source['id'] in sec['enabled_ids'])
                check.toggled.connect(lambda checked,id=source['id']:self.toggle_source(id,checked));vl.addWidget(check)
                if kind=='video':
                    detail=label(f"{source['width']} × {source['height']} · Original",'muted');detail.setContentsMargins(27,0,0,0);vl.addWidget(detail)
                    options=QHBoxLayout();options.setContentsMargins(27,0,0,0)
                    blackout=QCheckBox('Black out');blackout.setAccessibleName('Black out '+source['name']);blackout.setChecked(source['id'] in sec.get('blackout_ids',[]));blackout.setEnabled(source['id'] in sec['enabled_ids']);blackout.toggled.connect(lambda checked,id=source['id']:self.blackout_display(id,checked));options.addWidget(blackout)
                    place=button('Place…',lambda checked=False,id=source['id']:self.display_placement(id));place.setToolTip('Position, size and stacking order');place.setObjectName('quiet');place.setEnabled(source['id'] in sec['enabled_ids']);options.addWidget(place);vl.addLayout(options)
                else:
                    gainrow=QHBoxLayout();gainrow.setContentsMargins(27,0,0,0);gainrow.setSpacing(7)
                    slider=QSlider(Qt.Horizontal);slider.setRange(0,100);slider.setValue(round(sec.get('gains',{}).get(source['id'],1)*100))
                    slider.setAccessibleName(source['name']+' audio level')
                    amount=label(str(slider.value())+'%','muted');amount.setMinimumWidth(33)
                    slider.valueChanged.connect(lambda value,l=amount:l.setText(str(value)+'%'))
                    slider.sliderReleased.connect(lambda id=source['id'],s=slider:self.change_gain(id,s.value()/100))
                    slider.actionTriggered.connect(lambda action,id=source['id'],s=slider:QTimer.singleShot(0,lambda:self.change_gain(id,s.value()/100)) if not s.isSliderDown() else None)
                    gainrow.addWidget(slider,1);gainrow.addWidget(amount);vl.addLayout(gainrow)
                    detail=label('Microphone' if source.get('kind')=='microphone' else 'Application audio','muted');detail.setContentsMargins(27,0,0,0);vl.addWidget(detail)
                self.source_layout.addWidget(wrap);self.source_controls[source['id']]=check
        self.source_layout.addStretch()

    def toggle_source(self,id,checked):
        if self.loading or not self.project:return
        self.checkpoint();sec=self.project["sections"][self.section_index]
        if checked and id not in sec["enabled_ids"]:sec["enabled_ids"].append(id)
        elif not checked and id in sec["enabled_ids"]:sec["enabled_ids"].remove(id)
        self.save_project();self.render_sections();self.render_sources()

    def change_gain(self,id,value):
        if not self.project:return
        sec=self.project["sections"][self.section_index]
        if abs(sec.get("gains",{}).get(id,1)-value)<.001:return
        self.checkpoint();sec.setdefault("gains",{})[id]=value;self.save_project()

    def apply_trim(self):
        if self.loading or not self.project:return
        a,b=self.trim_start.value(),self.trim_end.value();sec=self.project["sections"][self.section_index]
        if a>=b:
            self.show_error("The out point must be after the in point.");self.render_sections();return
        values=self.project["sections"];i=self.section_index
        if any(j!=i and a<other["end"]-.001 and b>other["start"]+.001 for j,other in enumerate(values)):
            self.show_error("Clips cannot overlap. Adjust the adjacent clip first.");self.render_sections();return
        if abs(a-sec["start"])<.005 and abs(b-sec["end"])<.005:return
        self.checkpoint();sec["start"],sec["end"]=a,b;self.save_project();self.render_sections();self.playback.seek(a)

    def split_section(self):
        if not self.project:return
        sec=self.project["sections"][self.section_index];pos=self.playback.time
        if not sec["start"]+.05<pos<sec["end"]-.05:
            self.show_error("Place the playhead inside the selected clip to split it.");return
        self.checkpoint();second=copy.deepcopy(sec);second["start"]=pos;sec["end"]=pos
        self.project["sections"].insert(self.section_index+1,second);self.section_index+=1
        self.save_project();self.render_sections();self.render_sources()

    def remove_section(self):
        if not self.project or len(self.project["sections"])<=1:return
        self.checkpoint();self.project["sections"].pop(self.section_index)
        self.section_index=min(self.section_index,len(self.project["sections"])-1)
        self.save_project();self.render_sections();self.render_sources();self.playback.seek(self.project["sections"][self.section_index]["start"])

    def undo(self):
        if not self.undo_stack:return
        self.redo_stack.append(copy.deepcopy(self.project));self.restore(self.undo_stack.pop())

    def redo(self):
        if not self.redo_stack:return
        self.undo_stack.append(copy.deepcopy(self.project));self.restore(self.redo_stack.pop())

    def restore(self,project):
        self.project.clear();self.project.update(project);self.project_title.setText(project.get("name","Recording"))
        self.section_index=min(self.section_index,len(project["sections"])-1)
        index=self.section_index;position=self.playback.time
        self.save_project();self.playback.load(self.path.parent,self.project)
        self.section_index=index
        sec=self.project["sections"][index]
        self.playback.seek(max(sec["start"],min(sec["end"]-.001,position)))
        self.seek_slider.setRange(0,round(self.project["duration"]*1000));self.render_sections();self.render_sources()

    def position_changed(self,seconds):
        if not self.project:return
        if not self.seek_slider.isSliderDown():
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(round(seconds*1000))
            self.seek_slider.blockSignals(False)
        self.clock_label.setText(timecode(seconds)+" / "+timecode(self.project["duration"]))
        current=next((i for i,s in enumerate(self.project["sections"]) if s["start"]<=seconds<s["end"]),self.section_index)
        if current!=self.section_index:
            self.section_index=current;self.render_sections();self.render_sources()

    def start_export(self):
        if not self.project or self.export_task:return
        self.save_project();self.playback.pause()
        default=self.path.parent/("export-"+self.path.parent.name+".mp4")
        destination,_=QFileDialog.getSaveFileName(self,"Export video",str(default),"MP4 video (*.mp4)")
        if not destination:return
        if not destination.lower().endswith(".mp4"):destination+=".mp4"
        if Path(destination).exists():self.show_error("This file already exists. Choose a new name to preserve the original.");return
        self.set_editor_enabled(False);self.archive.setEnabled(False);self.export_row.show();self.export_progress.setValue(0)
        def finished(result):
            self.export_task=None;self.export_row.hide();self.set_editor_enabled(True);self.archive.setEnabled(True)
            self.statusBar().showMessage("Export complete: "+result,15000)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(result).parent)))
        def failed(error):
            self.export_task=None;self.export_row.hide();self.set_editor_enabled(True);self.archive.setEnabled(True);self.show_error(error)
        self.export_task=self.start_task(lambda task:export_project(self.path,destination,task.progress.emit,task.cancel),finished,failed)
        self.export_task.progress.connect(lambda value:self.export_progress.setValue(round(value*100)))

    def cancel_export(self):
        if self.export_task:self.export_task.cancel.set()

    def show_error(self,error):
        self.error_text.setText(str(error)[-2200:]);self.error_bar.show()

    def show_settings(self):
        cfg=settings();dialog=QDialog(self);dialog.setWindowTitle('Layer — Settings');dialog.resize(660,700)
        outer=QVBoxLayout(dialog);outer.setContentsMargins(16,12,16,12)
        scroll=QScrollArea();scroll.setWidgetResizable(True);body=QWidget();vl=QVBoxLayout(body);vl.setContentsMargins(12,12,12,12);vl.setSpacing(20);scroll.setWidget(body);outer.addWidget(scroll,1)
        vl.addWidget(label('Settings','title'))
        subtitle=label('Capture preferences and desktop integration.','muted');subtitle.setWordWrap(True);vl.addWidget(subtitle)
        form=QFormLayout();form.setSpacing(16);form.setLabelAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        path=QLineEdit(cfg['library']);path.setAccessibleName('Recording folder');pr=QHBoxLayout();pr.setSpacing(8);pr.addWidget(path,1)
        def browse():
            picked=QFileDialog.getExistingDirectory(dialog,'Recording folder',path.text())
            if picked:path.setText(picked)
        pr.addWidget(button('Browse',browse));form.addRow('Save recordings to',pr)
        fps=QComboBox();fps.addItems(sorted(set(['30','60','120',str(cfg['fps'])]),key=int));fps.setCurrentText(str(cfg['fps']));fps.setAccessibleName('Capture frame rate');form.addRow('Frame rate',fps)
        bitrate=QSpinBox();bitrate.setRange(4000,80000);bitrate.setSingleStep(1000);bitrate.setSuffix(' kbps');bitrate.setValue(cfg['bitrate']);bitrate.setAccessibleName('Video bitrate per display');form.addRow('Bitrate per display',bitrate)
        auto=QCheckBox('Enable replay when the capture service starts');auto.setChecked(cfg.get('auto_replay',False));form.addRow(auto)
        vendor=QComboBox()
        for title,key in [('Automatic','auto'),('NVIDIA · NVENC','nvidia'),('AMD · VA-API','amd'),('Intel · VA-API','intel')]:vendor.addItem(title,key)
        vendor.setCurrentIndex(vendor.findData(cfg['gpu_vendor']));vendor.setAccessibleName('GPU profile');form.addRow('GPU profile',vendor)
        codec=QComboBox();codec.addItems(['h264','hevc','av1']);codec.setCurrentText(cfg['capture_codec']);codec.setAccessibleName('Capture codec');form.addRow('Capture codec',codec)
        vl.addLayout(form)
        appearance=label('Appearance','heading');vl.addWidget(appearance)
        desc=label('Layer follows an optional system theme-manager when available and uses its built-in palette otherwise. Theme updates keep your edits intact.','muted');desc.setWordWrap(True);vl.addWidget(desc)
        vl.addWidget(label('Suggested keyboard shortcuts','heading'))
        shortcuts=QTableWidget(5,2);shortcuts.setHorizontalHeaderLabels(['Action','Shortcut']);shortcuts.verticalHeader().hide();shortcuts.setShowGrid(False)
        shortcuts.setEditTriggers(QAbstractItemView.NoEditTriggers);shortcuts.setSelectionMode(QAbstractItemView.NoSelection);shortcuts.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for row,(action,key) in enumerate([('Open Layer','Super+Alt+Z'),('Save last minute','Super+Alt+F7'),('Save last five minutes','Super+Alt+F8'),('Start / stop recording','Super+Alt+F9'),('Enable / disable replay','Super+Alt+F10')]):
            shortcuts.setItem(row,0,QTableWidgetItem(action));shortcuts.setItem(row,1,QTableWidgetItem(key))
        shortcuts.setFixedHeight(200);vl.addWidget(shortcuts)
        info=label('Replay keeps a rolling five-minute RAM buffer. Older unsaved segments are removed automatically. Capture changes apply the next time the recording engine starts. Add the supplied compositor bindings to enable the suggested shortcuts.','muted');info.setWordWrap(True);vl.addWidget(info)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.button(QDialogButtonBox.Save).setText('Save changes');buttons.button(QDialogButtonBox.Save).setObjectName('primary');outer.addWidget(buttons)
        buttons.rejected.connect(dialog.reject)
        def save():
            try:
                directory=Path(path.text()).expanduser();directory.mkdir(parents=True,exist_ok=True)
                if not directory.is_dir():raise ValueError('Choose a valid folder.')
                cfg.update(library=str(directory),fps=int(fps.currentText()),bitrate=bitrate.value(),auto_replay=auto.isChecked(),gpu_vendor=vendor.currentData(),capture_codec=codec.currentText())
                from .configuration import validate_config
                validate_config(cfg)
                atomic_json(__import__('katman.common',fromlist=['SETTINGS_FILE']).SETTINGS_FILE,cfg);dialog.accept();self.refresh_archive()
            except Exception as e:QMessageBox.warning(dialog,'Could not save settings',str(e))
        buttons.accepted.connect(save);dialog.exec()

    def setup_tray(self):
        self.tray=QSystemTrayIcon(icon("layers",self.theme.tokens["primary"],24),self)
        menu=QMenu(self)
        for text,callback in [("Open library",self.show_window),("Start / stop recording",lambda:self.capture_action("toggle-record")),
                              ("Enable / disable replay",lambda:self.capture_action("toggle-replay")),
                              ("Save the last minute",lambda:self.capture_action("save-replay",seconds=60)),
                              ("Save the last 5 minutes",lambda:self.capture_action("save-replay",seconds=300))]:
            action=menu.addAction(text);action.triggered.connect(callback)
        menu.addSeparator();menu.addAction("Stop capture and quit",self.shutdown)
        self.tray.setContextMenu(menu);self.tray.activated.connect(lambda reason:self.show_window() if reason==QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def show_window(self):
        if self.studio_window:
            self.studio_window.showNormal();self.studio_window.raise_();self.studio_window.activateWindow();return
        self.showNormal();self.raise_();self.activateWindow()

    def shutdown(self):
        if self.export_task:
            self.show_error("Finish or cancel the export before quitting.");self.show_window();return
        self.start_task(lambda task:request("shutdown"),lambda _:QApplication.quit())

    def closeEvent(self,event):
        if self.test_mode:
            self.playback.pause();event.accept();return
        if self.export_task:
            self.show_error("Export is in progress. You can minimize the window or cancel the export.");event.ignore();return
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.hide();self.playback.pause();event.ignore()
            self.tray.showMessage("Layer","Layer is running in the system tray. Use its menu to control recording and replay.")
        else:
            self.playback.pause();event.accept();QApplication.quit()


def main(open_path=None):
    QLocale.setDefault(QLocale(QLocale.English,QLocale.UnitedStates))
    app=QApplication(sys.argv);app.setApplicationName("Layer");app.setOrganizationName("Layer")
    app.setDesktopFileName("katman");app.setStyle("Fusion")
    name=str(RUNTIME/"window.sock")
    client=QLocalSocket();client.connectToServer(name)
    if client.waitForConnected(250):
        client.write(json.dumps({"path":open_path}).encode());client.flush();client.waitForBytesWritten(500);return
    QLocalServer.removeServer(name)
    server=QLocalServer();server.setSocketOptions(QLocalServer.UserAccessOption)
    if not server.listen(name):raise RuntimeError("The application window connection could not be opened.")
    window=Window(open_path)
    def incoming():
        socket=server.nextPendingConnection()
        def read():
            try:
                data=json.loads(bytes(socket.readAll()))
                if data.get("path"):window.open_project(data["path"])
                window.show_window()
            except (ValueError,TypeError):pass
            socket.disconnectFromServer();socket.deleteLater()
        if socket.bytesAvailable():read()
        else:socket.readyRead.connect(read)
    server.newConnection.connect(incoming)
    def cleanup():
        window.quitting=True
        if window.studio_window:window.studio_window.close()
        window.poll_timer.stop();window.playback.pause()
        for task in list(window.tasks):
            task.cancel.set();task.wait(15000)
        server.close();QLocalServer.removeServer(name)
    app.aboutToQuit.connect(cleanup)
    app.setQuitOnLastWindowClosed(False);window.show();sys.exit(app.exec())
