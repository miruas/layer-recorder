# Recording and Studio guide

## Capture

Open the native application or use:

```sh
layer record   # start/stop normal recording
layer replay   # enable/disable replay
layer save-1   # save the last available minute
layer save-5   # save the last available five minutes
layer status
```

Replay and normal recording can run together. Saved projects contain original independent media plus edit decisions. The folder name follows application identity, not browser-tab or conversation titles. Saving does not stop replay or display a success notification. Errors remain visible.

All active displays and available audio streams are captured independently. Source selection happens afterward. A stream that appears mid-capture is discovered. New saves omit application sources outside the selected time window and genuinely silent application tracks. Microphone tracks remain selectable. PipeWire must expose voice/media separately for independent selection.

## Open Studio

Choose a recording in the library. Studio opens with sources on the left, the composition and independent timeline in the center, and Clip/Video/Audio/Objects controls on the right. Closing Studio returns to the library.

Edits save automatically and preserve original media. Undo/Redo covers edit decisions. Keep the project and its media folder together.

## Timeline and composition

- Drag a clip body to move it; drag edges to trim. Use numeric Clip fields for exact placement/source ranges.
- Control-click selects multiple clips. Split, remove, duplicate, copy/paste, snap and zoom are available.
- Lower video tracks appear in front. Use the source-tree context menu to reorder tracks.
- Uncheck a source to omit it. With **Fit remaining displays**, excluding a recorded display also removes its occupied frame bounds. Blackout instead retains opaque space.
- Video offers position/size, source crop, rotate/flip, opacity, borders and layout presets. Move/resize layers directly on the canvas. Crop canvas affects the whole composition.
- Append imports at the end; Overlay imports at the playhead. Imported Layer projects retain their independent sources and edits in both modes. Ordinary video, audio and still images are supported.

## Animation and time

Animation keyframes interpolate position, size, rotation, opacity and gain. Times are relative to the clip/object. Splitting/trimming preserves evaluated animation boundaries.

Clip controls support 0.25×–4× playback, reverse and freeze at the playhead with adjustable hold duration. Variable speed assigns discrete rates at source offsets; it is not an eased continuous speed curve. Audio pitch is preserved.

Select two video or two audio clips and choose Crossfade. The overlap and fades remain editable.

## Drawings and privacy

Choose Pen, Line, Arrow, Rectangle, Blackout, Blur or Pixelate and draw on the composition. Eraser removes a complete stroke. Objects lists timed drawings, titles and regions; edit their start/end, geometry, color and animation numerically. Select mode supports moving/resizing.

A selected privacy region can be motion-tracked. Review every frame of the affected range before sharing: tracking estimates can drift or fail. A failed analysis reports an error rather than applying partial tracking. Manual keyframes remain available.

## Titles, subtitles and audio

Add Text creates an editable title. Captions supports editing, SRT import/export and optional local automatic recognition. Install the speech engine/model separately, choose a source or the current mix and review the transcription.

Independent audio clips have timing, gain/keyframes, fades, denoise, normalization and mute. Record voiceover adds an independent microphone source at the playhead.

## Image and preview

Video controls include brightness, contrast, saturation, gamma, warmth, tint, sharpness, 3D LUT and stabilization analysis. Missing LUT/analysis files produce an error.

Paused seeking renders the actual edited composition. Play builds an audiovisual preview with the same compositor graph as export. Preview quality options and source proxies reduce working resolution; exports use original media. Frame buttons and Left/Right step at timeline FPS.

## Export

Set resolution, FPS, encoder and quality in Export. Available GPU families follow the local hardware/FFmpeg build; unsupported codecs report errors. WAV, MP3 and M4A audio-only export, multi-project batch export and edited-frame PNG are supported. Existing files and original media cannot be overwritten.

## Keyboard

| Shortcut | Studio action |
| --- | --- |
| Space | Play/pause |
| Left / Right | Previous/next frame |
| Home / End | Timeline start/end |
| Ctrl+B | Split selected |
| Delete | Remove selected |
| Ctrl+C / Ctrl+V | Copy/paste clips |
| Ctrl+D | Duplicate |
| Ctrl+A | Select all clips |
| Ctrl+Z / Ctrl+Shift+Z | Undo/redo |
| Ctrl+I | Append media |
| Ctrl+E | Export |
