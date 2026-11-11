# Licensing and upstream notices

Layer and its modified GPU Screen Recorder distribution are licensed **GPL-3.0-only**; see LICENSE. Original GPU Screen Recorder authorship and notices remain intact. Independently licensed third-party components retain their own notices and permissions. Patches to upstream components retain their upstream licensing. The Git tree publishes source and patch recipes. Release assets may provide compiled packages with matching corresponding-source archives. Model weights are never bundled.

| Component | Upstream | Pinned revision | License |
| --- | --- | --- | --- |
| GPU Screen Recorder core fork | dec05eba/gpu-screen-recorder | `1d4946a16139b446e6308f741e9ba99f2ed5b3a7` (6.1.7) | GPL-3.0-only |
| Optional GSR UI | dec05eba/gpu-screen-recorder-ui | `e3560df24b54c61cefe3f6dd34a3238350e31195` | GPLv3; upstream notices apply |
| Optional whisper.cpp | ggml-org/whisper.cpp | `727d3e7666485e12450746b523bfb4e7f057cc5e` | MIT |
| Optional Whisper base weights | OpenAI Whisper / whisper.cpp model distribution | SHA-256 pinned by download tool | MIT |

Corresponding upstream license texts are in `licenses/`. Public source URLs and revisions are in `packaging/native-lock.json`; helper modifications are in `patches/`. The builder fetches source/submodules at the selected revision and applies the published patch. Downstream binary distributors must satisfy the corresponding-source/license requirements for everything they distribute, including linked libraries.

The bundled GPU Screen Recorder source is in `upstream/gpu-screen-recorder`; its full public ancestry is included through the subtree import. The Flatpak build also packages FFmpeg, x264, x265, libaom, libass, vid.stab, libdrm, the legacy Intel VA-API driver, Pillow, NumPy and OpenCV. Each retains its original license and notices in the package. OpenCV is built without a separate FFmpeg decoder. The native interface uses eframe/egui and has no Qt dependency. Layer is not affiliated with GPU vendors or the upstream recording projects.

The Rust core and editor statically link the dependency versions recorded in `native/recorder/Cargo.lock` and `native/editor/Cargo.lock`. Binary packages include their original license texts under `licenses/layer-recorder/rust`; corresponding-source archives include vendored crate sources. Earlier wf-recorder helper integration was removed in 0.7.
