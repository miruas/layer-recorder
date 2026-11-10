#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail
mkdir -p /build /out
if [ ! -d /build/src/.git ]; then
    git -c safe.directory=/source -c safe.directory=/source/.git clone --no-hardlinks /source /build/src
else
    git -c safe.directory='*' -C /build/src fetch /source HEAD
    git -c safe.directory='*' -C /build/src reset --hard FETCH_HEAD
fi
chown -R builder:builder /build /out
runuser -u builder -- bash -c '
set -euo pipefail
cd /build/src
python3 tools/build-native.py gpu-screen-recorder --reuse
python3 tools/build-native.py wf-recorder --reuse
python3 tools/build-native.py gsr-ui --reuse --prefix /usr/lib/layer-recorder/overlay --destdir /build/overlay
python3 tools/build-native.py whisper --reuse
QT_QPA_PLATFORM=offscreen python3 tools/smoke-ui.py
LAYER_RELEASE_CONTAINER=1 python3 tools/build-release.py --overlay-prefix /build/overlay --speech-binary speech/whisper-cli
cp dist/*.pkg.tar.zst dist/*-corresponding-sources.tar.gz dist/*-SHA256SUMS /out/
'
