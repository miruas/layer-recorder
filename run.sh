#!/bin/sh
LAYER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$LAYER_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "${LAYER_PYTHON:-python3}" -m katman "$@"
