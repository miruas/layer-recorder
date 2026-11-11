# Recording and editor guide

## Capture and replay

Open Layer from the desktop launcher, or use:

```sh
layer record   # start/stop ordinary recording
layer replay   # enable/disable replay
layer save-1   # save the configured short replay duration
layer save-5   # save the configured long replay duration
layer status
```

The compatibility command names do not fix the durations. Defaults are one and five minutes; change the buffer and short/long saves through the bundled normal GSR settings. Layer's Recording preferences opens that settings page and separately lets you choose its recording library. Capture-format changes apply to a newly started capture; stop/save an ordinary recording before reloading capture configuration.

Replay and ordinary recording can run together. Saving does not stop replay or show a Layer success notification. Errors remain visible. Application folders use class/app identity, never browser-tab or conversation titles; native Wayland desktops without an identity provider use `Desktop`.

Displays and available audio sources are captured independently for selection afterward. PipeWire application/role streams are discovered dynamically. New saves omit application sources outside their saved time window and genuinely silent application tracks; microphones remain selectable. Voice/media separation requires the application to expose separate streams. PulseAudio fallback captures separate devices, with a warning that per-application selection is unavailable; restart capture to refresh that device inventory.

## One library and editor window

Choose a recording in the archive to edit it in the same native window. The library remains on the left; the composition and timeline are central, with Clip, Video, Audio, Objects, Captions and Canvas inspector tabs on the right. Opening another recording replaces the active document rather than launching a second editor.

Edits save automatically and preserve original media. Undo/Redo covers edit decisions. Keep `project.katman` and its `media/` folder together.

## Timeline and composition

- Drag clips to move them. Use the numeric Clip fields to trim source ranges and set precise timing/placement.
- Control-click selects multiple clips. Split, remove, duplicate, copy/paste, snapping and timeline zoom are available. Drag the timeline ruler to seek.
- Source checkboxes omit tracks. Blackout keeps a selected video clip's occupied region opaque instead of revealing its content.
- Video inspector fields control position, size, source crop, rotation, flip, opacity, borders and layout presets. Use these fields to reposition a recorded monitor or place sources side by side/overlapping.
- Canvas controls change output dimensions and optionally scale contents. Removing a display does not automatically promise a tight fit of all remaining bounds; adjust canvas size/layout for the intended result.
- Import appends media; Overlay imports at the playhead. Layer projects retain independent sources and edits. Ordinary video, audio and still images are supported.

## Animation and time

Keyframes interpolate clip/object position, size, rotation, opacity and audio gain. Keyframe times are relative to the clip/object. Split/trim operations preserve evaluated animation boundaries.

Clip controls include playback speed, reverse and a freeze at the playhead with adjustable hold duration. Variable-speed segments assign discrete rates at source offsets, not an eased continuous speed curve. Audio pitch is preserved.

Select two clips of the same kind, then use Tools → Crossfade selected clips. The overlap duration and fades remain editable.

## Drawings and privacy

Choose Pen, Line, Arrow, Rectangle, Blackout, Blur or Pixelate, then draw on the composition. Objects lists timed drawings, text and privacy regions. Use its numeric geometry, time, color and keyframe controls to refine an object; Delete selected object removes it. Direct canvas manipulation of existing video layers/objects is not required for these inspector operations.

Motion tracking is available for a selected privacy region. Review the affected frames before sharing: tracking estimates can drift or fail. A failed analysis reports an error instead of applying partial tracking. Manual keyframes remain available.

## Titles, captions and audio

Objects → Add text creates a title. Captions supports subtitle-row editing, SRT import/export and optional local speech recognition. The speech model is a separate optional download (`layer download-model`); it is not bundled with the application or uploaded with recordings. Review automatic text before export.

Independent audio clips expose timing, gain/keyframes, fades, denoise, normalization and mute. Tools → Record voiceover records a microphone through GSR, then imports it at the playhead as an independent source. Stop and add the recording, or cancel to discard it.

## Preview and processing

Video controls include brightness, contrast, saturation, gamma, warmth, tint, sharpness, 3D LUT and stabilization analysis. Missing LUT/analysis files produce an error. Paused seeking renders the edited composition; Play prepares an audiovisual preview using the export compositor. Source proxies can reduce preview work while export uses original media. Left/Right step by timeline frames.

HDR/10-bit capture choices follow GSR capabilities. The current editor is not certified as an HDR color-managed workflow; do not assume an HDR capture preference guarantees HDR preview/export parity.

## Export

Choose Export to set resolution, FPS, encoder and quality. GPU options depend on the available hardware/FFmpeg build and may still fail if a GPU lacks that codec. Audio-only WAV, MP3 and M4A and edited-frame PNG are separate outputs. For batch export, check recordings in the library and choose Batch export. Existing output files and original media are not overwritten.

## Editor keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| Space | Play/pause |
| Left / Right | Previous/next frame |
| Ctrl+B | Split selected clips |
| Delete | Remove selected clips |
| Ctrl+C / Ctrl+V | Copy/paste clips |
| Ctrl+D | Duplicate selected clips |
| Ctrl+Z / Ctrl+Shift+Z | Undo/redo |
| Ctrl+E | Export |

Global recording/overlay shortcuts are configured through GSR or the desktop's own bindings; see [Integration](INTEGRATION.md).
