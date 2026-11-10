"""Native editors for animation, titles, subtitles, render settings and analysis."""
import copy
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QDialogButtonBox,QTableWidget,QTableWidgetItem,QComboBox,QLabel,QPushButton,QDoubleSpinBox,QSpinBox,QCheckBox,QLineEdit,QTextEdit,QFontComboBox,QColorDialog,QFileDialog,QInputDialog,QListWidget)
from .studio_model import value_at,clip_duration,duration,uid,parse_srt,write_srt,import_sources
from .studio_render import export_studio


def spin(value,minimum=-16384,maximum=16384,decimals=2):
    s=QDoubleSpinBox();s.setRange(minimum,maximum);s.setDecimals(decimals);s.setValue(value);return s


def dialog_buttons(dialog,layout):
    b=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel);b.accepted.connect(dialog.accept);b.rejected.connect(dialog.reject);layout.addWidget(b);return b


class StudioDialogs:
    def keyframes(self,obj=None):
        self.pause();external=obj is not None
        if obj is None:
            try:obj=self.current_clip()
            except ValueError as e:self.error(str(e));return
        d=QDialog(self);d.setWindowTitle('Animation keyframes');d.resize(580,420);v=QVBoxLayout(d)
        prop=QComboBox();properties=(['gain'] if self.sources[obj['source_id']]['type']=='audio' else ['x','y','w','h','rotation','opacity']) if 'source_id' in obj else ['x','y','w','h']+([] if obj.get('kind') in ('redact','blur','pixelate') else ['opacity']);prop.addItems(properties);v.addWidget(prop)
        table=QTableWidget(0,2);table.setHorizontalHeaderLabels(['Time in clip (s)','Value']);table.horizontalHeader().setStretchLastSection(True);v.addWidget(table)
        values=copy.deepcopy(obj.get('keys',{}));previous=[None]
        def store():
            if previous[0] is not None:values[previous[0]]=[[float(table.item(i,0).text()),float(table.item(i,1).text())] for i in range(table.rowCount())]
        def load():
            try:store()
            except (ValueError,AttributeError):pass
            name=prop.currentText();previous[0]=name;rows=values.get(name,[]);table.setRowCount(len(rows))
            for i,(t,value) in enumerate(rows):table.setItem(i,0,QTableWidgetItem(str(t)));table.setItem(i,1,QTableWidgetItem(str(value)))
        prop.currentIndexChanged.connect(load);load();row=QHBoxLayout();v.addLayout(row)
        def add():
            i=table.rowCount();table.insertRow(i);local=max(0,self.position-obj.get('start',0));table.setItem(i,0,QTableWidgetItem(f'{local:.3f}'));table.setItem(i,1,QTableWidgetItem(str(value_at(obj,prop.currentText(),local,1))))
        a=QPushButton('Add at playhead');a.clicked.connect(add);row.addWidget(a);remove=QPushButton('Remove selected');remove.clicked.connect(lambda:table.removeRow(table.currentRow()) if table.currentRow()>=0 else None);row.addWidget(remove)
        note=QLabel('Values interpolate linearly. X/Y/width/height use canvas pixels; opacity and gain use 0–1.');note.setWordWrap(True);v.addWidget(note);dialog_buttons(d,v)
        if d.exec():
            try:
                store();values={k:sorted(rows) for k,rows in values.items() if rows}
                if not external:self.checkpoint()
                obj['keys']=values
                if not external:self.changed()
            except (ValueError,AttributeError):self.error('Enter valid numeric keyframe times and values.')
        d.deleteLater()

    def speed_segments(self):
        try:c=self.current_clip()
        except ValueError as e:self.error(str(e));return
        d=QDialog(self);d.setWindowTitle('Variable speed segments');v=QVBoxLayout(d);table=QTableWidget(0,2);table.setHorizontalHeaderLabels(['Source offset (s)','Speed (0.25–4×)']);v.addWidget(table)
        keys=c.get('speed_keys') or [[0,c.get('speed',1)]];table.setRowCount(len(keys))
        for i,pair in enumerate(keys):
            for col,value in enumerate(pair):table.setItem(i,col,QTableWidgetItem(str(value)))
        row=QHBoxLayout();add=QPushButton('Add segment');add.clicked.connect(lambda:(table.insertRow(table.rowCount()),table.setItem(table.rowCount()-1,0,QTableWidgetItem('0')),table.setItem(table.rowCount()-1,1,QTableWidgetItem('1'))));row.addWidget(add);remove=QPushButton('Remove');remove.clicked.connect(lambda:table.removeRow(table.currentRow()) if table.currentRow()>=0 else None);row.addWidget(remove);v.addLayout(row)
        note=QLabel('Each rate starts at its source offset and remains active until the next segment. Audio pitch is preserved.');note.setWordWrap(True);v.addWidget(note);dialog_buttons(d,v)
        if d.exec():
            try:
                keys=sorted([[float(table.item(i,0).text()),float(table.item(i,1).text())] for i in range(table.rowCount())]);assert all(0<=t<c['length'] and .25<=rate<=4 for t,rate in keys)
                self.checkpoint();c['speed_keys']=keys;c['duration']=clip_duration(c);c['effect_offset']=0;c['effect_length']=c['duration'];self.changed()
            except (ValueError,AttributeError,AssertionError):self.error('Use source offsets inside the clip and rates between 0.25× and 4×.')
        d.deleteLater()

    def add_text(self):
        text,ok=QInputDialog.getMultiLineText(self,'Add title','Title text:','')
        if ok and text.strip():
            self.checkpoint();ann={'id':uid(),'kind':'text','text':text,'font':'Noto Sans','font_size':48,'color':self.ink,'start':self.position,'end':max(self.position+1,duration(self.model)),'x':self.model['width']*.1,'y':self.model['height']*.1,'w':self.model['width']*.6,'h':120,'opacity':1,'keys':{}}
            self.model['annotations'].append(ann);self.annotation_selection=ann['id'];self.selection=set();self.inspector.setCurrentWidget(self.objects);self.changed();self.edit_annotation()

    def edit_annotation(self):
        original=next((a for a in self.model['annotations'] if a['id']==self.annotation_selection),None)
        if not original:self.error('Select an object in the Objects panel first.');return
        item=copy.deepcopy(original);d=QDialog(self);d.setWindowTitle('Object and privacy settings');d.resize(480,560);v=QVBoxLayout(d);form=QFormLayout();v.addLayout(form);fields={}
        for key,title,default in [('start','Start',0),('end','End',duration(self.model)),('x','Left',0),('y','Top',0),('w','Width',100),('h','Height',100),('font_size','Font size',48),('opacity','Opacity',1)]:
            if key=='font_size' and item['kind']!='text' or key=='opacity' and item['kind'] in ('redact','blur','pixelate'):continue
            value=spin(item.get(key,default),0 if key not in ('x','y') else -16384,1 if key=='opacity' else 100000);form.addRow(title,value);fields[key]=value
        font=QFontComboBox();font.setCurrentFont(__import__('PySide6.QtGui',fromlist=['QFont']).QFont(item.get('font','Noto Sans')));form.addRow('Font',font)
        text=QTextEdit(item.get('text',''));text.setMaximumHeight(100);form.addRow('Text',text)
        if item['kind']!='text':
            font.hide();text.hide();form.labelForField(font).hide();form.labelForField(text).hide()
        color=QPushButton('Choose color');color.clicked.connect(lambda:self.choose_object_color(item));form.addRow('Color',color)
        if item['kind'] in ('redact','blur','pixelate'):color.hide();form.labelForField(color).hide()
        keys=QPushButton('Edit position / size keyframes…');keys.clicked.connect(lambda:self.keyframes(item));v.addWidget(keys)
        hint=QLabel('Move and resize objects directly in Select mode. Privacy masks are opaque, blurred or pixelated for their chosen time range. Tracking estimates must be reviewed over the full range.');hint.setWordWrap(True);v.addWidget(hint);dialog_buttons(d,v)
        if d.exec():
            for key,field in fields.items():item[key]=field.value()
            if item['end']<=item['start'] or min(item['w'],item['h'])<=0:self.error('Give the object a positive duration and size.')
            else:self.checkpoint();item['font']=font.currentFont().family();item['text']=text.toPlainText();original.clear();original.update(item);self.changed()
        d.deleteLater()

    def choose_object_color(self,item):
        value=QColorDialog.getColor(QColor(item.get('color','#ffffff')),self,'Object color')
        if value.isValid():item['color']=value.name()

    def captions(self):
        d=QDialog(self);d.setWindowTitle('Subtitles');d.resize(820,500);v=QVBoxLayout(d);table=QTableWidget(len(self.model['subtitles']),3);table.setHorizontalHeaderLabels(['Start (s)','End (s)','Text']);table.horizontalHeader().setStretchLastSection(True);v.addWidget(table)
        for i,s in enumerate(self.model['subtitles']):
            for col,value in enumerate((s['start'],s['end'],s['text'])):table.setItem(i,col,QTableWidgetItem(str(value)))
        row=QHBoxLayout();add=QPushButton('Add');add.clicked.connect(lambda:(table.insertRow(table.rowCount()),table.setItem(table.rowCount()-1,0,QTableWidgetItem(str(self.position))),table.setItem(table.rowCount()-1,1,QTableWidgetItem(str(self.position+2))),table.setItem(table.rowCount()-1,2,QTableWidgetItem('Caption'))));row.addWidget(add);remove=QPushButton('Remove');remove.clicked.connect(lambda:table.removeRow(table.currentRow()) if table.currentRow()>=0 else None);row.addWidget(remove);v.addLayout(row)
        form=QFormLayout();font=QFontComboBox();size=spin(self.model['subtitle_style'].get('size',36),8,1000,0);form.addRow('Font',font);form.addRow('Size',size);style=copy.deepcopy(self.model['subtitle_style']);color=QPushButton('Choose caption color');color.clicked.connect(lambda:self.choose_object_color(style));form.addRow('Color',color);v.addLayout(form);dialog_buttons(d,v)
        if d.exec():
            try:
                rows=[{'id':uid(),'start':float(table.item(i,0).text()),'end':float(table.item(i,1).text()),'text':table.item(i,2).text()} for i in range(table.rowCount())];assert all(s['end']>s['start']>=0 for s in rows)
                self.checkpoint();self.model['subtitles']=rows;self.model['subtitle_style'].update(font=font.currentFont().family(),size=size.value(),color=style.get('color','#ffffff'));self.changed()
            except (ValueError,AttributeError,AssertionError):self.error('Check subtitle times and fill in every text row.')
        d.deleteLater()

    def import_srt(self):
        path,_=QFileDialog.getOpenFileName(self,'Import subtitles',str(self.path.parent),'SubRip (*.srt)')
        if path:
            try:rows=parse_srt(Path(path).read_text(encoding='utf-8-sig'));assert rows;self.checkpoint();self.model['subtitles']=rows;self.changed()
            except (OSError,AssertionError,ValueError):self.error('No valid subtitle entries were found in this file.')
    def export_srt(self):
        path,_=QFileDialog.getSaveFileName(self,'Export subtitles',str(self.path.parent/'subtitles.srt'),'SubRip (*.srt)')
        if path:
            if Path(path).exists():self.error('Choose a new filename to preserve the existing file.');return
            Path(path).write_text(write_srt(self.model['subtitles']),encoding='utf8')

    def automatic_captions(self):
        from .studio_jobs import transcribe
        d=QDialog(self);d.setWindowTitle('Automatic subtitles');v=QVBoxLayout(d);form=QFormLayout();v.addLayout(form)
        source=QComboBox();source.addItem('Current audible mix',None)
        for track in self.model['tracks']:
            if track['kind']=='audio':source.addItem(track['name'],track['id'])
        language=QComboBox()
        for label,code in [('Auto-detect','auto'),('Turkish','tr'),('English','en'),('German','de'),('French','fr'),('Spanish','es'),('Japanese','ja'),('Chinese','zh')]:language.addItem(label,code)
        form.addRow('Audio',source);form.addRow('Language',language)
        note=QLabel('Runs locally without uploading audio. Choose the voice or microphone track for cleaner captions. Review generated text and timing.');note.setWordWrap(True);v.addWidget(note);dialog_buttons(d,v)
        accepted=d.exec();chosen,lang=source.currentData(),language.currentData();d.deleteLater()
        if not accepted:return
        model=copy.deepcopy(self.model)
        if chosen:
            for track in model['tracks']:
                if track['kind']=='audio':track['mute']=track['id']!=chosen
        def finish(rows):
            self.checkpoint();self.model['subtitles']=rows;self.changed()
            if rows:self.captions()
            else:self.statusBar().showMessage('No speech was detected in the selected audio.',10000)
        self.run_job('Transcribing locally…',lambda task:transcribe(self.path,model,task.cancel,task.progress.emit,lang),finish)

    def track_privacy(self):
        from .studio_jobs import track_region
        ann=next((a for a in self.model['annotations'] if a['id']==self.annotation_selection),None)
        if not ann or ann['kind'] not in ('redact','blur','pixelate'):self.error('Select a privacy region first.');return
        model,item=copy.deepcopy(self.model),copy.deepcopy(ann)
        def finish(keys):self.checkpoint();ann['keys']=keys;self.changed();self.statusBar().showMessage('Tracking applied. Inspect the complete range before exporting.',15000)
        self.run_job('Tracking privacy region…',lambda task:track_region(self.path,model,item,task.cancel,task.progress.emit),finish)

    def stabilize_selected(self):
        from .studio_jobs import stabilize
        try:clip=self.current_clip()
        except ValueError as e:self.error(str(e));return
        if self.sources[clip['source_id']]['type']!='video':self.error('Select a video clip to stabilize.');return
        item=copy.deepcopy(clip)
        def finish(file):self.checkpoint();clip['stabilization']=file;self.changed()
        self.run_job('Analyzing camera motion…',lambda task:stabilize(self.path,item,task.cancel,task.progress.emit),finish)

    def canvas_settings(self):
        d=QDialog(self);d.setWindowTitle('Canvas');v=QVBoxLayout(d);form=QFormLayout();v.addLayout(form)
        width=spin(self.model['width'],2,16384,0);height=spin(self.model['height'],2,16384,0);fps=spin(self.model['fps'],1,120,0);form.addRow('Width',width);form.addRow('Height',height);form.addRow('Frame rate',fps)
        preset=QComboBox();preset.addItems(['Custom','Landscape · 1920 × 1080','Portrait · 1080 × 1920','Square · 1080 × 1080'])
        preset.currentIndexChanged.connect(lambda i:(width.setValue([(0,0),(1920,1080),(1080,1920),(1080,1080)][i][0]),height.setValue([(0,0),(1920,1080),(1080,1920),(1080,1080)][i][1])) if i else None);form.addRow('Preset',preset)
        scale=QCheckBox('Scale existing contents with the canvas');scale.setChecked(True);v.addWidget(scale);dialog_buttons(d,v)
        if d.exec():
            self.checkpoint();sx,sy=width.value()/self.model['width'],height.value()/self.model['height']
            if scale.isChecked():
                for obj in [c for tr in self.model['tracks'] for c in tr['clips']]+self.model['annotations']:
                    for key,factor in [('x',sx),('y',sy),('w',sx),('h',sy)]:
                        if key in obj:obj[key]*=factor
                        if key in obj.get('keys',{}):obj['keys'][key]=[[t,value*factor] for t,value in obj['keys'][key]]
            self.model.update(width=round(width.value()/2)*2,height=round(height.value()/2)*2,fps=round(fps.value()));self.changed()
        d.deleteLater()

    def render_settings(self,batch=False):
        d=QDialog(self);d.setWindowTitle('Batch export' if batch else 'Export timeline');v=QVBoxLayout(d);form=QFormLayout();v.addLayout(form)
        width=spin(self.model['width'],2,16384,0);height=spin(self.model['height'],2,16384,0);fps=spin(self.model['fps'],1,120,0);quality=spin(__import__('katman.common',fromlist=['settings']).settings()['export_quality'],0,51,0)
        codec=QComboBox()
        from .hardware import export_choices
        from .common import settings
        for label,value in export_choices():codec.addItem(label,value)
        configured=settings()['export_codec'];index=codec.findData(configured)
        if index>=0:codec.setCurrentIndex(index)
        audio=QCheckBox('Export audio only');audio.toggled.connect(lambda checked:[field.setEnabled(not checked) for field in (width,height,fps,codec,quality)]);form.addRow('Width',width);form.addRow('Height',height);form.addRow('FPS',fps);form.addRow('Encoder',codec);form.addRow('Quality · lower is better',quality);form.addRow(audio)
        note=QLabel('GPU encoders require hardware support. An unsupported encoder reports an error; existing files are never replaced.');note.setWordWrap(True);v.addWidget(note)
        files=[]
        if batch:
            listing=QListWidget();v.addWidget(listing);add=QPushButton('Add recording projects…');v.addWidget(add)
            def choose():
                paths,_=QFileDialog.getOpenFileNames(d,'Add projects',str(self.path.parent),'Layer projects (*.katman)')
                for path in paths:
                    if path not in files:files.append(path);listing.addItem(Path(path).parent.name)
            add.clicked.connect(choose)
        dialog_buttons(d,v)
        if d.exec():
            options={'size':(max(2,round(width.value()/2)*2),max(2,round(height.value()/2)*2)),'fps':round(fps.value()),'codec':codec.currentData(),'quality':round(quality.value()),'audio_only':audio.isChecked()}
            if batch:
                directory=QFileDialog.getExistingDirectory(self,'Batch output folder',str(self.path.parent))
                if directory and files:
                    from .common import load_project
                    from .studio_model import from_project
                    def render(task):
                        results=[]
                        for i,path in enumerate(files):
                            _,project=load_project(path);dest=Path(directory)/(Path(path).parent.name+('-audio.m4a' if options['audio_only'] else '.mp4'))
                            result=export_studio(path,dest,options,lambda p:task.progress.emit((i+p)/len(files)),task.cancel,from_project(project));results.append(result)
                        return results
                    self.run_job('Exporting batch…',render,lambda paths:self.statusBar().showMessage(f'{len(paths)} exports completed.',10000))
            else:
                path,_=QFileDialog.getSaveFileName(self,'Export timeline',str(self.path.parent/('audio.m4a' if audio.isChecked() else 'edited.mp4')),'Audio (*.m4a *.wav *.mp3)' if audio.isChecked() else 'Video (*.mp4 *.mkv *.mov)')
                if path:
                    model=copy.deepcopy(self.model);self.run_job('Exporting timeline…',lambda task:export_studio(self.path,path,options,task.progress.emit,task.cancel,model),lambda file:self.statusBar().showMessage('Export complete: '+file,12000))
        d.deleteLater()

    def import_media(self,overlay=False):
        paths,_=QFileDialog.getOpenFileNames(self,'Add recording or media',str(self.path.parent),'Media (*.katman *.mp4 *.mkv *.mov *.webm *.wav *.flac *.mp3 *.m4a *.png *.jpg *.webp)')
        if not paths:return
        project,model=copy.deepcopy(self.project),copy.deepcopy(self.model);at=self.position if overlay else duration(model)
        def work(task):
            local_at=at
            for file in paths:
                import_sources(self.path,project,model,file,local_at)
                if not overlay:local_at=duration(model)
                if task.cancel.is_set():raise RuntimeError('Import cancelled.')
            return project,model
        def finish(result):self.checkpoint();self.project,self.model=result;self.sources={s['id']:s for s in self.project['sources']};self.changed();self.load_waves()
        self.run_job('Importing independent media tracks…',work,finish)

    def record_voiceover(self):
        from .common import audio_sources,graph,probe
        from .studio_model import import_sources
        devices=[v for v in audio_sources(graph()).values() if v['kind']=='microphone']
        if not devices:self.error('No microphone source is available.');return
        d=QDialog(self);d.setWindowTitle('Record voiceover');v=QVBoxLayout(d);device=QComboBox()
        for source in devices:device.addItem(source['name'],source['nodes'][0])
        v.addWidget(device);state=QLabel('Ready. The new audio clip starts at the current playhead.');state.setWordWrap(True);v.addWidget(state);record=QPushButton('Record');stop=QPushButton('Stop');stop.setEnabled(False);v.addWidget(record);v.addWidget(stop);buttons=dialog_buttons(d,v);buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        temp=tempfile.TemporaryDirectory(prefix='layer-voice-');file=Path(temp.name)/'Voiceover.wav';process=[None];at=self.position
        def begin():
            self.pause();process[0]=subprocess.Popen(['pw-record','--target',str(device.currentData()),'--rate','48000','--channels','2',str(file)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);record.setEnabled(False);stop.setEnabled(True);device.setEnabled(False);state.setText('Recording microphone…')
        def end():
            if process[0] and process[0].poll() is None:process[0].send_signal(signal.SIGINT);process[0].wait(timeout=10)
            stop.setEnabled(False);state.setText('Recording stopped. Choose OK to add the voiceover.');buttons.button(QDialogButtonBox.Ok).setEnabled(file.exists() and file.stat().st_size>1000)
        record.clicked.connect(begin);stop.clicked.connect(end)
        accepted=d.exec();end()
        if accepted and file.exists():
            try:self.checkpoint();import_sources(self.path,self.project,self.model,file,at);self.sources={s['id']:s for s in self.project['sources']};self.changed();self.load_waves()
            except Exception as e:self.error(str(e))
        temp.cleanup();d.deleteLater()
