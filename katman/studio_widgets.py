# SPDX-License-Identifier: GPL-3.0-only
"""Native multi-track timeline and direct manipulation canvas."""
import copy
import math
from PySide6.QtCore import Qt,QPointF,QRectF,Signal
from PySide6.QtGui import QPainter,QPen,QColor,QFont
from PySide6.QtWidgets import QWidget,QMenu
from .studio_model import duration,clip_duration,find_clip,source_offset,value_at,trim_clip
from .editing import draw_annotations,stroke_hit


def timeline_colors(source,background,muted=False):
    """Opaque composited source color with WCAG-paired label ink."""
    source,background=QColor(source),QColor(background);alpha=(95 if muted else 170)/255
    rgb=[round(source.getRgb()[i]*alpha+background.getRgb()[i]*(1-alpha)) for i in range(3)]
    linear=[v/255/12.92 if v/255<=.04045 else ((v/255+.055)/1.055)**2.4 for v in rgb]
    luminance=sum(a*b for a,b in zip(linear,(.2126,.7152,.0722)))
    return QColor(*rgb),QColor('#000000' if (luminance+.05)/.05>=1.05/(luminance+.05) else '#ffffff')


def selection_frame(p,rect,accent):
    """Media can contain any pixel: keep a black/light outline pair visible."""
    p.setBrush(Qt.NoBrush);p.setPen(QPen(QColor('#000000'),3));p.drawRect(rect)
    p.setPen(QPen(QColor('#ffffff'),1,Qt.DashLine));p.drawRect(rect)
    handle=QRectF(rect.right()-5,rect.bottom()-5,10,10)
    p.fillRect(handle,QColor('#000000'));p.fillRect(handle.adjusted(1,1,-1,-1),QColor('#ffffff'));p.fillRect(handle.adjusted(3,3,-3,-3),QColor(accent))


class TrackTimeline(QWidget):
    def __init__(self,studio):
        super().__init__();self.studio=studio;self.zoom=30.;self.drag=None;self.setMouseTracking(True);self.setFocusPolicy(Qt.StrongFocus);self.setAccessibleName('Multi-track timeline. Control-click selects multiple clips. Drag clip edges to trim; drag the body to move.')
        self.row=46;self.label_width=170;self.refresh()
    def refresh(self):
        self.setMinimumSize(max(700,round(self.label_width+(duration(self.studio.model)+2)*self.zoom)),34+self.row*len(self.studio.model['tracks']));self.update()
    def rect_for(self,row,clip):return QRectF(self.label_width+clip['start']*self.zoom,34+row*self.row+4,max(3,clip_duration(clip)*self.zoom),self.row-8)
    def paintEvent(self,event):
        s=self.studio;t=s.theme.tokens;p=QPainter(self);p.fillRect(self.rect(),QColor(t['background']));p.setRenderHint(QPainter.Antialiasing)
        font=QFont(self.font());font.setPixelSize(11);p.setFont(font);step=1 if self.zoom>=45 else 5 if self.zoom>=12 else 15
        for sec in range(0,math.ceil(duration(s.model))+3,step):
            x=self.label_width+sec*self.zoom;p.setPen(QColor(t['divider']));p.drawLine(round(x),24,round(x),self.height());p.setPen(QColor(t['muted']));p.drawText(QRectF(x+3,0,70,24),Qt.AlignVCenter,s.timecode(sec))
        for row,track in enumerate(s.model['tracks']):
            top=34+row*self.row;p.fillRect(QRectF(0,top,self.label_width-4,self.row),QColor(t['panel']));p.setPen(QColor(t['text']));font.setPixelSize(11);p.setFont(font)
            prefix='Muted · ' if track.get('mute') or not track.get('visible',True) else ''
            p.drawText(QRectF(9,top+3,self.label_width-20,self.row-5),Qt.AlignVCenter,p.fontMetrics().elidedText(prefix+track['name'],Qt.ElideRight,self.label_width-20))
            color=s.theme.source_color(row)
            for clip in track['clips']:
                rect=self.rect_for(row,clip);selected=clip['id'] in s.selection;fill,ink=timeline_colors(color,t['background'],track.get('mute') or not track.get('visible',True))
                p.setPen(QPen(QColor(t['primary']) if selected else QColor(t['divider']),2 if selected else 1));p.setBrush(fill);p.drawRoundedRect(rect,4,4)
                if track['kind']=='audio':
                    peaks=s.waves.get(clip['source_id'],[]);source=s.sources.get(clip['source_id'],{});media=max(.001,source.get('media_duration',1));p.setPen(QPen(ink,1))
                    for x in range(2,max(3,round(rect.width())-2),4):
                        local=source_offset(clip,x/max(.01,self.zoom));index=int(local/media*len(peaks));height=min(12,peaks[index]*15) if 0<=index<len(peaks) else 0
                        p.drawLine(QPointF(rect.x()+x,rect.center().y()-height),QPointF(rect.x()+x,rect.center().y()+height))
                p.setPen(ink);p.drawText(rect.adjusted(7,1,-7,-1),Qt.AlignTop|Qt.AlignLeft,p.fontMetrics().elidedText(clip['name'],Qt.ElideRight,max(1,round(rect.width()-14))))
                if selected:
                    p.setPen(QPen(QColor(t['primary']),2));p.drawLine(QPointF(rect.left()+3,rect.top()+7),QPointF(rect.left()+3,rect.bottom()-7));p.drawLine(QPointF(rect.right()-3,rect.top()+7),QPointF(rect.right()-3,rect.bottom()-7))
        x=self.label_width+s.position*self.zoom;p.setPen(QPen(QColor(t['primary']),1.5));p.drawLine(round(x),22,round(x),self.height())
    def mousePressEvent(self,event):
        s=self.studio;pos=event.position();time=max(0,(pos.x()-self.label_width)/self.zoom)
        if event.button()==Qt.RightButton:
            menu=QMenu(self);menu.addAction('Split selected clips',s.split_selected);menu.addAction('Duplicate selected clips',s.duplicate);menu.addAction('Delete selected clips',s.delete_selected);menu.exec(event.globalPosition().toPoint());return
        if event.button()!=Qt.LeftButton:return
        if pos.y()<32:s.seek(time);self.drag=('seek',);return
        row=int((pos.y()-34)//self.row)
        if not 0<=row<len(s.model['tracks']):return
        track=s.model['tracks'][row]
        for clip in reversed(track['clips']):
            rect=self.rect_for(row,clip)
            if rect.contains(pos):
                s.primary=clip['id']
                if event.modifiers()&Qt.ControlModifier:
                    s.selection.symmetric_difference_update({clip['id']});s.fill_inspector();self.update();return
                if clip['id'] not in s.selection:s.selection={clip['id']}
                s.checkpoint();s.pause();side='left' if abs(pos.x()-rect.left())<7 else 'right' if abs(pos.x()-rect.right())<7 else 'move'
                originals={c['id']:copy.deepcopy(c) for tr in s.model['tracks'] for c in tr['clips'] if c['id'] in s.selection}
                self.drag=(side,pos,originals,row,clip['id']);s.fill_inspector();self.update();return
        s.selection=set();s.seek(time);s.fill_inspector();self.update()
    def mouseMoveEvent(self,event):
        if not self.drag:return
        s=self.studio
        if self.drag[0]=='seek':s.seek(max(0,(event.position().x()-self.label_width)/self.zoom));return
        side,point,originals,row,primary=self.drag;delta=(event.position().x()-point.x())/self.zoom
        if s.snap.isChecked():
            target=originals[primary]['start']+delta
            candidates=[0]+[edge for tr in s.model['tracks'] for c in tr['clips'] if c['id'] not in s.selection for edge in (c['start'],c['start']+clip_duration(c))]
            nearest=min(candidates,key=lambda x:abs(x-target))
            if abs(nearest-target)*self.zoom<8:delta=nearest-originals[primary]['start']
            else:delta=round(delta*s.model['fps'])/s.model['fps']
        try:
            for id,original in originals.items():
                tr,c=find_clip(s.model,id);c.clear();c.update(copy.deepcopy(original))
                if side=='move':c['start']=max(0,c['start']+delta)
                elif id==primary:trim_clip(c,side,delta)
        except ValueError as e:s.statusBar().showMessage(str(e),2500)
        self.refresh()
    def mouseReleaseEvent(self,event):
        if not self.drag:return
        state=self.drag;self.drag=None
        if state[0]!='seek':
            side,point,originals,row,id=state;s=self.studio
            if side=='move' and len(originals)==1:
                newrow=int((event.position().y()-34)//self.row);old,c=find_clip(s.model,id)
                if 0<=newrow<len(s.model['tracks']):
                    target=s.model['tracks'][newrow]
                    if target is not old and target['kind']==old['kind']:old['clips'].remove(c);target['clips'].append(c)
            s.changed()


class StudioCanvas(QWidget):
    def __init__(self,studio):
        super().__init__();self.studio=studio;self.image=None;self.content=QRectF();self.gesture=None;self.setMinimumSize(240,150);self.setFocusPolicy(Qt.StrongFocus);self.setAccessibleName('Edited video canvas. Select a drawing or privacy tool to draw; use Move layer to reposition video.')
    def paintEvent(self,event):
        s=self.studio;p=QPainter(self);p.fillRect(self.rect(),QColor('#090b0c'));w,h=s.model['width'],s.model['height'];scale=min((self.width()-20)/w,(self.height()-20)/h);self.content=QRectF((self.width()-w*scale)/2,(self.height()-h*scale)/2,w*scale,h*scale)
        if self.image is not None:p.drawImage(self.content,self.image)
        else:p.setPen(QColor('#cbd0d5'));p.drawText(self.rect(),Qt.AlignCenter,'Preparing preview…')
        if s.selection:
            try:
                clip=s.current_clip();local=s.position-clip['start']
                if 'w' in clip and 0<=local<=clip_duration(clip):
                    rect=QRectF(self.content.x()+value_at(clip,'x',local)*scale,self.content.y()+value_at(clip,'y',local)*scale,value_at(clip,'w',local)*scale,value_at(clip,'h',local)*scale)
                    selection_frame(p,rect,s.theme.tokens['primary'])
            except ValueError:pass
        if s.annotation_selection:
            a=next((a for a in s.model['annotations'] if a['id']==s.annotation_selection),None)
            if a:
                local=s.position-a['start'];rect=QRectF(self.content.x()+value_at(a,'x',local)*scale,self.content.y()+value_at(a,'y',local)*scale,value_at(a,'w',local,100)*scale,value_at(a,'h',local,100)*scale)
                selection_frame(p,rect,s.theme.tokens['primary'])
        if self.gesture and self.gesture[0]=='draw':
            tool,points=self.gesture[1],self.gesture[2]
            draw_annotations(p,self.content,[{'tool':'redact' if tool in ('redact','blur','pixelate') else 'rectangle' if tool=='crop_canvas' else tool,'points':points,'color':s.ink,'width':s.ink_width/1080}])
    def coordinates(self,point):return ((point.x()-self.content.x())/max(1,self.content.width())*self.studio.model['width'],(point.y()-self.content.y())/max(1,self.content.height())*self.studio.model['height'])
    def mousePressEvent(self,event):
        if event.button()!=Qt.LeftButton or not self.content.contains(event.position()):return
        s=self.studio;s.pause();x,y=self.coordinates(event.position());tool=s.canvas_mode.currentData();w,h=s.model['width'],s.model['height']
        if tool in ('pen','line','arrow','rectangle','redact','blur','pixelate','crop_canvas'):
            self.gesture=('draw',tool,[[x/w,y/h]])
        elif tool=='move':
            for track in reversed(s.model['tracks']):
                if track['kind']!='video':continue
                for clip in reversed(track['clips']):
                    local=s.position-clip['start']
                    if not 0<=local<=clip_duration(clip):continue
                    cx,cy,cw,ch=[value_at(clip,k,local,d) for k,d in [('x',0),('y',0),('w',w),('h',h)]]
                    if cx<=x<=cx+cw and cy<=y<=cy+ch:
                        s.checkpoint();s.primary=clip['id'];s.selection={clip['id']};s.annotation_selection=None;s.inspector.setCurrentIndex(1);resize=abs(x-cx-cw)*self.content.width()/w<12 and abs(y-cy-ch)*self.content.height()/h<12
                        self.gesture=('layer',clip['id'],x,y,cx,cy,cw,ch,resize);s.fill_inspector();return
        else:
            for a in reversed(s.model['annotations']):
                local=s.position-a['start'];ax,ay,aw,ah=[value_at(a,k,local,d) for k,d in [('x',0),('y',0),('w',100),('h',100)]]
                hit=ax<=x<=ax+aw and ay<=y<=ay+ah
                if hit and a['kind'] in ('pen','line','arrow','rectangle'):
                    hit=stroke_hit({**a,'tool':a['kind']},[(x-ax)/max(1,aw),(y-ay)/max(1,ah)],(aw*self.content.width()/w,ah*self.content.height()/h))
                if a['start']<=s.position<=a['end'] and hit:
                    if tool=='eraser':s.checkpoint();s.model['annotations'].remove(a);s.annotation_selection=None;s.changed();return
                    s.annotation_selection=a['id'];s.selection=set();s.inspector.setCurrentWidget(s.objects);s.checkpoint();resize=abs(x-ax-aw)*self.content.width()/w<12 and abs(y-ay-ah)*self.content.height()/h<12
                    self.gesture=('object',a['id'],x,y,ax,ay,aw,ah,resize);s.refresh_objects();self.update();return
    def mouseMoveEvent(self,event):
        if not self.gesture:return
        s=self.studio;x,y=self.coordinates(event.position());kind=self.gesture[0]
        if kind=='draw':
            points=self.gesture[2];point=[max(0,min(1,x/s.model['width'])),max(0,min(1,y/s.model['height']))]
            if self.gesture[1]=='pen':
                if len(points)<5000:points.append(point)
            else:points[:]=points[:1]+[point]
        else:
            _,id,px,py,ax,ay,aw,ah,resize=self.gesture
            obj=find_clip(s.model,id)[1] if kind=='layer' else next(a for a in s.model['annotations'] if a['id']==id)
            values={'w':max(2,aw+x-px),'h':max(2,ah+y-py)} if resize else {'x':ax+x-px,'y':ay+y-py}
            if kind=='object' and resize and obj.get('kind')=='text' and not obj.get('keys',{}).get('h'):obj['font_size']=obj.get('font_size',48)*values['h']/max(1,obj['h'])
            for key,value in values.items():
                if obj.get('keys',{}).get(key):s.put_key(obj,key,max(0,s.position-obj.get('start',0)),value)
                else:obj[key]=value
        self.update()
    def mouseReleaseEvent(self,event):
        if not self.gesture:return
        kind=self.gesture[0];s=self.studio
        if kind=='draw':
            _,tool,points=self.gesture
            if tool=='crop_canvas' and len(points)>1:
                a,b=points[0],points[-1];s.crop_canvas((min(a[0],b[0])*s.model['width'],min(a[1],b[1])*s.model['height'],abs(b[0]-a[0])*s.model['width'],abs(b[1]-a[1])*s.model['height']));self.gesture=None;return
            if len(points)>1 or tool=='pen':
                s.checkpoint();w,h=s.model['width'],s.model['height'];end=max(s.position+.1,duration(s.model));ann={'id':__import__('uuid').uuid4().hex[:12],'kind':tool,'start':s.position,'end':end,'color':s.ink,'width':s.ink_width/1080,'keys':{}}
                if tool in ('redact','blur','pixelate'):
                    a,b=points[0],points[-1];ann.update(x=min(a[0],b[0])*w,y=min(a[1],b[1])*h,w=max(2,abs(b[0]-a[0])*w),h=max(2,abs(b[1]-a[1])*h))
                else:ann.update(x=0,y=0,w=w,h=h,points=points)
                s.model['annotations'].append(ann);s.annotation_selection=ann['id'];s.selection=set();s.inspector.setCurrentWidget(s.objects)
        self.gesture=None;s.changed();self.update()
