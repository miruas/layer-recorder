# Upstream provenance and fork policy

Layer is a GPL-3.0-only source fork/distribution of **GPU Screen Recorder**, originally developed by **dec05eba**, extended with an independent-source recording workspace and native editor. It is an independent modified project; upstream authorship and copyright notices are preserved.

## Recorder foundation

- Official upstream: https://repo.dec05eba.com/gpu-screen-recorder
- Imported stable tag: `6.1.7`
- Exact upstream commit: `1d4946a16139b446e6308f741e9ba99f2ed5b3a7`
- Source location: `upstream/gpu-screen-recorder/`
- Import method: unsquashed Git subtree; upstream's full reachable commit history is preserved as an ancestor of Layer's main branch.
- Original license: `GPL-3.0-only`, preserved in the recorder subtree and the root license metadata.

The `upstream-gsr` branch points at the unchanged official base. View its history in a fresh clone with `git log origin/upstream-gsr`; use `git merge-base --is-ancestor <upstream-commit> main` to check ancestry. Configure the official remote in a fresh clone with `git remote add upstream https://repo.dec05eba.com/gpu-screen-recorder`. No private machine-local working history was imported.

The official project is hosted outside GitHub. This repository therefore has source-fork ancestry and attribution, but is not attached to a GitHub fork network and does not display a GitHub “forked from” badge. A third-party GitHub mirror is not substituted for the official upstream.

## Layer changes

Modified upstream files carry dated Layer change notices. The in-tree recorder's `--info` output adds `fork_name`, `fork_version` and `upstream_revision` while retaining the original `gsr_version` field for compatibility. Layer builds and uses this local recorder fork by default (`gsr_binary: auto`). The normal upstream CLI remains available.

The Layer workspace adds independent display/audio projects, a bounded RAM ring and durable long-recording spool, readable audio roles, source-window filtering, native Studio, video/audio editing and optional GSR UI bridging. NVIDIA/AMD/Intel profiles share the native recorder's hardware support, with a patched Wayland helper for fallback outputs.

The upstream sample environment's machine-specific recording directory is replaced by a generic relative example. A manual-page example uses a generic home-relative path. The original public documentation PNG is retained; its SHA-256 and upstream commit are recorded in `packaging/upstream-assets.json`. No private settings, credentials, recordings or local build products are included.

## Build and privileged capture helper

```sh
python tools/build-native.py gpu-screen-recorder
```

This builds from the source already included in this repository, including Layer's native modifications. It copies the unprivileged recorder and control CLI into the ignored local `bin/` directory. A matching KMS broker is also compiled. The build does not grant privileges or replace system binaries.

Direct KMS capture needs a compatible, properly installed `gsr-kms-server` on PATH. A distribution-installed GPU Screen Recorder package can supply it. The matching broker built from this fork is available under `build/native/gpu-screen-recorder/build/`; install/configure it only through an appropriate system packaging/administration workflow. The recorder reports missing/incompatible helper protocols rather than silently bypassing permissions.

For an explicit system recorder instead, set `gsr_binary` to `gpu-screen-recorder` or an absolute path. That is an intentional configuration choice; `auto` always selects the locally built Layer fork.

## Updating upstream

Fetch the selected official tag into a separate upstream ref, inspect its changes/license, then merge it with `git subtree pull --prefix=upstream/gpu-screen-recorder upstream <selected-tag>` without squashing history. Resolve Layer's small native changes deliberately, update the recorded upstream revision, asset provenance and native lock, rebuild and test capture. Do not overwrite upstream copyright notices or re-import local configuration/build products.

Third-party MIT components such as wf-recorder and whisper.cpp retain their own license texts. GPLv3 metadata does not replace those notices. Earlier standalone Layer commits are preserved in history; the fork distribution from0.5.1 uses GPL-3.0-only.
