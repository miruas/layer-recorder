# Quick original-video export

Open **Export** and select **Quick export · copy original video** for an eligible
recording. Video packets are remuxed by the native Rust export worker; no video
decoder, compositor or encoder runs. Source resolution, frame rate, codec and
encoded video quality remain unchanged. Selected audio clips keep their timing
and gain and are mixed to 48 kHz AAC at 192 kbps, as in a normal video export.
An audio mix is processing: this mode does not promise lossless audio.

The mode currently requires one visible, unmodified video clip starting at zero,
with no source in-point or tail trim, covering its known complete media duration.
Its source dimensions must match the canvas. A bounded background FFprobe check verifies actual geometry, duration and display metadata; anamorphic or rotated inputs require rendering. Hidden video/audio tracks are
excluded. Multiple screens, sequential video clips, resizing, color changes,
blackouts, privacy effects, titles, captions, speed changes and fades require
rendering. The dialog explains why the quick mode is unavailable and keeps normal
export available. It never silently removes an effect to qualify for copying.

Partial video cuts deliberately require rendering. Packet copying between
keyframes may retain decoder preroll or extra tail frames. MP4 edit lists can
preserve the first requested frame in compatible players, but that alone does
not prove an exact entire cut. Choose normal rendering for precise boundaries.

MP4, MOV and MKV destinations are supported. The source video codec must be
compatible with the chosen container; choose MKV or a normal rendered export if
remuxing fails. New output filenames are required. Original media is not changed;
outputs publish only after completion, and cancellation removes temporary media.

The headless command uses the same native worker without opening a window:

```sh
layer export /path/to/project.katman --output /path/to/clip.mp4 --fast
```

An ineligible command reports the reason instead of silently switching methods.
Normal `layer export` retains the accurate compositor.

## Render speed controls

Normal export provides **Fastest**, **Balanced** and **Better compression**.
They choose codec-specific speed presets while preserving the selected quality:
x264/x265 use `ultrafast`/`fast`/`slow`; NVENC uses `p1`/`p4`/`p7`; CPU AV1 uses
`cpu-used` 8/6/4. File sizes and encoding efficiency can differ at the same quality
setting. VA-API speed controls are driver-specific, so the generic selector is
disabled for VA-API. The renderer API accepts explicit `vaapi_quality` levels
when supported by the device.

The renderer also accepts validated `threads` and `filter_threads` values from
0 (automatic) to 64. Defaults remain 4 and 2. More threads are not always faster,
particularly when several high-resolution sources compete for memory bandwidth.
Static transform scales no longer evaluate animated dimensions on every frame;
redundant final scale/pad operations are omitted only when proven equivalent.
Generated-media regressions compare optimized and reference frames byte for byte.

## Example measurement

One generated-media run on Linux x86-64 used a three-second 1920 × 1080, 60 FPS
H.264 video with two synthetic audio sources. Original-video copying plus AAC mix
took 0.134 seconds; the default libx264 `fast`, quality 20 compositor export took
1.036 seconds. The copied video's encoded packet SHA-256 hashes matched the
source. Outputs were 10.4 MB and 3.9 MB respectively: copying preserves the input
encoding rather than improving its compression. This short warm-system example
is not a general speedup or hardware guarantee. Source creation was excluded.
