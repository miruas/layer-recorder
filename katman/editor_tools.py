"""Native clip, canvas, and assembly tools for the Layer window."""
import copy
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QDialogButtonBox,QLabel,QDoubleSpinBox,QColorDialog,QFileDialog,QComboBox)
from .canvas import Preview
from .editing import scene_layout,join_next,import_recording


class EditorTools:
    def selected_clip(self):return self.project['sections'][self.section_index]

    def set_canvas_tool(self):
        self.preview.tool=self.canvas_tool.currentData();self.preview.stroke=None;self.preview.drag=None
        self.preview.setCursor(Qt.ArrowCursor if self.preview.tool=='view' else Qt.CrossCursor)
        if self.preview.tool!='view':self.playback.pause()
        self.preview.update()

    def choose_ink(self):
        color=QColorDialog.getColor(QColor(self.preview.ink),self,'Drawing color')
        if color.isValid():self.preview.ink=color.name();self.ink_button.setToolTip('Drawing color: '+color.name())

    def add_stroke(self,stroke):
        if not self.project:return
        self.checkpoint();self.selected_clip().setdefault('annotations',[]).append(stroke);self.save_project()

    def remove_stroke(self,index):
        if not self.project:return
        strokes=self.selected_clip().get('annotations',[])
        if 0<=index<len(strokes):self.checkpoint();strokes.pop(index);self.save_project()

    def move_display(self,id,dx,dy):
        if not self.project or abs(dx)+abs(dy)<1:return
        sec=self.selected_clip();videos=[s for s in self.project['sources'] if s['type']=='video' and s['id'] in sec['enabled_ids']]
        _,_,rects=scene_layout(videos,sec)
        if id not in rects:return
        self.checkpoint();x,y,w,h=rects[id];sec.setdefault('placements',{})[id]=[round(x+dx),round(y+dy),w,h]
        self.save_project();self.render_sections()

    def blackout_display(self,id,checked):
        self.checkpoint();sec=self.selected_clip();ids=set(sec.get('blackout_ids',[]))
        ids.add(id) if checked else ids.discard(id)
        sec['blackout_ids']=list(ids);self.save_project()

    def display_placement(self,id):
        if not self.project:return
        self.playback.pause();sec=self.selected_clip();videos=[s for s in self.project['sources'] if s['type']=='video' and s['id'] in sec['enabled_ids']]
        _,_,rects=scene_layout(videos,sec)
        if id not in rects:return
        d=QDialog(self);d.setWindowTitle('Display placement');v=QVBoxLayout(d);form=QFormLayout();v.addLayout(form)
        spins=[]
        for name,value in zip(('Left','Top','Width','Height'),rects[id]):
            spin=QDoubleSpinBox();spin.setRange(-16384 if len(spins)<2 else 2,16384);spin.setDecimals(0);spin.setSuffix(' px');spin.setValue(value);form.addRow(name,spin);spins.append(spin)
        order=list(sec.get('layer_order',[s['id'] for s in videos]));order=[i for i in order if i in rects]
        for s in videos:
            if s['id'] not in order:order.append(s['id'])
        stack=QComboBox()
        for i in range(len(order)):stack.addItem(f'{i+1} · '+('Back' if i==0 else 'Front' if i==len(order)-1 else 'Middle'))
        stack.setCurrentIndex(order.index(id));form.addRow('Stacking order',stack)
        note=QLabel('Position and size apply to this clip. Overlapping displays follow the stacking order.');note.setWordWrap(True);v.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel|QDialogButtonBox.Reset);v.addWidget(buttons);buttons.accepted.connect(d.accept);buttons.rejected.connect(d.reject)
        def reset():
            from .export import layout
            rect=layout(videos)[2][id]
            for spin,value in zip(spins,rect):spin.setValue(value)
        buttons.button(QDialogButtonBox.Reset).clicked.connect(reset)
        if d.exec():
            self.checkpoint();sec.setdefault('placements',{})[id]=[s.value() for s in spins];order.remove(id);order.insert(stack.currentIndex(),id);sec['layer_order']=order
            self.save_project();self.render_sections();self.preview.update()
        d.deleteLater()

    def region_dialog(self,title,initial,full_frame=True):
        self.playback.pause();d=QDialog(self);d.setWindowTitle(title);d.resize(820,580);v=QVBoxLayout(d)
        preview=Preview(self.playback,self.theme,d);preview.frames=self.preview.frames;preview.show_crop=True;preview.crop_on_full=full_frame;preview.tool='crop';preview.pending_crop=list(initial);v.addWidget(preview,1)
        hint=QLabel('Drag a rectangle or enter percentages. Changes apply only to the selected clip.');hint.setWordWrap(True);v.addWidget(hint)
        row=QHBoxLayout();spins=[]
        for name,value in zip(('Left','Top','Width','Height'),initial):
            col=QVBoxLayout();col.addWidget(QLabel(name));spin=QDoubleSpinBox();spin.setAccessibleName(name+' percentage');spin.setRange(0,100);spin.setDecimals(2);spin.setSuffix(' %');spin.setValue(value*100);col.addWidget(spin);row.addLayout(col);spins.append(spin)
        v.addLayout(row);error=QLabel();error.setWordWrap(True);v.addWidget(error)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel|QDialogButtonBox.Reset);v.addWidget(buttons);buttons.accepted.connect(d.accept);buttons.rejected.connect(d.reject)
        def changed():
            values=[s.value()/100 for s in spins];x,y,w,h=values;valid=w>=.002 and h>=.002 and x+w<=1.000001 and y+h<=1.000001
            buttons.button(QDialogButtonBox.Ok).setEnabled(valid);error.setText('Keep the rectangle inside the frame and give it a non-zero size.' if not valid else '')
            preview.pending_crop=values;preview.update()
        def drag(values):
            for spin,value in zip(spins,values):spin.blockSignals(True);spin.setValue(value*100);spin.blockSignals(False)
            changed()
        for spin in spins:spin.valueChanged.connect(changed)
        preview.crop_changed.connect(drag);buttons.button(QDialogButtonBox.Reset).clicked.connect(lambda:drag([0,0,1,1]));changed()
        result=[s.value()/100 for s in spins] if d.exec() else None;d.deleteLater();return result

    def crop_frame(self):
        if not self.project:return
        value=self.region_dialog('Crop frame',self.selected_clip().get('crop',[0,0,1,1]))
        if value is not None:self.checkpoint();self.selected_clip()['crop']=value;self.save_project();self.render_sections()

    def privacy_region(self):
        if not self.project:return
        value=self.region_dialog('Privacy blackout',[.2,.2,.3,.3],False)
        if value:
            x,y,w,h=value;self.add_stroke({'tool':'redact','color':'#000000','width':.004,'points':[[x,y],[x+w,y+h]]})

    def change_speed(self):
        if self.loading or not self.project:return
        self.playback.pause();self.checkpoint();self.selected_clip()['speed']=self.speed.currentData();self.save_project();self.playback.seek(self.playback.time)

    def save_frame(self):
        if not self.project:return
        self.playback.pause();path,_=QFileDialog.getSaveFileName(self,'Save edited frame',str(self.path.parent/'frame.png'),'PNG image (*.png)')
        if path:
            if not path.lower().endswith('.png'):path+='.png'
            if Path(path).exists():self.show_error('Choose a new filename to preserve the existing image.');return
            if not self.preview.snapshot().save(path):self.show_error('The frame could not be saved. Choose a writable folder.')

    def join_clip(self):
        if not self.project:return
        candidate=copy.deepcopy(self.project)
        try:join_next(candidate,self.section_index)
        except ValueError as e:self.show_error(str(e));return
        self.checkpoint();self.project['sections']=candidate['sections'];self.save_project();self.render_sections();self.render_sources()

    def move_clip(self,delta):
        if not self.project:return
        i=self.section_index;j=i+delta;sections=self.project['sections']
        if not 0<=j<len(sections):return
        self.playback.pause();self.checkpoint();sections[i],sections[j]=sections[j],sections[i];self.section_index=j
        self.save_project();self.render_sections();self.render_sources();self.playback.seek(sections[j]['start'])

    def choose_import(self,overlay=False):
        if not self.project:return
        path,_=QFileDialog.getOpenFileName(self,'Overlay recording' if overlay else 'Append recording',str(self.path.parent),'Recordings (*.katman *.mp4 *.mkv *.webm *.mov)')
        if path:self.import_path(path,overlay)

    def import_path(self,path,overlay=False):
        self.playback.pause();self.set_editor_enabled(False);self.archive.setEnabled(False);self.statusBar().showMessage('Importing recording…')
        original=copy.deepcopy(self.project);project_path=self.path;index=self.section_index
        def finish(result=None,error=None):
            self.set_editor_enabled(True);self.archive.setEnabled(True);self.statusBar().clearMessage()
            if error:self.show_error(error);return
            self.checkpoint();self.project.clear();self.project.update(result);self.section_index=index if overlay else len(result['sections'])-1
            self.preview.frames.clear();self.playback.load(self.path.parent,self.project);self.seek_slider.setRange(0,round(result['duration']*1000))
            self.save_project();self.render_sections();self.render_sources();self.playback.seek(self.selected_clip()['start'])
            from .presentation import derivatives
            self.start_task(lambda task:derivatives(self.path.parent,copy.deepcopy(result)),lambda assets:(setattr(self.timeline,'waves',assets['waves']),self.timeline.update()),lambda _:None)
        self.start_task(lambda task:import_recording(project_path,original,path,overlay,index),lambda result:finish(result),lambda error:finish(error=error))
