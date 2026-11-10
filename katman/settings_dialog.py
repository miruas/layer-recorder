# SPDX-License-Identifier: GPL-3.0-only
"""Native recording, replay, keyboard and GSR preferences."""
import copy,shutil,subprocess
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence,QIcon
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QFormLayout,QHBoxLayout,QWidget,QTabWidget,QScrollArea,QLabel,QLineEdit,QPushButton,QComboBox,QSpinBox,QCheckBox,QDialogButtonBox,QKeySequenceEdit,QFileDialog,QMessageBox)
from .common import settings,SETTINGS_FILE,atomic_json
from .configuration import validate_config
from .shortcuts import ACTIONS,check_conflicts
from . import gsr_integration as gsr


class SettingsDialog(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent);self.cfg=settings();self.fields={};self.shortcut_fields={};self.gsr_fields={};self.apply_now=False;self.test_mode=getattr(parent,'test_mode',False)
        self.setWindowTitle('Layer — Settings');self.resize(780,680);self.setMinimumSize(620,520)
        outer=QVBoxLayout(self);outer.setContentsMargins(20,18,20,18);outer.setSpacing(14)
        title=QLabel('Settings');title.setObjectName('title');outer.addWidget(title)
        self.tabs=QTabWidget();outer.addWidget(self.tabs,1)
        capture=self.page('Recording');self.text(capture,'Choose the format used by each display. Original source tracks remain independent.')
        path=QLineEdit(self.cfg['library']);path.setAccessibleName('Recording folder');self.fields['library']=path;row=QHBoxLayout();row.addWidget(path,1);browse=QPushButton('Browse');row.addWidget(browse);browse.clicked.connect(lambda:self.browse(path));capture.addRow('Save recordings to',row)
        self.number(capture,'fps','Frame rate',1,240,' fps');self.number(capture,'bitrate','Bitrate per display',1000,200000,' kbps')
        self.combo(capture,'gpu_vendor','GPU profile',[('Automatic','auto'),('NVIDIA · NVENC','nvidia'),('AMD · VA-API','amd'),('Intel · VA-API','intel')])
        self.combo(capture,'capture_codec','Capture codec',[('H.264 (AVC)','h264'),('H.265 (HEVC)','hevc'),('AV1','av1')])
        from .hardware import export_choices
        self.combo(capture,'export_codec','Default export encoder',export_choices())
        self.text(capture,'H.264 favors compatibility. H.265 usually produces smaller files at comparable quality; playback support depends on the receiving device. Capture changes apply when recording next starts.')
        self.apply=QCheckBox('Apply recording changes now · preserve replay first');self.apply.setAccessibleName('Apply capture changes now');capture.addRow(self.apply)
        self.text(capture,'Applying now saves the available Layer replay before restarting idle capture. An ordinary recording is never interrupted. GSR recording options apply on its next capture.')
        replay=self.page('Replay');self.text(replay,'Keep recent independently encoded screens and sounds in RAM. Save either preset without stopping the buffer.')
        self.number(replay,'buffer','RAM buffer length',30,3600,' seconds');self.number(replay,'replay_short','Short replay preset',1,3600,' seconds');self.number(replay,'replay_long','Long replay preset',1,3600,' seconds')
        self.check(replay,'auto_replay','Enable replay when the capture service starts')
        self.text(replay,'Short ≤ long ≤ buffer. Higher bitrates, more displays and longer buffers use more RAM. Saved clips go to the recording folder; older parts of an ordinary recording can be kept on disk.')
        shortcuts=self.page('Shortcuts');self.check(shortcuts,'shortcuts_enabled','Enable Layer global shortcuts')
        self.text(shortcuts,'Click a shortcut and press the combination you want. Clear it to disable that action. Other applications’ shortcuts are preserved; conflicts are reported before saving.')
        for action,title in ACTIONS.items():
            edit=QKeySequenceEdit(QKeySequence(self.cfg['shortcuts'][action]));edit.setMaximumSequenceLength(1);edit.setAccessibleName(title+' shortcut');self.shortcut_fields[action]=edit;shortcuts.addRow(title,edit)
        self.text(shortcuts,'Layer registers its own bindings with Hyprland and reapplies them after a compositor reload. Existing GSR keys are configured in the next tab.')
        integration=self.page('GSR Integration');self.gsr_data=gsr.read();self.gsr_original=gsr.hotkeys(self.gsr_data) if self.gsr_data else {}
        self.check(integration,'gsr_sync','Synchronize codec and RAM replay length with GSR')
        self.fields['gsr_sync'].setEnabled(bool(self.gsr_data));self.text(integration,'The patched GSR menu mirrors its recording and replay actions into Layer. Preferences are backed up privately before changes; unrelated GSR settings remain intact.')
        for action,title in gsr.LABELS.items():
            edit=QKeySequenceEdit(QKeySequence(self.gsr_original.get(action,'')));edit.setMaximumSequenceLength(1);edit.setEnabled(bool(self.gsr_data));edit.setAccessibleName(title+' shortcut');self.gsr_fields[action]=edit;integration.addRow(title,edit)
        self.text(integration,'Linked Layer saves use your short/long presets; GSR keeps its own one-minute/full-buffer video saves. Codec/replay changes take effect when GSR capture restarts; shared-key updates reload immediately with the bundled overlay.')
        if not self.gsr_data:self.text(integration,'Open GPU Screen Recorder once to create its settings, then reopen this page to connect it.')
        open_gsr=QPushButton('Open GSR overlay');open_gsr.setEnabled(bool(shutil.which('gsr-ui-cli')));open_gsr.clicked.connect(self.open_overlay);integration.addRow(open_gsr)
        self.text(integration,'Layer follows an available system theme-manager live; otherwise it uses its built-in native palette. No system theme files are changed.')
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.button(QDialogButtonBox.Save).setText('Save changes');buttons.button(QDialogButtonBox.Save).setObjectName('primary');buttons.button(QDialogButtonBox.Save).setIcon(QIcon());buttons.button(QDialogButtonBox.Cancel).setIcon(QIcon());buttons.accepted.connect(self.save);buttons.rejected.connect(self.reject);outer.addWidget(buttons)
    def page(self,title):
        scroll=QScrollArea();scroll.setWidgetResizable(True);body=QWidget();form=QFormLayout(body);form.setContentsMargins(16,18,16,18);form.setSpacing(14);form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow);scroll.setWidget(body);self.tabs.addTab(scroll,title);return form
    def text(self,form,text):
        label=QLabel(text);label.setWordWrap(True);label.setObjectName('muted');form.addRow(label)
    def number(self,form,key,title,minimum,maximum,suffix):
        field=QSpinBox();field.setRange(minimum,maximum);field.setValue(self.cfg[key]);field.setSuffix(suffix);field.setAccessibleName(title);self.fields[key]=field;form.addRow(title,field)
    def combo(self,form,key,title,values):
        field=QComboBox()
        for label,value in values:field.addItem(label,value)
        field.setCurrentIndex(field.findData(self.cfg[key]));field.setAccessibleName(title);self.fields[key]=field;form.addRow(title,field)
    def check(self,form,key,title):
        field=QCheckBox(title);field.setChecked(self.cfg[key]);field.setAccessibleName(title);self.fields[key]=field;form.addRow(field)
    def browse(self,field):
        selected=QFileDialog.getExistingDirectory(self,'Recording folder',field.text())
        if selected:field.setText(selected)
    def open_overlay(self):subprocess.Popen(['gsr-ui-cli','toggle-show'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def save(self):
        try:
            cfg=copy.deepcopy(self.cfg)
            for key,field in self.fields.items():cfg[key]=field.text() if isinstance(field,QLineEdit) else field.currentData() if isinstance(field,QComboBox) else field.isChecked() if isinstance(field,QCheckBox) else field.value()
            cfg['shortcuts']={key:field.keySequence().toString(QKeySequence.PortableText) for key,field in self.shortcut_fields.items()};cfg=validate_config(cfg)
            if not self.test_mode:check_conflicts(cfg)
            directory=Path(cfg['library']).expanduser();directory.mkdir(parents=True,exist_ok=True)
            if not directory.is_dir():raise ValueError('Choose a valid recording folder.')
            changes={key:field.keySequence().toString(QKeySequence.PortableText) for key,field in self.gsr_fields.items() if field.keySequence().toString(QKeySequence.PortableText)!=QKeySequence(self.gsr_original.get(key,'')).toString(QKeySequence.PortableText)}
            if self.gsr_data and (cfg['gsr_sync'] or changes):gsr.update(cfg,changes or None)
            atomic_json(SETTINGS_FILE,cfg);self.apply_now=self.apply.isChecked();self.accept()
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as e:QMessageBox.warning(self,'Could not save settings',str(e))
