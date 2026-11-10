import hashlib
import json
import math
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from katman.common import atomic_json, load_project, probe, run
from katman.export import export_project


class ExportIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "media").mkdir()
        for name, color, size in [("left", "red", "160x90"), ("right", "blue", "90x160")]:
            run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color={color}:s={size}:r=30:d=2",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", self.root / "media" / f"{name}.mkv"])
        for name, freq in [("game", 440), ("chat", 880)]:
            run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=2:sample_rate=48000",
                 "-c:a", "flac", self.root / "media" / f"{name}.mkv"])
        self.project = {"format": "katman", "version": 1, "duration": 2, "sources": [], "sections": []}
        for id, w,h,x in [("left",160,90,0),("right",90,160,160)]:
            self.project["sources"].append(dict(id=id,name=id,type="video",file=f"media/{id}.mkv",width=w,height=h,
                        logical_width=w,logical_height=h,x=x,y=0,media_duration=2,timeline_start=0,trim_in=0))
        for id in ["game", "chat"]:
            self.project["sources"].append(dict(id=id,name=id,type="audio",file=f"media/{id}.mkv",media_duration=2,
                                                timeline_start=0,trim_in=0))
        self.path = self.root / "project.katman"
        self.hashes = {p.name: hashlib.sha256(p.read_bytes()).digest() for p in (self.root / "media").glob("*")}

    def tearDown(self):
        for p in (self.root / "media").glob("*"):
            self.assertEqual(hashlib.sha256(p.read_bytes()).digest(), self.hashes[p.name])
        self.temp.cleanup()

    def export(self, name):
        atomic_json(self.path, self.project)
        dest = self.root / name
        export_project(self.path, dest)
        return dest, probe(dest)

    def test_desktop_geometry_and_audio_mix(self):
        file, info = self.export("desktop.mp4")
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (250,160))
        self.assertAlmostEqual(float(info["format"]["duration"]), 2, delta=.12)
        self.assertTrue(any(s["codec_type"] == "audio" for s in info["streams"]))

    def test_isolated_audio_and_trim(self):
        self.project["sections"] = [dict(start=.3,end=1.3,enabled_ids=["right","game"],gains={"game":.8})]
        file, info = self.export("isolated.mp4")
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"],video["height"]),(90,160))
        raw = subprocess.check_output(["ffmpeg","-v","error","-i",str(file),"-vn","-f","s16le","-ac","1","-ar","48000","-"])
        samples = struct.unpack('<'+'h'*(len(raw)//2),raw)
        def power(freq):
            a=sum(v*math.cos(2*math.pi*freq*i/48000) for i,v in enumerate(samples))
            b=sum(v*math.sin(2*math.pi*freq*i/48000) for i,v in enumerate(samples))
            return a*a+b*b
        self.assertGreater(power(440), power(880)*100)
        self.assertAlmostEqual(float(info["format"]["duration"]),1,delta=.12)

    def test_per_section_sources_and_silent_export(self):
        self.project["sections"] = [dict(start=0,end=.5,enabled_ids=["left"],gains={}),
                                    dict(start=1,end=1.5,enabled_ids=["right"],gains={})]
        file, info = self.export("sections.mp4")
        self.assertAlmostEqual(float(info["format"]["duration"]),1,delta=.13)

    def test_reject_original_overwrite_and_path_escape(self):
        atomic_json(self.path,self.project)
        with self.assertRaises(ValueError):
            export_project(self.path,self.root/'media'/'left.mkv')
        self.project['sources'][0]['file']='../outside.mkv'
        atomic_json(self.path,self.project)
        with self.assertRaises(ValueError):
            load_project(self.path)


if __name__ == '__main__':
    unittest.main()
