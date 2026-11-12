# Editor controls and properties

## Toolbar and library

The top toolbar groups navigation and capture controls. **Recordings** opens or hides the library. Opening a recording hides the library to give the canvas more room; it remains available from Recordings. The library has search, Refresh, Open recording and selection for batch export. The library and Properties panels can be resized.

**Record & replay** contains the current recording/replay actions. Record and Replay appear while idle. Replay save durations appear during replay; Pause or Resume appears during recording and stays disabled when GPU Screen Recorder cannot pause the current capture mode. **Capture** contains streaming, region capture, screenshots, the GSR overlay, settings and status refresh. Configured replay durations and canonical GSR action commands are unchanged.

**Properties** opens or hides the selected editing controls. Double-clicking a timeline clip opens Properties. **Preferences** opens application settings. On narrower windows, the toolbar uses a shorter title and omits the extra capture status summary.

## Editing menus and canvas tools

The recording's editing bar keeps related actions together:

- **Add media:** Append recording, Overlay media and Record voiceover.
- **Edit:** Undo, Redo and Remove selected.
- **Split:** splits selected clips at the current timeline position.
- **Tools:** Build source proxies, Save current frame and the duration/action for crossfading two selected clips.
- **Export:** opens export settings for the current recording. Batch export remains in the library.

Canvas controls expose **Select**, a **Draw** menu for Pen, Line, Arrow and Rectangle, and a **Privacy** menu for Blackout, Blur and Pixelate. Drawing color and stroke width appear when a drawing tool is selected. Playback and timeline position remain near the canvas/timeline rather than inside clip properties.

## Properties

Properties follow the current editing task. Clip starts with identification and timing. Speed, reverse playback, freeze frames and variable speed remain in **Speed and freeze frame**. Video keeps transform and crop visible, with **Color and effects** and **Animation keyframes** available below. Audio keeps level, fades and mute visible, with **Audio cleanup** and **Gain keyframes** below. Text objects expose **Font properties** separately; captions expose **Caption style** separately. Native controls retain keyboard focus and editing behavior.

Object creation, privacy region tracking, subtitles, transcription and canvas editing remain available from their corresponding properties tabs. Timeline position, source proxy generation and frame export belong to global editing controls.

## Undo, saving and responsiveness

Idle clip inspection copies only the selected clip. Objects and captions snapshot their own JSON fields. Canvas inspection snapshots only dimensions, frame rate and background; tracks and annotations are included when a canvas edit scales contents.

A changed scope is temporarily swapped back to its original JSON while the document creates an undo checkpoint, then the edited scope is restored. This preserves unrelated tracks, unknown extension fields and absent optional fields. Idle inspection creates no model/history changes. A continuous pointer gesture uses one undo checkpoint. A released pointer, scope change or external document revision ends grouping. Keyboard edits create normal checkpoints.

Saving runs in the background and coalesces pending changes. Previews receive the current in-memory model and source snapshot, while jobs that need the saved project wait for its save barrier. Save failures remain visible; closing waits for the save worker's flush result.

Supported paused edits redraw cached source textures; other scenes use the accurate compositor. Play streams composed frames directly rather than first creating a timeline movie. See [Preview performance](PERFORMANCE.md) for supported paths, limits and reproducible measurements.

Headless checks cover idle properties tabs, scoped clip edits and grouped undo/redo, object/caption changes, canvas scaling with absent-field restoration, source snapshots and streamed preview data. Live visual verification is separate from these checks.

## Preview controls

The play row contains Native/1080p/1440p/4K preview bounds and Source/60/120 FPS cadence. Native and Source are the defaults. The dimension/cadence label reports the request; Playing FPS reports delivered frames while playback runs. A higher cadence can repeat a slower source and does not add captured detail. Preview choices persist independently from capture and export.

Privacy regions and titles remain separate overlays while editing. Changing their geometry preserves source-frame and underlay caches; selecting an unsupported scene retains the accurate compositor. Captions and advanced base effects can still require processing.
