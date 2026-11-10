#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail
mkdir -p /build /out
if [ ! -d /build/src/.git ]; then
    git -c safe.directory=/source clone --no-hardlinks /source /build/src
fi
chown -R builder:builder /build /out
runuser -u builder -- bash -c '
set -euo pipefail
cd /build/src
python3 tools/build-native.py gpu-screen-recorder
python3 tools/build-native.py wf-recorder
python3 tools/build-native.py gsr-ui --prefix /usr/lib/layer-recorder/overlay --destdir /build/overlay
python3 tools/build-native.py whisper
QT_QPA_PLATFORM=offscreen python3 tools/smoke-ui.py
LAYER_RELEASE_CONTAINER=1 python3 tools/build-release.py --overlay-prefix /build/overlay --speech-binary speech/whisper-cli
cp dist/*.pkg.tar.zst dist/*-corresponding-sources.tar.gz dist/*-SHA256SUMS /out/
'
