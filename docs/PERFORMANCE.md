# Editor preview performance

The editor uses separate paths for paused interaction, playback, analysis and final export. A faster preview does not change the original recording or the chosen export settings.

## Paused interaction

For supported scenes, the native editor decodes source frames in the background and keeps bounded GPU textures. Moving, resizing, cropping, flipping or rotating a cached layer changes its draw geometry; it does not launch a new compositor render for each pointer movement. Opacity, fades, borders, blackouts and supported drawing objects can also use this path. Seeking to an uncached source time still requires decoding.

Source-frame requests include the source path, source time and preview dimensions. The source cache has a 128 MiB texture budget, supports up to 32 active layers, uses two background decode workers and discards requests/results superseded by a newer scene. A scene outside its limits uses the compositor fallback.

Opaque redactions, canvas-anchored pixelation and two-iteration box blur run as GPU overlays in annotation order. Blur runs four separable passes over the privacy rectangle plus its required 24-pixel halo; it does not process every canvas pixel in every pass. Source layers compose into a persistent underlay. Moving a privacy region restores that underlay by a GPU copy, then updates the affected overlays, without decoding source frames or composing the source layers again. Borders are applied inside each source before its opacity and rotation.

Titles are isolated transparent sprites, rendered with the export font and black outline. Moving, tinting or fading a title reuses the sprite. Editing text, font or font size can require another sprite render; no source footage is decoded for that operation.

Supported captions use independent libass sprites rather than rerendering the moving picture. The sprite uses the same ASS generation, font-family resolution, size, RGB color, line wrapping and left/right/bottom margins as export, including supported per-caption style overrides. The rasterizer crops transparent margins and retains the crop's canvas offset, so cropping the sprite does not change caption placement. Captions draw after privacy annotations, in the same order as the export compositor. Caption text, timing or style edits invalidate the relevant caption sprite, not the cached source/base picture.

One cancellable worker serves title and caption sprites. Visible, currently required sprites take priority over optional caption prefetch for transitions in the next two seconds. Prefetch may be cancelled or skipped; it does not guarantee that a future caption will be ready. The shared overlay texture budget is 64 MiB, with at most 256 retained texture entries. A current request set contains at most 64 keys, with its title requests additionally limited to 32 MiB. A caption request is also bounded before the worker allocates its raster. Unsupported styles, requests beyond these limits and rasterization failures use the accurate compositor instead of silently dropping caption content.

Color adjustments, LUTs, stabilization and reverse frame selection retain the accurate compositor. For eligible scenes, their paused base picture is cached separately from overlays: moving a privacy region over a color-adjusted clip does not rerender that base. Changing the base effect or seeking still requires a new base picture. Unsupported caption styles and scenes retain the full compositor. Supported caption sprites remain independent of that cached base. GPU or sprite failures stop the stripped-overlay playback and prepare the accurate preview; an unprocessed underlay is never used as a replacement for a missing privacy overlay.

While a new frame or glyph sprite is loading, the editor retains the last fully processed frame and shows “Updating preview…”. The held frame includes its privacy overlays; an incomplete composition is never presented. This avoids a black flash without displaying the unprocessed source.

Four reusable RGBA8 framebuffer textures hold the underlay, composition and two scratch targets; at the maximum 3840 × 2160 raster these attachments use about 127 MiB. This is separate from source textures, the shared title/caption sprite budget, decoder buffers and the recording/replay buffer. No source is silently enlarged and no low-resolution proxy is used by default. Preview overlays use the same geometry, inclusive mask boundaries, annotation ordering and nearest-neighbor grid phase as export. RGB GPU filtering and FFmpeg's YUV conversion can differ slightly in rounding and edge sampling; they are not promised bit-identical.

## Playback

Play launches `python3 -m katman.preview_stream` with the current model, a source snapshot, playback position and exact output size. The compositor sends packed RGBA frames directly to the native player. It does not encode an intermediate MP4 or render the entire timeline before displaying its first frame.

The native player starts its playback clock when the first complete frame arrives. Preview cadence follows the source by default, up to 120 FPS; the preview controls also allow explicit 60 or 120 FPS scheduling. A higher selected cadence can repeat frames from a slower source and does not add captured motion detail. Playback continues through the remaining timeline; there is no implicit 30-second limit. Eligible playback decodes/composes its base without annotations, then applies the current GPU overlays at the displayed frame timestamp. Overlay-only edits keep that base decoder running. Other scenes retain the complete export compositor graph.

When available, `paplay` receives stereo 48 kHz signed 16-bit PCM from a second output of the same FFmpeg process. Audio starts after the first video frame is sent. Missing audio playback support and compositor failures produce diagnostics in the editor. The playback owner cancels the whole process group, including FFmpeg and the audio player. A single latest-frame mailbox replaces pending UI frames instead of building up a display backlog. Frame arrival requests a redraw; there is no fixed 15 ms polling cap. A measured playback counter is shown separately from the selected target cadence.

## Source frame-rate discovery

Older recordings may lack per-source frame-rate metadata. The editor opens the document first, then runs best-effort FFprobe discovery on a background worker. Each probe is limited to two seconds, with a three-second total budget per discovery call. Only regular video files whose canonical paths remain inside the recording folder are eligible; URLs, parent-path escapes, escaping symlinks and FIFOs are skipped. Repeated references to the same canonical file share a probe result for that call.

Discovery fills only missing `avg_frame_rate` and `r_frame_rate` source fields. Existing source FPS, project FPS and editor/export model FPS are preserved. Results are merged only when the document generation, source ID and file still match, so a result for a closed or changed recording cannot update the current document. Failed, unavailable or timed-out probes are nonfatal; existing frame-rate fallback remains in effect.

If a discovered rate changes Source-mode preview playback, that playback restarts at its current timeline position using the resolved cadence. Explicit 60 or 120 FPS preview choices remain explicit. Discovery never changes recording settings, retimes clips or rewrites the export frame rate.

## Editing and persistence

Properties capture the selected editing scope for undo. Inspecting a clip does not copy the entire timeline; object, caption and canvas changes capture their relevant fields. One continuous pointer gesture creates one undo checkpoint.

Project writes run on a background save worker. Pending writes coalesce to the latest document snapshot, with a short 100 ms collection interval. Disk-dependent jobs wait for a save barrier. Frame previews and playback receive in-memory source snapshots, so they can reflect current edits without waiting for a saved project file. Snapshot sources retain the recording-folder path boundary. Save errors remain visible, and closing the editor waits for an asynchronous flush acknowledgement.

## Preview size and capture quality

Preview quality defaults to Native with source frame rate, up to 120 FPS. Native preserves the combined canvas resolution within a 3840 × 2160 safety bound; for example, two side-by-side 1080p sources retain a 3840 × 1080 canvas. Explicit 1080p, 1440p, 4K and custom bounds preserve aspect ratio and avoid upscaling. The 1080p bound applies to the combined canvas, so that same dual-source canvas becomes 1920 × 540. Preferences persist in Layer settings; see [Configuration](CONFIGURATION.md#preview-quality-and-frame-rate).

Earlier versions used fixed 960 × 640 bounds and 24 FPS playback. Those are no longer the editor defaults. Higher resolution and frame rate increase decoding, transfer and drawing costs. The configured preview cadence is a target, not a guarantee that every scene will sustain it. These controls do not set capture resolution, capture frame rate, recording bitrate or export quality.

Capture remains owned by GPU Screen Recorder. Capture resolution, frame rate, codec, quality preset or custom bitrate are independently configurable; see [Configuration](CONFIGURATION.md). Export uses its selected dimensions, frame rate, codec and quality.

## Reproducible measurement

From the repository root:

```sh
python3 tools/benchmark-preview.py --stream
python3 tools/benchmark-preview.py --stream --seconds 10
PYTHONPATH=tests:. python3 -m unittest discover -s tests -q
```

Inside an existing X11/Wayland test desktop, verify GPU output and reuse:

```sh
python3 tools/test-gpu-preview.py bin/layer-studio --output build/gpu-preview --quick
```

This check generates its own sources and isolates configuration, cache and editor IPC. It compares GPU readbacks with FFmpeg references, checks fully opaque privacy boundaries, moves overlays through 60 real document updates, and verifies that no source decode/base render repeats. A color-adjusted scene verifies one cached compositor base; a one-pixel 1080p pattern verifies native detail survives. These are correctness and cache-reuse checks, not a claim of 120 FPS GPU throughput. The GUI check requires Pillow, FFmpeg and a running desktop or Xvfb. It does not start recording or access a recording library.

The benchmark creates temporary synthetic media: two solid-color 1920 × 1080 H.264 sources at 24 fps, a 440 Hz audio source, a cropped video layer, a black privacy rectangle and text. The combined canvas is 3840 × 1080; output is 960 × 270, or 1,036,800 bytes per RGBA frame. No recording library or personal media is used.

The measurements below describe the earlier 960 × 270, 24 FPS benchmark configuration, not the new Native/source defaults or GPU effect overlays. They are individual runs on Linux x86-64, AMD Ryzen 7 9800X3D, with 8 logical CPUs visible, Python 3.14.8 and FFmpeg 9.0.2. Rendering uses the software compositor and the benchmark's ultrafast H.264 preset. Other work on the machine was not controlled. These figures are examples, not a minimum performance guarantee.

| Timeline length | First frame render | Repeated frame render | Entire timeline MP4 encode | First streamed frame |
| --- | ---: | ---: | ---: | ---: |
| 2 seconds | 148.4 ms | 113.4 ms | 1,249.5 ms | 134.9 ms |
| 10 seconds | 161.6 ms | 120.3 ms | 6,173.5 ms | 145.5 ms |

Frame renders request position 0.8 seconds and produce separate PNGs. “Repeated” reuses process-level capability/font caches and warm filesystem state; it is not a hit in the native texture cache or PNG result cache. No operating-system cache flush is performed before the first render.

The MP4 measurement encodes the full timeline, including audio. Streaming begins at 0.8 seconds and measures arrival of one complete RGBA frame from a fresh Python worker; audio playback is disabled for that timing. Media generation is excluded. The benchmark drains remaining output without native display pacing. It does not measure display latency, audible synchronization, GPU interaction latency, analysis or final export speed.

The measurements demonstrate avoiding an entire-timeline encode before playback. They do not establish a universal speedup for all edits. Solid-color sources compress easily; real screen motion, codecs, storage and scene complexity can change the result substantially.

## Final export

Eligible complete, unchanged single-video clips can bypass video rendering and encoding through the Rust packet-copy worker. Selected audio is mixed separately. Precise cuts and edited/composited scenes retain the accurate renderer, which now avoids proven redundant/static filter work and exposes codec-specific speed controls. See [Quick export](FAST_EXPORT.md) for eligibility, timing limitations and a measured example.

## Remaining costs

An uncached seek still starts a decoder. Complex scenes and unsupported effects use FFmpeg and can remain expensive. Reverse streaming can buffer a source segment before producing reversed frames. Tracking, stabilization analysis, denoising, transcription, proxy generation and final export retain their processing costs. Neither 60 nor 120 FPS playback is guaranteed for every combination of effects or hardware. Measure the chosen resolution, source codec and effect stack on the target system; no fixed GPU speedup is claimed here.
