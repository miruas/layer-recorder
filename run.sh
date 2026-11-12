#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
LAYER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PATH="$LAYER_ROOT/bin:$PATH"
export PYTHONPATH="$LAYER_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "${LAYER_PYTHON:-python3}" -m katman "$@"
