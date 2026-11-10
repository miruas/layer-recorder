# Testing and verification

## Portable checks

```sh
python -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen python tools/smoke-ui.py
python tools/check-public-tree.py
```

Tests generate their own colored video/tone media in temporary directories. They do not require or distribute personal recordings. Use isolated XDG configuration/cache directories when testing on a machine with an existing installation.

Coverage includes decoded composition pixels, independent audio, cropping, privacy effects, transforms, keyframes, splits/trims/fades, speed/reverse/freeze, subtitles, source-preserving imports, proxies/stabilization, original-media hashes, RAM/spool continuity, source-window membership, PipeWire role metadata, vendor commands, settings validation and installer ownership/idempotence.

The native settings smoke test uses a temporary configuration/library, does not start capture and verifies that selecting a GPU profile/codec survives Save. Screenshots are optional local QA output; none are bundled.

## Real hardware

```sh
LAYER_TEST_GPU=1 python -m unittest discover -s tests -v
LAYER_TEST_VAAPI_DEVICE=/dev/dri/renderD<number> \
  python -m unittest discover -s tests -v
```

Choose a VA-API render node supported by your driver. NVIDIA tests require working H.264/HEVC/AV1 NVENC support for the supplied matrix. VA-API tests require H.264/HEVC encoding. These tests intentionally fail on unsupported requested hardware/codecs; default CI skips hardware-only checks.

For release0.5.0, all61 local tests passed with NVIDIA and AMD hardware enabled. This includes actual NVENC video exports and AMD VA-API H.264/HEVC exports. A separate live AMD Wayland capture produced a7.46sec project with independent audio and no capture warning. The patched wf-recorder and GSR UI builds completed locally. Intel command/profile paths are unit-tested, with physical Intel validation still open. No new five-minute soak was performed for the cross-vendor packaging change.

CI runs CPU/media/configuration/UI/package checks on Python3.12–3.14. It has no hardware GPU and must not be described as hardware verification.

## Build and privacy checks

`python -m build` produces source and wheel distributions. Inspect the source archive before sharing a release. `tools/check-public-tree.py` audits tracked source paths/text, rejects binary files, machine home/media paths and common credential patterns. This is a release guard, not a guarantee that an arbitrary repository is free of sensitive content; review staged files too.

Optional native sources are pinned in `packaging/native-lock.json`. Patches are published, compiled outputs ignored. Rebuild after changing a patch, and repeat a real short capture on the affected backend before claiming support.
