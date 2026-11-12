# Editor preview performance

The editor uses separate paths for paused interaction, playback, analysis and final export. A faster preview does not change the original recording or the chosen export settings.

## Paused interaction

For supported scenes, the native editor decodes source frames in the background and keeps bounded GPU textures. Moving, resizing, cropping, flipping or rotating a cached layer changes its draw geometry; it does not launch a new compositor render for each pointer movement. Opacity, fades, borders, blackouts and supported drawing objects can also use this path. Seeking to an uncached source time still requires decoding.

Source-frame requests include the source path, source time and preview dimensions. The cache has a 64 MiB texture budget, supports up to 32 active layers, uses two background decode workers and discards requests/results superseded by a newer scene. A scene outside its limits uses the compositor fallback.

The fast path is intentionally selective. Text, active captions, blur, pixelation, color adjustments, LUTs, stabilization and ordinary reverse playback use the accurate FFmpeg compositor. Decode failure also triggers fallback. Effects and privacy processing are not silently removed to make the preview faster. Rendering at preview resolution can produce different edge sampling from a full-resolution export.

## Playback

Play launches `python3 -m katman.preview_stream` with the current model, a source snapshot, playback position and exact output size. The compositor sends packed RGBA frames directly to the native player. It does not encode an intermediate MP4 or render the entire timeline before displaying its first frame.

The native player starts its 24 fps clock when the first complete frame arrives. Playback continues through the remaining timeline; there is no implicit 30-second limit. The same compositor graph applies crop, transforms, annotations, privacy effects, captions and audio effects used for export.

When available, `paplay` receives stereo 48 kHz signed 16-bit PCM from a second output of the same FFmpeg process. Audio starts after the first video frame is sent. Missing audio playback support and compositor failures produce diagnostics in the editor. The playback owner cancels the whole process group, including FFmpeg and the audio player.

## Editing and persistence

Properties capture the selected editing scope for undo. Inspecting a clip does not copy the entire timeline; object, caption and canvas changes capture their relevant fields. One continuous pointer gesture creates one undo checkpoint.

Project writes run on a background save worker. Pending writes coalesce to the latest document snapshot, with a short 100 ms collection interval. Disk-dependent jobs wait for a save barrier. Frame previews and playback receive in-memory source snapshots, so they can reflect current edits without waiting for a saved project file. Snapshot sources retain the recording-folder path boundary. Save errors remain visible, and closing the editor waits for an asynchronous flush acknowledgement.

## Preview size and capture quality

Timeline playback and compositor frame previews fit within **960 × 640**, preserving canvas aspect ratio. Native source textures use the same per-source bounds. Playback is 24 fps. These limits reduce preview work; they do not set capture resolution, capture frame rate, recording bitrate or export quality.

Capture remains owned by GPU Screen Recorder. Capture resolution, frame rate, codec, quality preset or custom bitrate are independently configurable; see [Configuration](CONFIGURATION.md). Export uses its selected dimensions, frame rate, codec and quality.

## Reproducible measurement

From the repository root:

```sh
python3 tools/benchmark-preview.py --stream
python3 tools/benchmark-preview.py --stream --seconds 10
PYTHONPATH=tests:. python3 -m unittest discover -s tests -q
```

The benchmark creates temporary synthetic media: two solid-color 1920 × 1080 H.264 sources at 24 fps, a 440 Hz audio source, a cropped video layer, a black privacy rectangle and text. The combined canvas is 3840 × 1080; output is 960 × 270, or 1,036,800 bytes per RGBA frame. No recording library or personal media is used.

Measurements below are individual runs on Linux x86-64, AMD Ryzen 7 9800X3D, with 8 logical CPUs visible, Python 3.14.8 and FFmpeg 9.0.2. Rendering uses the software compositor and the benchmark's ultrafast H.264 preset. Other work on the machine was not controlled. These figures are examples, not a minimum performance guarantee.

| Timeline length | First frame render | Repeated frame render | Entire timeline MP4 encode | First streamed frame |
| --- | ---: | ---: | ---: | ---: |
| 2 seconds | 148.4 ms | 113.4 ms | 1,249.5 ms | 134.9 ms |
| 10 seconds | 161.6 ms | 120.3 ms | 6,173.5 ms | 145.5 ms |

Frame renders request position 0.8 seconds and produce separate PNGs. “Repeated” reuses process-level capability/font caches and warm filesystem state; it is not a hit in the native texture cache or PNG result cache. No operating-system cache flush is performed before the first render.

The MP4 measurement encodes the full timeline, including audio. Streaming begins at 0.8 seconds and measures arrival of one complete RGBA frame from a fresh Python worker; audio playback is disabled for that timing. Media generation is excluded. The benchmark drains remaining output without native display pacing. It does not measure display latency, audible synchronization, GPU interaction latency, analysis or final export speed.

The measurements demonstrate avoiding an entire-timeline encode before playback. They do not establish a universal speedup for all edits. Solid-color sources compress easily; real screen motion, codecs, storage and scene complexity can change the result substantially.

## Remaining costs

An uncached seek still starts a decoder. Complex scenes and unsupported effects use FFmpeg and can remain expensive. Reverse streaming can buffer a source segment before producing reversed frames. Tracking, stabilization analysis, denoising, transcription, proxy generation and final export retain their processing costs. A 24 fps preview cannot guarantee real-time playback for every combination of effects or hardware.
