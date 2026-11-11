# SPDX-License-Identifier: GPL-3.0-only
"""Headless editor worker contract tests with synthetic CPU media only."""
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from katman import editor_worker as worker
from katman import studio_jobs, studio_render
from katman.common import atomic_json, probe, run
from katman.studio_model import duration


def model_fixture():
    return {
        "version": 1,
        "width": 64,
        "height": 64,
        "fps": 10,
        "tracks": [
            {
                "id": "video-track",
                "source_id": "video",
                "name": "Video",
                "kind": "video",
                "mute": False,
                "visible": True,
                "clips": [{
                    "id": "video-clip",
                    "source_id": "video",
                    "name": "Clip",
                    "start": 0,
                    "in": 0,
                    "length": 2,
                    "duration": 2,
                    "speed": 1,
                    "gain": 1,
                    "keys": {},
                    "x": 0,
                    "y": 0,
                    "w": 64,
                    "h": 64,
                    "opacity": 1,
                    "rotation": 0,
                }],
            },
            {
                "id": "audio-track",
                "source_id": "audio",
                "name": "Audio",
                "kind": "audio",
                "mute": False,
                "visible": True,
                "clips": [{
                    "id": "audio-clip",
                    "source_id": "audio",
                    "name": "Tone",
                    "start": 0,
                    "in": 0,
                    "length": 2,
                    "duration": 2,
                    "speed": 1,
                    "gain": 1,
                    "keys": {},
                }],
            },
        ],
        "annotations": [],
        "subtitles": [],
        "subtitle_style": {"font": "Noto Sans", "size": 8, "color": "#ffffff"},
        "snap": True,
    }


class EditorWorker(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        for module in (worker, studio_jobs, studio_render):
            patcher = patch.object(module, "CACHE", self.cache)
            patcher.start()
            self.addCleanup(patcher.stop)
        run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x64:r=10:d=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", self.media / "clip.mkv",
        ])
        run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=48000",
            "-c:a", "flac", self.media / "tone.mkv",
        ])
        run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=64x64:r=10:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", self.root / "extra.mp4",
        ])
        self.project = {
            "format": "katman",
            "version": 1,
            "duration": 2,
            "custom": {"preserve": True},
            "sources": [
                {
                    "id": "video",
                    "name": "Clip",
                    "type": "video",
                    "file": "media/clip.mkv",
                    "width": 64,
                    "height": 64,
                    "logical_width": 64,
                    "logical_height": 64,
                    "x": 0,
                    "y": 0,
                    "media_duration": 2,
                    "timeline_start": 0,
                    "trim_in": 0,
                    "extra_source_field": {"keep": "yes"},
                },
                {
                    "id": "audio",
                    "name": "Tone",
                    "type": "audio",
                    "file": "media/tone.mkv",
                    "media_duration": 2,
                    "timeline_start": 0,
                    "trim_in": 0,
                },
            ],
        }
        self.model = model_fixture()
        self.project["studio"] = self.model
        self.path = self.root / "project.katman"
        atomic_json(self.path, self.project)
        self.hashes = {p.name: hashlib.sha256(p.read_bytes()).digest() for p in self.media.iterdir()}

    def tearDown(self):
        for name, digest in self.hashes.items():
            self.assertEqual(hashlib.sha256((self.media / name).read_bytes()).digest(), digest, name)
        self.temp.cleanup()

    def assert_ok(self, result):
        self.assertTrue(result.get("ok"), result)
        return result["data"]

    def request(self, **payload):
        return worker.request({"path": str(self.path), **payload})

    def pixel(self, file, t):
        info = probe(file)
        width = next(s for s in info["streams"] if s["codec_type"] == "video")["width"]
        data = subprocess.check_output([
            "ffmpeg", "-v", "error", "-ss", str(t), "-i", str(file),
            "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        ])
        return tuple(data[(32 * width + 32) * 3:(32 * width + 32) * 3 + 3])

    def test_batch_export_separates_recordings_and_never_overwrites(self):
        import shutil
        other=self.root/'second';other.mkdir();shutil.copytree(self.media,other/'media');atomic_json(other/'project.katman',self.project)
        output=self.root/'batch'
        payload={'operation':'batch_export','paths':[str(self.path),str(other/'project.katman')],'output_directory':str(output),'options':{'fps':10,'size':[64,64]}}
        result=worker.request(payload);self.assertTrue(result['ok'],result)
        files=[Path(value) for value in result['data']['files']];self.assertEqual(len(files),2);self.assertNotEqual(files[0].name,files[1].name)
        originals={file:hashlib.sha256(file.read_bytes()).hexdigest() for file in files}
        again=worker.request(payload);self.assertTrue(again['ok'],again);self.assertFalse(again['data']['files']);self.assertEqual(len(again['data']['skipped']),2)
        self.assertEqual(originals,{file:hashlib.sha256(file.read_bytes()).hexdigest() for file in files})

    def test_waveforms_measure_real_audio_and_reuse_cache(self):
        result=self.request(operation='waveforms');self.assertTrue(result['ok'],result)
        data=result['data']['items'];self.assertEqual(len(data),1);self.assertGreater(max(data[0]['peaks']),.1);self.assertLessEqual(len(data[0]['peaks']),1024)
        with patch('katman.studio_render.run_process',side_effect=AssertionError('Cached waveform unexpectedly decoded audio again')):
            again=self.request(operation='waveforms')
        self.assertEqual(again,result)

    def test_frame_is_64px_deterministic_and_cached(self):
        result = self.assert_ok(self.request(operation="frame", model=self.model, position=.5, width=64))
        file = Path(result["file"])
        self.assertTrue(file.is_file())
        self.assertEqual(file.suffix, ".png")
        self.assertEqual(file.parent, (self.cache / "studio-native-frames").resolve())
        info = probe(file)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (64, 64))
        again = self.assert_ok(self.request(operation="frame", model=self.model, position=.5, width=64))
        self.assertEqual(result["file"], again["file"])
        other = self.assert_ok(self.request(operation="frame", model=self.model, position=1, width=64))
        self.assertNotEqual(result["file"], other["file"])
        self.assertTrue(Path(other["file"]).is_file())

    def test_frame_uses_saved_studio_when_model_is_omitted(self):
        result = self.assert_ok(self.request(operation="frame", position=0))
        self.assertTrue(Path(result["file"]).is_file())

    def test_preview_uses_the_existing_cached_renderer(self):
        result = self.assert_ok(self.request(operation="preview", model=self.model, width=64))
        file = Path(result["file"])
        self.assertTrue(file.is_file())
        self.assertEqual(file.suffix, ".mp4")
        info = probe(file)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (64, 64))
        self.assertAlmostEqual(float(info["format"]["duration"]), 2, delta=.15)
        again = self.assert_ok(self.request(operation="preview", model=self.model, width=64))
        self.assertEqual(result["file"], again["file"])

    def test_export_and_trim_render_with_cpu_ffmpeg(self):
        output = self.root / "export.mp4"
        result = self.assert_ok(self.request(
            operation="export",
            model=self.model,
            output=str(output),
            options={"size": [64, 64], "fps": 10, "quality": 30, "preset": "ultrafast"},
        ))
        self.assertEqual(Path(result["file"]), output.resolve())
        info = probe(output)
        self.assertAlmostEqual(float(info["format"]["duration"]), 2, delta=.15)
        self.assertTrue(any(s["codec_type"] == "audio" for s in info["streams"]))

        trimmed = self.media / "two-colors.mkv"
        run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x64:r=10:d=1",
            "-f", "lavfi", "-i", "color=blue:s=64x64:r=10:d=1",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p",
            "-c:v", "libx264", trimmed,
        ])
        project = copy.deepcopy(self.project)
        project["sources"].append({
            "id": "two", "name": "Two", "type": "video", "file": "media/two-colors.mkv",
            "width": 64, "height": 64, "media_duration": 2, "timeline_start": 0, "trim_in": 0,
        })
        atomic_json(self.path, project)
        trimmed_model = model_fixture()
        trimmed_model["tracks"] = [{
            "id": "two-track", "source_id": "two", "name": "Two", "kind": "video",
            "mute": False, "visible": True,
            "clips": [{
                "id": "two-clip", "source_id": "two", "name": "Two", "start": 0, "in": 1,
                "length": 1, "duration": 1, "speed": 1, "gain": 1, "keys": {},
                "x": 0, "y": 0, "w": 64, "h": 64, "opacity": 1, "rotation": 0,
            }],
        }]
        output = self.root / "trim.mp4"
        self.assert_ok(self.request(
            operation="export", model=trimmed_model, output=str(output),
            options={"size": [64, 64], "fps": 10, "quality": 30, "preset": "ultrafast"},
        ))
        self.assertAlmostEqual(float(probe(output)["format"]["duration"]), 1, delta=.15)
        self.assertGreater(self.pixel(output, .3)[2], 160)

    def test_export_never_overwrites_original_media(self):
        result = self.request(
            operation="export", model=self.model,
            output=str(self.media / "clip.mkv"), options={"size": [64, 64]},
        )
        self.assertFalse(result["ok"])
        self.assertIn("existing", result["error"].lower())

    def test_import_appends_or_overlays_and_preserves_fields(self):
        original_source = copy.deepcopy(self.project["sources"][0])
        base = duration(self.model)
        result = self.assert_ok(worker.request({
            "operation": "import",
            "path": str(self.path),
            "project": copy.deepcopy(self.project),
            "model": copy.deepcopy(self.model),
            "source": str(self.root / "extra.mp4"),
        }))
        self.assertTrue(result["project"]["custom"]["preserve"])
        self.assertEqual(result["project"]["sources"][0], original_source)
        self.assertEqual(len(result["project"]["sources"]), 3)
        self.assertEqual(len(result["project"]["sources"]), len(result["model"]["tracks"]))
        imported = result["project"]["sources"][-1]
        self.assertEqual(imported["type"], "video")
        self.assertEqual((imported["width"], imported["height"]), (64, 64))
        self.assertAlmostEqual(imported["media_duration"], 1, delta=.15)
        self.assertTrue((self.root / imported["file"]).is_file())
        clip = result["model"]["tracks"][-1]["clips"][0]
        self.assertAlmostEqual(clip["start"], base)
        self.assertAlmostEqual(duration(result["model"]), base + 1, delta=.15)

        overlay = self.assert_ok(worker.request({
            "operation": "import",
            "path": str(self.path),
            "project": copy.deepcopy(self.project),
            "model": copy.deepcopy(self.model),
            "source": str(self.root / "extra.mp4"),
            "overlay": True,
            "position": .5,
        }))
        self.assertAlmostEqual(overlay["model"]["tracks"][-1]["clips"][0]["start"], .5)

    def test_analysis_operations_are_delegated_with_cancel_and_progress(self):
        seen = []

        def proxies(path, cancel=None, progress=None):
            seen.append(cancel)
            if progress: progress(.4)
            return 2

        def stabilized(path, clip, cancel=None, progress=None):
            seen.append(cancel)
            if progress: progress(.4)
            return str(self.root / "motion.trf")

        def tracked(path, model, annotation, cancel=None, progress=None):
            seen.append(cancel)
            if progress: progress(.4)
            return {"x": [[0, 1]], "y": [[0, 2]], "w": [[0, 3]], "h": [[0, 4]]}

        def transcribed(path, model, cancel=None, progress=None, language="auto"):
            seen.append(cancel)
            self.assertEqual(language, "en")
            if progress: progress(.4)
            return [{"id": "s1", "start": 0, "end": 1, "text": "Hello"}]

        with patch.multiple(
            worker,
            build_proxies=proxies,
            stabilize=stabilized,
            track_region=tracked,
            transcribe=transcribed,
        ):
            events = []
            self.assertEqual(self.assert_ok(self.request(operation="proxies"))["count"], 2)
            self.assert_ok(self.request(
                operation="stabilize", clip=copy.deepcopy(self.model["tracks"][0]["clips"][0]),
            ))
            self.assert_ok(self.request(
                operation="track", model=self.model,
                annotation={"id": "a", "kind": "redact", "start": 0, "end": 1, "x": 0, "y": 0, "w": 64, "h": 64},
            ))
            captions = self.assert_ok(self.request(
                operation="transcribe", model=self.model, language="en",
            ))["captions"]
            self.assertEqual(captions[0]["text"], "Hello")
            worker.request(
                {"operation": "transcribe", "path": str(self.path), "model": self.model, "language": "en"},
                progress=events.append,
            )
        self.assertTrue(events)
        self.assertTrue(all(isinstance(event, threading.Event) for event in seen))
        self.assertFalse(seen[0].is_set())

    def test_subtitle_import_from_text_and_file_and_export(self):
        rows = self.assert_ok(self.request(
            operation="import_srt",
            text="1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        ))["subtitles"]
        self.assertEqual(rows[0]["text"], "Hello")
        source = self.root / "subs.srt"
        source.write_text("1\n00:00:01,000 --> 00:00:02,500\nFrom file\n", encoding="utf-8")
        rows = self.assert_ok(self.request(
            operation="import_srt", file="subs.srt",
        ))["subtitles"]
        self.assertEqual(rows[0]["text"], "From file")
        model = copy.deepcopy(self.model)
        model["subtitles"] = rows
        output = self.root / "captions.srt"
        result = self.assert_ok(self.request(operation="export_srt", model=model, output=str(output)))
        self.assertEqual(Path(result["file"]), output)
        self.assertIn("From file", output.read_text())
        output.write_text("keep", encoding="utf8")
        result = self.request(operation="export_srt", model=model, output=str(output))
        self.assertFalse(result["ok"])
        self.assertEqual(output.read_text(), "keep")
        self.assertFalse(self.request(operation="import_srt", text="not subtitles")["ok"])

    def test_errors_are_truthful_and_voiceover_is_explicitly_unsupported(self):
        self.assertIn("path", worker.request({"operation": "frame"})["error"])
        bad = copy.deepcopy(self.model)
        bad["width"] = 0
        result = self.request(operation="frame", model=bad)
        self.assertFalse(result["ok"])
        self.assertIn("canvas", result["error"])
        self.assertFalse(self.request(operation="frame", model=self.model, position=-1)["ok"])
        self.assertFalse(self.request(operation="missing")["ok"])
        result = self.request(operation="voiceover")
        self.assertFalse(result["ok"])
        self.assertIn("not supported", result["error"])
        result = self.request(operation="import", model=self.model, project=self.project, source=str(self.root / "missing.mp4"))
        self.assertFalse(result["ok"])
        self.assertIn("missing", result["error"])
        cancel = threading.Event()
        cancel.set()
        result = worker.request({"operation": "frame", "path": str(self.path), "model": self.model, "position": 0}, cancel=cancel)
        self.assertFalse(result["ok"])
        self.assertIn("cancelled", result["error"])

    def test_main_writes_progress_and_final_json_lines_only(self):
        def fake(path, model, cancel=None, progress=None, language="auto"):
            progress(.5)
            return [{"id": "s1", "start": 0, "end": 1, "text": "Hi"}]

        payload = {"operation": "transcribe", "path": str(self.path), "model": self.model, "language": "en"}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload) + "\n")), \
                patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr), \
                patch.object(worker, "transcribe", fake):
            code = worker.main()
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(lines[0], {"event": "progress", "value": .5})
        self.assertTrue(lines[-1]["ok"])
        self.assertEqual(lines[-1]["data"]["captions"][0]["text"], "Hi")

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps({"operation": "missing"}) + "\n")), \
                patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            code = worker.main()
        result = json.loads(stdout.getvalue().strip())
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertIn("Unknown", stderr.getvalue())

    def test_worker_process_has_json_stdout_and_no_pyside6_import(self):
        environment = os.environ.copy()
        environment.update(
            XDG_CACHE_HOME=str(self.root / "xdg-cache"),
            XDG_CONFIG_HOME=str(self.root / "xdg-config"),
            XDG_RUNTIME_DIR=str(self.root / "xdg-runtime"),
        )
        root = Path(worker.__file__).resolve().parent.parent
        code = (
            "import sys, json;"
            "from katman import editor_worker as w;"
            "r = w.request({'operation': 'import_srt', 'text': '1\\n00:00:00,000 --> 00:00:01,000\\nHi\\n'});"
            "print(json.dumps(r));"
            "print('PySide6' in sys.modules)"
        )
        process = subprocess.run(
            [sys.executable, "-c", code], cwd=root, env=environment,
            capture_output=True, text=True, timeout=60,
        )
        lines = process.stdout.splitlines()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(json.loads(lines[0])["ok"])
        self.assertEqual(lines[1], "False")

        payload = json.dumps({"operation": "import_srt", "text": "1\n00:00:00,000 --> 00:00:01,000\nHi\n"}) + "\n"
        process = subprocess.run(
            [sys.executable, "-m", "katman.editor_worker"], cwd=root, env=environment,
            input=payload, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(process.stdout.splitlines()), 1)
        self.assertTrue(json.loads(process.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
