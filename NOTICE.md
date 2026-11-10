# Licensing and upstream notices

Layer application source is distributed under GPL-3.0-or-later; see LICENSE. Patches to upstream components retain their upstream licensing. This repository publishes source and patch recipes, not locally compiled binaries or model weights.

| Component | Upstream | Pinned revision | License |
| --- | --- | --- | --- |
| wf-recorder helper | ammen99/wf-recorder | `c5de47440e8e81c92befb696cb69819cdb3bfe8a` | MIT |
| Optional GSR UI | dec05eba/gpu-screen-recorder-ui | `e3560df24b54c61cefe3f6dd34a3238350e31195` | GPLv3; upstream notices apply |
| Optional whisper.cpp | ggml-org/whisper.cpp | `727d3e7666485e12450746b523bfb4e7f057cc5e` | MIT |
| Optional Whisper base weights | OpenAI Whisper / whisper.cpp model distribution | SHA-256 pinned by download tool | MIT |

Corresponding upstream license texts are in `licenses/`. Public source URLs and revisions are in `packaging/native-lock.json`; helper modifications are in `patches/`. The builder fetches source/submodules at the selected revision and applies the published patch. Downstream binary distributors must satisfy the corresponding-source/license requirements for everything they distribute, including linked libraries.

GPU Screen Recorder, FFmpeg, Qt/PySide6, NumPy and OpenCV are external dependencies with their own licenses. Install them through their supported distribution channels. Layer is not affiliated with GPU vendors or the upstream recording projects.
