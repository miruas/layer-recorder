# SPDX-License-Identifier: GPL-3.0-only
"""Native compositing canvas shared by the viewer and crop editor."""
import math
from PySide6.QtCore import Qt, Signal, QRectF, QPointF
from PySide6.QtGui import QImage, QPainter, QPen, QColor, QFont
from PySide6.QtWidgets import QWidget
from .editing import scene_layout, crop_geometry, draw_annotations, stroke_hit
from .presentation import icon


class Preview(QWidget):
    stroke_added=Signal(object)
    stroke_removed=Signal(int)
    source_moved=Signal(str,float,float)
    crop_changed=Signal(object)

    def __init__(self, playback, theme, parent=None):
        super().__init__(parent)
        self.playback,self.theme=playback,theme
        self.frames={};self.tool='view';self.ink='#ff4c4c';self.pen_width=4
        self.crop_on_full=True;self.show_crop=False;self.pending_crop=[0,0,1,1]
        self.stroke=None;self.drag=None;self.content=QRectF();self.source_rects={};self.scale=1
        self.setMinimumSize(240,150);self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName('Video canvas. Choose a drawing tool or move display; use Placement for numeric positioning.')
        playback.frame.connect(self.received);playback.position.connect(lambda _:self.update());theme.changed.connect(lambda _:self.update())

    def received(self,id,frame):self.frames[id]=frame;self.update()

    def geometry_for(self):
        project=self.playback.project
        if not project:return None
        sec=self.playback.section();videos=[s for s in project['sources'] if s['type']=='video' and s['id'] in sec['enabled_ids']]
        w,h,rects=scene_layout(videos,sec)
        return sec,videos,w,h,rects

    def paint_content(self,p,bounds,editor=True):
        state=self.geometry_for()
        if not state:return
        sec,videos,w,h,rects=state
        cx,cy,cw,ch=(0,0,w,h) if self.show_crop and self.crop_on_full else crop_geometry(w,h,sec)
        scale=min(bounds.width()/cw,bounds.height()/ch)
        left,top=bounds.center().x()-cw*scale/2,bounds.center().y()-ch*scale/2
        content=QRectF(left,top,cw*scale,ch*scale)
        if editor:self.content,self.scale=content,scale;self.source_rects={}
        p.save();p.setClipRect(content);p.fillRect(content,QColor('#000000'));p.setRenderHint(QPainter.SmoothPixmapTransform)
        order=sec.get('layer_order',list(rects))
        videos=sorted(videos,key=lambda s:order.index(s['id']) if s['id'] in order else len(order))
        for source in videos:
            x,y,sw,sh=rects[source['id']];rect=QRectF(left+(x-cx)*scale,top+(y-cy)*scale,sw*scale,sh*scale)
            if editor:self.source_rects[source['id']]=rect
            frame=self.frames.get(source['id'])
            if source['id'] in sec.get('blackout_ids',[]):p.fillRect(rect,QColor('#000000'))
            elif frame is not None and not frame.isNull() and self.playback.time>=source.get('timeline_start',0):p.drawImage(rect,frame)
            elif editor:
                p.fillRect(rect,QColor('#20262a'));p.setPen(QColor('#d4d9dc'));p.drawText(rect,Qt.AlignCenter,source['name']+'\nWaiting for video')
        if not self.show_crop:
            draw_annotations(p,content,sec.get('annotations',[]))
            if editor and self.stroke:draw_annotations(p,content,[self.stroke])
        p.restore()
        if editor and self.show_crop:
            x,y,sw,sh=self.pending_crop;selected=QRectF(left+x*content.width(),top+y*content.height(),sw*content.width(),sh*content.height())
            p.setPen(QPen(QColor(self.theme.tokens['primary']),2,Qt.DashLine));p.setBrush(Qt.NoBrush);p.drawRect(selected)
        if editor and self.drag and self.tool=='move':
            id,start,current=self.drag;rect=self.source_rects[id].translated(current-start)
            p.setPen(QPen(QColor(self.theme.tokens['primary']),2,Qt.DashLine));p.setBrush(Qt.NoBrush);p.drawRect(rect)

    def paintEvent(self,event):
        p=QPainter(self);p.fillRect(self.rect(),QColor('#090b0c'))
        if self.playback.project:self.paint_content(p,QRectF(self.rect()).adjusted(12,12,-12,-12));return
        icon('layers','#9cabb7',42).paint(p,QRectF(self.width()/2-21,self.height()/2-57,42,42).toRect())
        p.setPen(QColor('#edf1f5'));f=QFont(self.font());f.setPixelSize(21);f.setWeight(QFont.DemiBold);p.setFont(f)
        p.drawText(QRectF(20,self.height()/2,self.width()-40,32),Qt.AlignCenter,'Select a recording')
        f.setPixelSize(13);f.setWeight(QFont.Normal);p.setFont(f);p.setPen(QColor('#aab5bf'))
        p.drawText(QRectF(24,self.height()/2+35,self.width()-48,44),Qt.AlignCenter|Qt.TextWordWrap,'Open a project from your library, or capture something new.')

    def normalized(self,pos):
        return [max(0,min(1,(pos.x()-self.content.x())/max(1,self.content.width()))),max(0,min(1,(pos.y()-self.content.y())/max(1,self.content.height())))]

    def mousePressEvent(self,event):
        if event.button()!=Qt.LeftButton or not self.playback.project or not self.content.contains(event.position()):return
        self.playback.pause();self.setFocus();point=self.normalized(event.position())
        if self.tool=='move':
            id=next((id for id,rect in reversed(list(self.source_rects.items())) if rect.contains(event.position())),None)
            if id:self.drag=(id,event.position(),event.position())
        elif self.tool=='eraser':
            strokes=self.playback.section().get('annotations',[])
            for i in range(len(strokes)-1,-1,-1):
                if stroke_hit(strokes[i],point,(self.content.width(),self.content.height())):
                    self.stroke_removed.emit(i);break
        elif self.tool!='view':self.stroke={'tool':self.tool,'color':self.ink,'width':self.pen_width/1080,'points':[point]}
        self.update()

    def mouseMoveEvent(self,event):
        if self.drag:self.drag=(self.drag[0],self.drag[1],event.position());self.update()
        if self.stroke:
            point=self.normalized(event.position())
            if self.tool=='pen':
                if len(self.stroke['points'])<5000:self.stroke['points'].append(point)
            else:self.stroke['points']=self.stroke['points'][:1]+[point]
            if self.tool=='crop':
                a,b=self.stroke['points'][0],point;self.pending_crop=[min(a[0],b[0]),min(a[1],b[1]),abs(b[0]-a[0]),abs(b[1]-a[1])]
            self.update()

    def mouseReleaseEvent(self,event):
        if self.drag:
            id,start,current=self.drag;self.drag=None
            self.source_moved.emit(id,(current.x()-start.x())/max(.01,self.scale),(current.y()-start.y())/max(.01,self.scale))
        if self.stroke:
            stroke=self.stroke;self.stroke=None
            if self.tool=='crop':self.crop_changed.emit(self.pending_crop)
            elif len(stroke['points'])>1 or self.tool=='pen':self.stroke_added.emit(stroke)
        self.update()

    def snapshot(self):
        state=self.geometry_for()
        if not state:raise ValueError('Open a recording first.')
        w,h=crop_geometry(state[2],state[3],state[0])[2:]
        image=QImage(w,h,QImage.Format_RGB32);image.fill(QColor('#000000'))
        p=QPainter(image);self.paint_content(p,QRectF(0,0,w,h),False);p.end();return image
