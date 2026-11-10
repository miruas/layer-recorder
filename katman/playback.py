# SPDX-License-Identifier: GPL-3.0-only
"""Synchronized independent-media playback; the UI only paints decoded frames."""
from PySide6.QtCore import QObject, QElapsedTimer, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

from .export import sections_for


class Playback(QObject):
    frame = Signal(str, object)
    position = Signal(float)
    playing_changed = Signal(bool)
    source_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.players = {}
        self.warming = set()
        self.project = None
        self.time = 0.0
        self.playing = False
        self.clock = QElapsedTimer()
        self.anchor = 0
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._tick)

    def load(self, folder, project):
        self.pause()
        for player, output, source in self.players.values():
            player.stop()
            player.setSource(QUrl())
            player.deleteLater()
            output.deleteLater()
        self.players.clear()
        self.warming.clear()
        self.project = project
        self.time = 0
        for source in project["sources"]:
            player = QMediaPlayer(self)
            if source["type"] == "video":
                output = QVideoSink(self)
                output.videoFrameChanged.connect(lambda f, id=source["id"]: self._frame(id, f))
                player.setVideoSink(output)
            else:
                output = QAudioOutput(self)
                player.setAudioOutput(output)
            player.errorOccurred.connect(lambda error, text, name=source["name"]: self.source_error.emit(f"{name}: {text}"))
            player.mediaStatusChanged.connect(lambda status, id=source["id"]: self._loaded(id, status))
            self.players[source["id"]] = player, output, source
            player.setSource(QUrl.fromLocalFile(str(folder / source["file"])))
        self.seek(sections_for(project)[0]["start"])

    def _loaded(self, id, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self.sync(force=True, only=id)
            if id in self.players:
                player, output, source = self.players[id]
                if source["type"] == "video" and not self.playing:
                    # Qt's FFmpeg backend needs an initial silent preroll before a
                    # paused source reliably produces its first decoded image.
                    self.warming.add(id)
                    player.play()

    def _frame(self, id, frame):
        if not frame.isValid():
            return
        self.frame.emit(id, frame.toImage())
        if id in self.warming:
            self.warming.discard(id)
            if not self.playing and id in self.players:
                self.players[id][0].pause()

    def section(self):
        if not self.project:
            return None
        sections = sections_for(self.project)
        return next((s for s in sections if s["start"] <= self.time < s["end"]), sections[-1])

    def sync(self, force=False, only=None):
        section = self.section()
        if not section:
            return
        for id, (player, output, source) in self.players.items():
            if only and id != only:
                continue
            player.setPlaybackRate(float(section.get("speed", 1)))
            local = self.time - source.get("timeline_start", 0) + source.get("trim_in", 0)
            valid = id in section["enabled_ids"] and local >= 0 and local < source.get("media_duration", 1e10)
            if source["type"] == "audio":
                output.setVolume(min(1, float(section.get("gains", {}).get(id, 1))) if valid else 0)
            target = round(max(0, local)*1000)
            if force or abs(player.position()-target) > 160:
                player.setPosition(target)
            if self.playing and valid:
                if player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                    player.play()
            else:
                if id not in self.warming:
                    player.pause()

    def seek(self, seconds):
        if not self.project:
            return
        self.time = max(0, min(float(seconds), self.project["duration"]))
        self.anchor = self.time
        self.clock.start()
        self.sync(force=True)
        self.position.emit(self.time)

    def play(self):
        if not self.project:
            return
        if self.section() is sections_for(self.project)[-1] and self.time >= self.section()["end"] - .04:
            self.seek(sections_for(self.project)[0]["start"])
        self.playing = True
        self.anchor = self.time
        self.clock.start()
        self.sync(force=True)
        self.timer.start()
        self.playing_changed.emit(True)

    def pause(self):
        self.playing = False
        self.timer.stop()
        for p, output, source in self.players.values():
            p.pause()
        self.playing_changed.emit(False)

    def toggle(self):
        self.pause() if self.playing else self.play()

    def _tick(self):
        target = self.anchor + self.clock.elapsed()/1000 * self.section().get("speed", 1)
        sections = sections_for(self.project)
        previous = self.section()
        if target >= previous["end"]:
            index = next((i for i,s in enumerate(sections) if s is previous), len(sections)-1)
            if index+1 < len(sections):
                self.seek(sections[index+1]["start"])
                return
            self.time = previous["end"]
            self.pause()
        else:
            self.time = target
        self.sync()
        self.position.emit(self.time)
