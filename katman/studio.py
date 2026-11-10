# SPDX-License-Identifier: GPL-3.0-only
"""Full native, independent-track editing workspace for existing Layer projects."""
import copy
import json
from pathlib import Path
from PySide6.QtCore import Qt,QTimer,QUrl,Signal
from PySide6.QtGui import QAction,QKeySequence,QColor,QImage
from PySide6.QtMultimedia import QMediaPlayer,QAudioOutput,QVideoSink
from PySide6.QtWidgets import (QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QSplitter,QTreeWidget,QTreeWidgetItem,QScrollArea,QLabel,QPushButton,QComboBox,QSlider,QTabWidget,QFormLayout,QCheckBox,QLineEdit,QProgressBar,QToolBar,QColorDialog,QFileDialog,QInputDialog,QListWidget,QHeaderView)
from .common import atomic_json,load_project
from .theme import ThemeManager
from .presentation import icon,derivatives
from .studio_model import from_project,find_clip,all_clips,clip_duration,duration,validate,split_clip,uid,value_at
from .studio_widgets import TrackTimeline,StudioCanvas
from .studio_dialogs import StudioDialogs,spin
from .studio_render import export_studio
from .studio_jobs import frame_image,preview_file,build_proxies


class StudioWindow(StudioDialogs,QMainWindow):
    closed=Signal()
    def __init__(self,path,parent=None,test_mode=False):
        super().__init__(parent);self.path,self.project=load_project(path);self.model=from_project(self.project);self.sources={s['id']:s for s in self.project['sources']};self.theme=ThemeManager(self)
        self.selection=set();self.primary=None;self.selected_track=None;self.annotation_selection=None;self.position=0;self.revision=0;self.undo_stack=[];self.redo_stack=[];self.tasks=[];self.clipboard=[];self.busy=False;self.loading=False;self.frame_task=None;self.frame_pending=False;self.preview_revision=-1;self.preview_path=None;self.playing=False;self.ink='#ff4c4c';self.ink_width=4;self.waves={};self.test_mode=test_mode;self.closing=False
        self.setWindowTitle(self.project.get('name','Recording')+' — Layer Studio');self.resize(1600,1000);self.setMinimumSize(1100,740)
        self.player=QMediaPlayer(self);self.audio=QAudioOutput(self);self.sink=QVideoSink(self);self.player.setAudioOutput(self.audio);self.player.setVideoSink(self.sink);self.player.positionChanged.connect(self.player_position);self.sink.videoFrameChanged.connect(self.player_frame)
        self.player.errorOccurred.connect(lambda code,text:self.error('Preview playback: '+text))
        self.build();self.refresh_all();self.theme.changed.connect(lambda _:self.refresh_theme())
        self.frame_timer=QTimer(self);self.frame_timer.setSingleShot(True);self.frame_timer.setInterval(180);self.frame_timer.timeout.connect(self.request_frame)
        if not test_mode:QTimer.singleShot(100,self.request_frame);QTimer.singleShot(150,self.load_waves);QTimer.singleShot(200,self.fit_timeline)

    @staticmethod
    def timecode(seconds):
        seconds=max(0,seconds);m,s=divmod(int(seconds),60);h,m=divmod(m,60);return (f'{h:02}:' if h else '')+f'{m:02}:{s:02}'

    def action(self,title,callback,shortcut=None,glyph=None,menu=None,toolbar=False):
        a=QAction(title,self);a.triggered.connect(lambda checked=False:callback())
        if shortcut:a.setShortcut(QKeySequence(shortcut));self.addAction(a)
        if glyph:a.setProperty('layerGlyph',glyph);a.setIcon(icon(glyph,self.theme.tokens['on_primary'] if title=='Export timeline…' else self.theme.tokens['text'],18))
        if menu:menu.addAction(a)
        if toolbar:
            a.setIconText({"Append media…":"Import","Split selected":"Split","Delete selected":"Remove","Export timeline…":"Export"}.get(title,title));self.tools.addAction(a)
            self.tools.widgetForAction(a).setObjectName('primary' if title=='Export timeline…' else 'quiet' if title in ('Undo','Redo') else '')
        self.edit_actions.append(a);return a

    def build(self):
        self.edit_actions=[];self.tools=QToolBar('Editing',self);self.tools.setMovable(False);self.tools.setToolButtonStyle(Qt.ToolButtonTextBesideIcon);self.addToolBar(self.tools)
        media=self.menuBar().addMenu('Media');edit=self.menuBar().addMenu('Edit');clip=self.menuBar().addMenu('Clip');captions=self.menuBar().addMenu('Captions');view=self.menuBar().addMenu('View');export=self.menuBar().addMenu('Export')
        self.action('Back to library',self.close,menu=media)
        self.action('Append media…',lambda:self.import_media(False),'Ctrl+I','folder',media,True);self.action('Overlay media at playhead…',lambda:self.import_media(True),menu=media)
        self.action('Record voiceover…',self.record_voiceover,menu=media)
        self.undo_action=self.action('Undo',self.undo,'Ctrl+Z','undo',edit,True);self.redo_action=self.action('Redo',self.redo,'Ctrl+Shift+Z','redo',edit,True)
        self.action('Split selected',self.split_selected,'Ctrl+B','split',edit,True);self.action('Delete selected',self.delete_selected,'Delete','remove',edit,True)
        self.action('Copy clips',self.copy_clips,'Ctrl+C',menu=edit);self.action('Paste clips',self.paste_clips,'Ctrl+V',menu=edit);self.action('Duplicate clips',self.duplicate,'Ctrl+D',menu=edit);self.action('Select all clips',self.select_all,'Ctrl+A',menu=edit)
        self.action('Animation keyframes…',self.keyframes,menu=clip);self.action('Variable speed segments…',self.speed_segments,menu=clip);self.action('Crossfade selected clips…',self.transition,menu=clip);self.action('Analyze stabilization',self.stabilize_selected,menu=clip)
        self.action('Edit subtitles…',self.captions,menu=captions);self.action('Import SRT…',self.import_srt,menu=captions);self.action('Export SRT…',self.export_srt,menu=captions);self.action('Automatic subtitles · offline',self.automatic_captions,menu=captions)
        self.action('Canvas size / aspect ratio…',self.canvas_settings,menu=view);self.action('Build source proxies',self.proxies,menu=view);self.action('Fit timeline',self.fit_timeline,menu=view);self.action('Fit canvas to visible layers',self.reframe_visible,menu=view)
        self.action('Export timeline…',self.render_settings,'Ctrl+E','export',export,True);self.action('Batch export…',lambda:self.render_settings(True),menu=export);self.action('Save edited frame…',self.save_frame,menu=export)
        self.action('Play / pause',self.toggle_play,'Space');self.action('Previous frame',lambda:self.seek(self.position-1/self.model['fps']),'Left');self.action('Next frame',lambda:self.seek(self.position+1/self.model['fps']),'Right');self.action('Timeline start',lambda:self.seek(0),'Home');self.action('Timeline end',lambda:self.seek(duration(self.model)),'End')
        root=QWidget();self.setCentralWidget(root);vertical=QVBoxLayout(root);vertical.setContentsMargins(12,8,12,8);vertical.setSpacing(8)
        self.error_label=QLabel();self.error_label.setObjectName('error');self.error_label.setWordWrap(True);self.error_label.hide();vertical.addWidget(self.error_label)
        splitter=QSplitter(Qt.Horizontal);vertical.addWidget(splitter,1)
        left=QWidget();ll=QVBoxLayout(left);ll.setContentsMargins(0,0,8,0);ll.addWidget(QLabel('Sources and tracks'));self.bin=QTreeWidget();self.bin.setColumnCount(2);self.bin.setHeaderLabels(['Application / source','Stream']);self.bin.header().setSectionResizeMode(0,QHeaderView.Stretch);self.bin.header().setSectionResizeMode(1,QHeaderView.ResizeToContents);self.bin.setMinimumWidth(260);self.bin.setIndentation(12);self.bin.setAccessibleName('Tracks. Check to show a video track or include an audio track.');self.bin.itemChanged.connect(self.track_changed);self.bin.currentItemChanged.connect(lambda item,old:setattr(self,'selected_track',item.data(0,Qt.UserRole) if item else None));self.bin.setContextMenuPolicy(Qt.CustomContextMenu);self.bin.customContextMenuRequested.connect(self.track_menu);ll.addWidget(self.bin,1)
        layer_note=QLabel('Lower video tracks appear in front.');layer_note.setObjectName('muted');layer_note.setWordWrap(True);ll.addWidget(layer_note)
        self.auto_fit=QCheckBox('Fit remaining displays');self.auto_fit.setChecked(True);self.auto_fit.setToolTip('Reframe the canvas when recorded display tracks are excluded or restored.');ll.addWidget(self.auto_fit)
        addrow=QHBoxLayout();addv=QPushButton('+ Video');adda=QPushButton('+ Audio');addv.clicked.connect(lambda:self.add_track('video'));adda.clicked.connect(lambda:self.add_track('audio'));addrow.addWidget(addv);addrow.addWidget(adda);ll.addLayout(addrow);splitter.addWidget(left)
        center=QWidget();cv=QVBoxLayout(center);cv.setContentsMargins(8,0,8,0);cv.setSpacing(8);self.canvas_controls=QWidget();top=QHBoxLayout(self.canvas_controls);top.setContentsMargins(0,0,0,0);self.canvas_mode=QComboBox()
        for title,key in [('Select object','select'),('Move / resize layer','move'),('Pen','pen'),('Line','line'),('Arrow','arrow'),('Rectangle','rectangle'),('Blackout region','redact'),('Blur region','blur'),('Pixelate region','pixelate'),('Eraser','eraser'),('Crop canvas','crop_canvas')]:self.canvas_mode.addItem(title,key)
        self.canvas_mode.setAccessibleName('Canvas tool');top.addWidget(self.canvas_mode);ink=QPushButton('Ink');ink.clicked.connect(self.choose_ink);top.addWidget(ink);self.width_spin=spin(4,1,64,0);self.width_spin.setMaximumWidth(70);self.width_spin.setAccessibleName('Drawing width');self.width_spin.valueChanged.connect(lambda v:setattr(self,'ink_width',v));top.addWidget(self.width_spin);text=QPushButton('+ Text');text.clicked.connect(self.add_text);top.addWidget(text);top.addStretch();self.quality=QComboBox();self.quality.addItems(['Preview · 960','Preview · 640','Preview · 480']);self.quality.setAccessibleName('Preview quality');self.quality.currentIndexChanged.connect(lambda _:self.frame_timer.start());top.addWidget(self.quality);cv.addWidget(self.canvas_controls)
        self.vertical_split=QSplitter(Qt.Vertical);cv.addWidget(self.vertical_split,1);preview=QWidget();pv=QVBoxLayout(preview);pv.setContentsMargins(0,0,0,0);self.canvas=StudioCanvas(self);pv.addWidget(self.canvas,1)
        transport=QHBoxLayout();self.play_button=QPushButton('Play');self.play_button.clicked.connect(self.toggle_play);transport.addWidget(self.play_button)
        for title,delta in [('− Frame',-1),('+ Frame',1)]:
            b=QPushButton(title);b.clicked.connect(lambda checked=False,delta=delta:self.seek(self.position+delta/self.model['fps']));transport.addWidget(b)
        self.seek_slider=QSlider(Qt.Horizontal);self.seek_slider.setAccessibleName('Timeline playhead');self.seek_slider.sliderMoved.connect(lambda v:self.seek(v/1000));transport.addWidget(self.seek_slider,1);self.clock=QLabel();transport.addWidget(self.clock);pv.addLayout(transport);self.vertical_split.addWidget(preview)
        lower=QWidget();lv=QVBoxLayout(lower);lv.setContentsMargins(0,6,0,0);row=QHBoxLayout();row.addWidget(QLabel('Independent tracks'));self.snap=QCheckBox('Snap');self.snap.setChecked(True);row.addWidget(self.snap);row.addStretch();row.addWidget(QLabel('Zoom'));zoom=QSlider(Qt.Horizontal);zoom.setRange(1,200);zoom.setValue(30);zoom.setMaximumWidth(130);zoom.setAccessibleName('Timeline zoom');row.addWidget(zoom);lv.addLayout(row)
        self.timeline=TrackTimeline(self);zoom.valueChanged.connect(self.change_zoom);self.timeline_scroll=QScrollArea();self.timeline_scroll.setWidget(self.timeline);self.timeline_scroll.setWidgetResizable(False);lv.addWidget(self.timeline_scroll,1);self.vertical_split.addWidget(lower);self.vertical_split.setSizes([500,300]);splitter.addWidget(center)
        self.inspector=QTabWidget();self.inspector.setMinimumWidth(270);self.fields={};self.field_specs={}
        self.timing_tab=self.form_tab('Clip',[
            ('start','Start on timeline',0,100000,0),('in','Source in',0,100000,0),('length','Source length',.01,100000,1),('speed','Speed',.25,4,1),('hold_duration','Freeze duration',.04,600,1)],['reverse','freeze'])
        self.clip_name=QLineEdit();self.clip_name.setAccessibleName('Clip name');self.timing_tab.insertRow(0,'Clip name',self.clip_name)
        key=QPushButton('Animation keyframes…');key.clicked.connect(lambda:self.keyframes());self.timing_tab.addRow(key)
        rate=QPushButton('Variable speed segments…');rate.clicked.connect(self.speed_segments);self.timing_tab.addRow(rate)
        self.video_form=self.form_tab('Video',[
            ('x','Left',-16384,16384,0),('y','Top',-16384,16384,0),('w','Width',2,16384,1920),('h','Height',2,16384,1080),('crop_x','Crop left',0,1,0),('crop_y','Crop top',0,1,0),('crop_w','Crop width',.01,1,1),('crop_h','Crop height',.01,1,1),('rotation','Rotation °',-360,360,0),('opacity','Opacity',0,1,1),('brightness','Brightness',-1,1,0),('contrast','Contrast',0,3,1),('saturation','Saturation',0,3,1),('gamma','Gamma',.1,5,1),('temperature','Warmth',-.5,.5,0),('tint','Tint',-.5,.5,0),('sharpness','Sharpness',-1,3,0),('border_width','Border width',0,100,0)],['flip_x','flip_y','blackout'])
        for key in ('crop_x','crop_y','crop_w','crop_h'):self.fields[key].setRange(.1 if key in ('crop_w','crop_h') else 0,100);self.fields[key].setSuffix(' %')
        self.lut=QLineEdit();self.lut.setReadOnly(True);self.video_form.addRow('3D LUT',self.lut);lut=QPushButton('Choose LUT…');lut.clicked.connect(self.choose_lut);self.video_form.addRow(lut);border=QPushButton('Border color…');border.clicked.connect(self.choose_border);self.video_form.addRow(border)
        stabilize=QPushButton('Analyze stabilization');stabilize.clicked.connect(self.stabilize_selected);self.video_form.addRow(stabilize)
        layouts=QComboBox();layouts.addItems(['Layout preset…','Fit canvas','Left half','Right half','Picture in picture']);layouts.activated.connect(self.layout_preset);self.video_form.addRow('Placement',layouts)
        self.audio_form=self.form_tab('Audio',[('gain','Level',0,4,1),('fade_in','Fade in (s)',0,600,0),('fade_out','Fade out (s)',0,600,0)],['denoise','normalize','mute'])
        self.objects=QWidget();ov=QVBoxLayout(self.objects);ov.setContentsMargins(10,10,10,10);self.object_list=QListWidget();self.object_list.setAccessibleName('Titles, drawings and privacy regions');self.object_list.currentItemChanged.connect(self.object_selected);ov.addWidget(self.object_list)
        for title,callback in [('Add text',self.add_text),('Add privacy region…',self.add_privacy),('Edit selected object',self.edit_annotation),('Track selected privacy region',self.track_privacy),('Delete selected object',self.delete_object)]:
            button=QPushButton(title);button.clicked.connect(callback);ov.addWidget(button)
        self.inspector.addTab(self.objects,'Objects');splitter.addWidget(self.inspector);splitter.setSizes([300,800,310])
        self.progress_row=QWidget();progress=QHBoxLayout(self.progress_row);progress.setContentsMargins(0,0,0,0);self.job_label=QLabel();progress.addWidget(self.job_label);self.progress=QProgressBar();progress.addWidget(self.progress,1);cancel=QPushButton('Cancel');cancel.clicked.connect(self.cancel_job);progress.addWidget(cancel);self.progress_row.hide();vertical.addWidget(self.progress_row)
        self.statusBar().showMessage('Select a clip to edit. Control-click selects multiple clips. Edits preserve original media.')

    def form_tab(self,title,specs,checks):
        scroll=QScrollArea();scroll.setWidgetResizable(True);body=QWidget();form=QFormLayout(body);form.setContentsMargins(12,12,12,12)
        for key,label,minimum,maximum,default in specs:
            field=spin(default,minimum,maximum);field.setAccessibleName(label);self.fields[key]=field;self.field_specs[key]=default;form.addRow(label,field)
        for key in checks:
            field=QCheckBox(key.replace('_',' ').capitalize());self.fields[key]=field;form.addRow(field)
        apply=QPushButton('Apply clip settings');apply.clicked.connect(self.apply_fields);form.addRow(apply);scroll.setWidget(body);self.inspector.addTab(scroll,title);return form

    def checkpoint(self):self.undo_stack.append((copy.deepcopy(self.project['sources']),copy.deepcopy(self.model)));self.undo_stack=self.undo_stack[-60:];self.redo_stack=[]
    def changed(self):
        try:validate(self.model,self.project,allow_empty=True)
        except Exception as e:
            if self.undo_stack:
                self.project['sources'],self.model=self.undo_stack.pop();self.sources={s['id']:s for s in self.project['sources']};self.refresh_all()
            self.error(str(e));return
        self.pause();self.revision+=1;self.project['studio']=self.model;atomic_json(self.path,self.project);self.preview_revision=-1;self.error_label.hide();self.refresh_all();self.frame_timer.start();self.statusBar().showMessage('All edits saved · Original media preserved',2500)
    def refresh_all(self):
        current_track=self.selected_track;self.bin.blockSignals(True);self.bin.clear()
        for kind,title in [('video','Video tracks'),('audio','Audio tracks')]:
            group=QTreeWidgetItem([title]);self.bin.addTopLevelItem(group)
            for track in self.model['tracks']:
                if track['kind']!=kind:continue
                source=self.sources.get(track.get('source_id'),{});parts=track['name'].split(' — ',1)
                display=parts[0];role=parts[1] if len(parts)>1 else ('Microphone' if source.get('kind')=='microphone' else 'Media' if kind=='audio' else 'Display')
                item=QTreeWidgetItem([display,role]);item.setData(0,Qt.UserRole,track['id']);item.setData(0,Qt.UserRole+1,track['name']);item.setData(0,Qt.UserRole+2,display);item.setToolTip(0,track['name']+('\nRecording: '+track['recording'] if track.get('recording') else ''));item.setToolTip(1,role);item.setFlags(item.flags()|Qt.ItemIsUserCheckable|Qt.ItemIsEditable);item.setCheckState(0,Qt.Checked if track.get('visible',True) and not track.get('mute',False) else Qt.Unchecked);group.addChild(item)
                if track['id']==current_track:self.bin.setCurrentItem(item)
            group.setExpanded(True)
        self.bin.blockSignals(False);self.timeline.refresh();self.refresh_objects();self.fill_inspector();self.seek_slider.setRange(0,round(duration(self.model)*1000));self.update_clock();self.undo_action.setEnabled(bool(self.undo_stack));self.redo_action.setEnabled(bool(self.redo_stack));self.canvas.update()
    def refresh_theme(self):
        for action in self.edit_actions:
            glyph=action.property('layerGlyph')
            if glyph:action.setIcon(icon(glyph,self.theme.tokens['on_primary'] if action.text()=='Export timeline…' else self.theme.tokens['text'],18))
        self.timeline.update();self.canvas.update()
    def refresh_objects(self):
        self.object_list.blockSignals(True);self.object_list.clear()
        for a in self.model['annotations']:
            from PySide6.QtWidgets import QListWidgetItem
            item=QListWidgetItem((a.get('text','')[:28] if a['kind']=='text' else a['kind'].capitalize())+' · '+self.timecode(a['start'])+'–'+self.timecode(a['end']));item.setData(Qt.UserRole,a['id']);self.object_list.addItem(item)
            if a['id']==self.annotation_selection:self.object_list.setCurrentItem(item)
        self.object_list.blockSignals(False)
    def object_selected(self,item,previous):
        self.annotation_selection=item.data(Qt.UserRole) if item else None
        if item:self.selection=set();self.fill_inspector();self.timeline.update()
        self.canvas.update()
    def current_clip(self):
        if not self.selection:raise ValueError('Select a timeline clip first.')
        id=self.primary if self.primary in self.selection else next(c['id'] for c in all_clips(self.model) if c['id'] in self.selection)
        return find_clip(self.model,id)[1]
    def fill_inspector(self):
        self.loading=True
        try:
            c=self.current_clip();self.clip_name.setText(c['name']);source=self.sources[c['source_id']]
            for key,field in self.fields.items():
                field.setEnabled(True)
                if isinstance(field,QCheckBox):field.setChecked(c.get(key,False))
                else:
                    value=value_at(c,key,max(0,self.position-c['start']),self.field_specs.get(key,0))
                    field.setValue(value*100 if key.startswith('crop_') else value)
            self.fields['freeze'].setEnabled(source['type']=='video');self.fields['hold_duration'].setEnabled(source['type']=='video')
            self.lut.setText(c.get('lut',''));self.inspector.setTabEnabled(1,source['type']=='video');self.inspector.setTabEnabled(2,source['type']=='audio')
        except ValueError:
            for field in self.fields.values():field.setEnabled(False)
        self.loading=False
    def apply_fields(self):
        if self.loading or self.busy:return
        try:c=self.current_clip()
        except ValueError as e:self.error(str(e));return
        self.checkpoint();before=copy.deepcopy(c)
        from .studio_model import source_offset
        freeze_at=source_offset(c,max(0,self.position-c['start']))
        for key,field in self.fields.items():
            value=field.isChecked() if isinstance(field,QCheckBox) else field.value()/100 if key.startswith('crop_') else field.value()
            if key in before.get('keys',{}) and before['keys'][key]:self.put_key(c,key,max(0,self.position-c['start']),value)
            else:c[key]=value
        if any(c.get(key,default)!=before.get(key,default) for key,default in [('crop_x',0),('crop_y',0),('crop_w',1),('crop_h',1)]):
            if abs(c['w']-before.get('w',c['w']))<.01 and abs(c['h']-before.get('h',c['h']))<.01:
                c['x']+=before.get('w',0)*(c.get('crop_x',0)-before.get('crop_x',0))/before.get('crop_w',1)
                c['y']+=before.get('h',0)*(c.get('crop_y',0)-before.get('crop_y',0))/before.get('crop_h',1)
                c['w']*=c.get('crop_w',1)/before.get('crop_w',1);c['h']*=c.get('crop_h',1)/before.get('crop_h',1)
        c['name']=self.clip_name.text().strip() or c['name']
        c['duration']=c.get('hold_duration',1) if c.get('freeze') else clip_duration(c)
        if any(c.get(k)!=before.get(k) for k in ('fade_in','fade_out','speed')):c['effect_offset']=0;c['effect_length']=clip_duration(c)
        if c.get('freeze') and not before.get('freeze'):c['freeze_at']=freeze_at
        if c['in']!=before['in'] or c['length']!=before['length']:c.pop('stabilization',None)
        self.changed()
    def track_changed(self,item,column):
        id=item.data(0,Qt.UserRole)
        if not id:return
        tr=next(t for t in self.model['tracks'] if t['id']==id);self.checkpoint()
        if item.text(0)!=item.data(0,Qt.UserRole+2) or column==1:tr['name']=item.text(0)+' — '+item.text(1)
        checked=item.checkState(0)==Qt.Checked
        tr['mute']=not checked if tr['kind']=='audio' else False;tr['visible']=checked
        if tr['kind']=='video' and self.auto_fit.isChecked() and self.sources.get(tr.get('source_id'),{}).get('backend'):self.reframe_visible(commit=False)
        # The native item must outlive its itemChanged signal dispatch.
        QTimer.singleShot(0,self.changed)
    def track_menu(self,point):
        item=self.bin.itemAt(point)
        if not item or not item.data(0,Qt.UserRole):return
        self.selected_track=item.data(0,Qt.UserRole)
        from PySide6.QtWidgets import QMenu
        menu=QMenu(self);menu.addAction('Move track forward / down',lambda:self.move_track(1));menu.addAction('Move track backward / up',lambda:self.move_track(-1));menu.exec(self.bin.viewport().mapToGlobal(point))
    def move_track(self,delta):
        selected=next((t for t in self.model['tracks'] if t['id']==self.selected_track),None)
        if not selected:return
        peers=[t for t in self.model['tracks'] if t['kind']==selected['kind']];i=peers.index(selected);j=i+delta
        if not 0<=j<len(peers):return
        self.checkpoint();a=self.model['tracks'].index(selected);b=self.model['tracks'].index(peers[j]);self.model['tracks'][a],self.model['tracks'][b]=self.model['tracks'][b],self.model['tracks'][a];self.changed()
    def add_privacy(self):
        kind,ok=QInputDialog.getItem(self,'Privacy region','Effect',['Blackout','Blur','Pixelate'],0,False)
        if not ok:return
        self.checkpoint();a={'id':uid(),'kind':{'Blackout':'redact','Blur':'blur','Pixelate':'pixelate'}[kind],'start':self.position,'end':max(self.position+1,duration(self.model)),'x':self.model['width']*.25,'y':self.model['height']*.25,'w':self.model['width']*.5,'h':self.model['height']*.5,'keys':{}}
        self.model['annotations'].append(a);self.annotation_selection=a['id'];self.selection=set();self.inspector.setCurrentWidget(self.objects);self.changed();self.edit_annotation()

    def add_track(self,kind):self.checkpoint();self.model['tracks'].append({'id':uid(),'name':'Video' if kind=='video' else 'Audio','kind':kind,'visible':True,'mute':False,'clips':[]});self.changed()
    def select_all(self):self.selection={c['id'] for c in all_clips(self.model)};self.fill_inspector();self.timeline.update()
    def split_selected(self):
        if not self.selection:return
        self.checkpoint();new=[]
        try:
            for id in list(self.selection):new.append(split_clip(self.model,id,self.position))
            self.selection=set(new);self.changed()
        except ValueError as e:self.error(str(e))
    def delete_selected(self):
        if not self.selection:
            if self.annotation_selection:self.delete_object()
            return
        self.checkpoint()
        for track in self.model['tracks']:track['clips']=[c for c in track['clips'] if c['id'] not in self.selection]
        self.selection=set();self.changed()
    def copy_clips(self):self.clipboard=[(tr['id'],copy.deepcopy(c)) for tr in self.model['tracks'] for c in tr['clips'] if c['id'] in self.selection]
    def paste_clips(self):
        if not self.clipboard:return
        self.checkpoint();first=min(c['start'] for tr,c in self.clipboard);selected=set()
        for trackid,c in self.clipboard:
            track=next((tr for tr in self.model['tracks'] if tr['id']==trackid),None)
            if track:
                item=copy.deepcopy(c);item.update(id=uid(),start=self.position+c['start']-first);track['clips'].append(item);selected.add(item['id'])
        self.selection=selected;self.changed()
    def duplicate(self):
        if not self.selection:return
        self.copy_clips();self.position=max(find_clip(self.model,id)[1]['start']+clip_duration(find_clip(self.model,id)[1]) for id in self.selection);self.paste_clips()
    def undo(self):
        if not self.undo_stack or self.busy:return
        self.redo_stack.append((copy.deepcopy(self.project['sources']),copy.deepcopy(self.model)));self.project['sources'],self.model=self.undo_stack.pop();self.sources={s['id']:s for s in self.project['sources']};self.selection.intersection_update({c['id'] for c in all_clips(self.model)});saved=self.redo_stack;self.changed();self.redo_stack=saved;self.redo_action.setEnabled(True)
    def redo(self):
        if not self.redo_stack or self.busy:return
        self.undo_stack.append((copy.deepcopy(self.project['sources']),copy.deepcopy(self.model)));self.project['sources'],self.model=self.redo_stack.pop();self.sources={s['id']:s for s in self.project['sources']};self.changed()
    def put_key(self,obj,key,time,value):
        points=obj.setdefault('keys',{}).setdefault(key,[]);points[:]=[[t,v] for t,v in points if abs(t-time)>.001]+[[time,value]];points.sort()
    def transition(self):
        if len(self.selection)!=2:self.error('Select two clips to crossfade.');return
        a,b=sorted([find_clip(self.model,id)[1] for id in self.selection],key=lambda c:c['start']);seconds,ok=QInputDialog.getDouble(self,'Crossfade','Overlap duration (seconds)',.5,.04,min(clip_duration(a),clip_duration(b)),2)
        if ok:
            kind_a=self.sources[a['source_id']]['type'];kind_b=self.sources[b['source_id']]['type']
            if kind_a!=kind_b:self.error('Choose two video clips or two audio clips for a crossfade.');return
            self.checkpoint();b['start']=a['start']+clip_duration(a)-seconds;a['fade_out']=seconds if kind_a=='audio' else 0;b['fade_in']=seconds;self.changed()
    def delete_object(self):
        if self.annotation_selection:self.checkpoint();self.model['annotations']=[a for a in self.model['annotations'] if a['id']!=self.annotation_selection];self.annotation_selection=None;self.changed()
    def choose_ink(self):
        color=QColorDialog.getColor(QColor(self.ink),self,'Drawing ink')
        if color.isValid():self.ink=color.name()
    def choose_lut(self):
        try:c=self.current_clip()
        except ValueError as e:self.error(str(e));return
        path,_=QFileDialog.getOpenFileName(self,'3D LUT',str(self.path.parent),'3D LUT (*.cube *.3dl *.dat)')
        if path:self.checkpoint();c['lut']=path;self.changed()
    def choose_border(self):
        try:c=self.current_clip()
        except ValueError as e:self.error(str(e));return
        color=QColorDialog.getColor(QColor(c.get('border_color','#ffffff')),self,'Border color')
        if color.isValid():self.checkpoint();c['border_color']=color.name();self.changed()
    def layout_preset(self,index):
        if not index:return
        try:c=self.current_clip()
        except ValueError as e:self.error(str(e));return
        self.checkpoint();w,h=self.model['width'],self.model['height'];source=self.sources[c['source_id']];aspect=source.get('width',w)/source.get('height',h)
        if index in (1,2,3):
            region_w=w if index==1 else w/2;region_x=0 if index in (1,2) else w/2
            factor=min(region_w/source.get('width',w),h/source.get('height',h));sw=source.get('width',w)*factor;sh=source.get('height',h)*factor;c.update(x=region_x+(region_w-sw)/2,y=(h-sh)/2,w=sw,h=sh)
        else:c.update(x=w*.65,y=h*.65,w=w*.3,h=w*.3/aspect)
        for key in ('x','y','w','h'):c.get('keys',{}).pop(key,None)
        self.changed()
    def crop_canvas(self,rect,checkpoint=True,commit=True):
        x,y,w,h=rect
        if w<2 or h<2:return
        if checkpoint:self.checkpoint()
        for obj in all_clips(self.model)+self.model['annotations']:
            if 'x' in obj:obj['x']-=x
            if 'y' in obj:obj['y']-=y
            for key,amount in [('x',x),('y',y)]:
                if key in obj.get('keys',{}):obj['keys'][key]=[[t,value-amount] for t,value in obj['keys'][key]]
        self.model['width']=max(2,round(w/2)*2);self.model['height']=max(2,round(h/2)*2)
        if commit:self.changed()

    def reframe_visible(self,commit=True):
        import math
        clips=[c for track in self.model['tracks'] if track['kind']=='video' and track.get('visible',True) for c in track['clips'] if c['start']<=self.position<c['start']+clip_duration(c)]
        if not clips:return
        boxes=[]
        for c in clips:
            t=self.position-c['start'];x,y,w,h=[value_at(c,k,t,d) for k,d in [('x',0),('y',0),('w',100),('h',100)]];angle=math.radians(value_at(c,'rotation',t,0));rw=abs(w*math.cos(angle))+abs(h*math.sin(angle));rh=abs(h*math.cos(angle))+abs(w*math.sin(angle));boxes.append((x+(w-rw)/2,y+(h-rh)/2,rw,rh))
        left=min(b[0] for b in boxes);top=min(b[1] for b in boxes);width=max(x+w for x,y,w,h in boxes)-left;height=max(y+h for x,y,w,h in boxes)-top
        self.crop_canvas((left,top,width,height),checkpoint=commit,commit=commit)

    def change_zoom(self,value):self.timeline.zoom=value;self.timeline.refresh()
    def fit_timeline(self):self.timeline.zoom=max(1,min(200,(self.timeline_scroll.viewport().width()-170)/max(1,duration(self.model))));self.timeline.refresh()
    def seek(self,seconds):
        self.position=max(0,min(float(seconds),max(0,duration(self.model)-1/self.model['fps'])));self.update_clock();self.timeline.update();self.canvas.update()
        if not self.playing:self.fill_inspector()
        if self.preview_revision==self.revision:self.player.setPosition(round(self.position*1000))
        if not self.playing:self.frame_timer.start()
    def update_clock(self):self.clock.setText(self.timecode(self.position)+' / '+self.timecode(duration(self.model)));self.seek_slider.setValue(round(self.position*1000))
    def pause(self):
        was_playing=self.playing;self.playing=False;self.player.pause();self.play_button.setText('Play');self.fill_inspector()
        if was_playing and not self.closing:self.frame_timer.start()
    def toggle_play(self):
        if self.playing:self.pause();return
        if self.preview_revision!=self.revision:
            model=copy.deepcopy(self.model);revision=self.revision
            def ready(file):
                if revision!=self.revision:return
                self.preview_path=file;self.preview_revision=revision;self.player.setSource(QUrl.fromLocalFile(file));self.player.setPosition(round(self.position*1000));self.player.play();self.playing=True;self.play_button.setText('Pause')
            width=[960,640,480][self.quality.currentIndex()]
            self.run_job('Building smooth preview…',lambda task:preview_file(self.path,model,task.cancel,task.progress.emit,width),ready)
        else:self.player.play();self.playing=True;self.play_button.setText('Pause')
    def player_position(self,milliseconds):
        if self.playing:self.position=milliseconds/1000;self.update_clock();self.timeline.update();self.canvas.update()
    def player_frame(self,frame):
        if frame.isValid() and self.playing and self.preview_revision==self.revision:self.canvas.image=frame.toImage();self.canvas.update()
    def request_frame(self):
        if self.closing or self.playing:return
        if duration(self.model)<=0:
            image=QImage(960,640,QImage.Format_RGB32);image.fill(QColor('#000000'));self.canvas.image=image;self.canvas.update();return
        if self.frame_task:
            self.frame_task.cancel.set();self.frame_pending=True;return
        from .ui import Task
        model=copy.deepcopy(self.model);revision=self.revision;position=self.position;width=[960,640,480][self.quality.currentIndex()]
        task=Task(lambda task:frame_image(self.path,model,position,task.cancel,width),self);self.frame_task=task;self.tasks.append(task)
        def success(image):
            if revision==self.revision and abs(position-self.position)<.05:self.canvas.image=image;self.canvas.update()
        def failure(error):
            if not task.cancel.is_set() and revision==self.revision:self.error(error)
        def done():
            self.frame_task=None;self.tasks.remove(task);task.deleteLater()
            if self.frame_pending:self.frame_pending=False;self.frame_timer.start()
        task.success.connect(success);task.failure.connect(failure);task.finished.connect(done);task.start()
    def run_job(self,title,function,success):
        if self.busy:self.error('Wait for the current operation or cancel it first.');return
        from .ui import Task
        self.pause();self.busy=True;self.progress_row.show();self.job_label.setText(title);self.progress.setValue(0);self.inspector.setEnabled(False);self.timeline.setEnabled(False);self.tools.setEnabled(False);self.canvas.setEnabled(False);self.bin.setEnabled(False);self.canvas_controls.setEnabled(False)
        for action in self.edit_actions:action.setEnabled(False)
        task=Task(function,self);self.job=task;self.tasks.append(task);task.progress.connect(lambda p:self.progress.setValue(round(p*100)))
        task.success.connect(success);task.failure.connect(self.error)
        def done():
            self.busy=False;self.inspector.setEnabled(True);self.timeline.setEnabled(True);self.tools.setEnabled(True);self.canvas.setEnabled(True);self.bin.setEnabled(True);self.canvas_controls.setEnabled(True);self.progress_row.hide()
            for action in self.edit_actions:action.setEnabled(True)
            self.undo_action.setEnabled(bool(self.undo_stack));self.redo_action.setEnabled(bool(self.redo_stack));self.tasks.remove(task);task.deleteLater();self.job=None
        task.finished.connect(done);task.start()
    def cancel_job(self):
        if getattr(self,'job',None):self.job.cancel.set()
    def proxies(self):self.run_job('Building source proxies…',lambda task:build_proxies(self.path,task.cancel,task.progress.emit),lambda count:self.statusBar().showMessage(f'{count} video proxies ready.',8000))
    def load_waves(self):
        from .ui import Task
        project=copy.deepcopy(self.project);task=Task(lambda task:derivatives(self.path.parent,project),self);self.tasks.append(task)
        task.success.connect(lambda result:(setattr(self,'waves',result['waves']),self.timeline.update()))
        task.finished.connect(lambda:(self.tasks.remove(task),task.deleteLater()));task.start()
    def save_frame(self):
        path,_=QFileDialog.getSaveFileName(self,'Save edited frame',str(self.path.parent/'studio-frame.png'),'PNG image (*.png)')
        if path:
            model=copy.deepcopy(self.model);position=self.position;self.run_job('Saving edited frame…',lambda task:export_studio(self.path,path,{},task.progress.emit,task.cancel,model,(position,position+.05),True),lambda file:self.statusBar().showMessage('Frame saved: '+file,8000))
    def error(self,text):self.error_label.setText(text);self.error_label.show();self.statusBar().showMessage(text,8000)
    def closeEvent(self,event):
        self.closing=True;self.pause();self.frame_timer.stop()
        for task in list(self.tasks):task.cancel.set()
        for task in list(self.tasks):task.wait(15000)
        self.player.stop();self.closed.emit();event.accept()
