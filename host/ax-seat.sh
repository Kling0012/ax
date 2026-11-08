#!/bin/sh
# F-9.3. Run only after the cutover host-operation approval.
set -eu
AX_SEAT_SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# -I ignores PYTHONPATH and user site packages for privileged operations.
exec python3 -I "$AX_SEAT_SOURCE/lib/ax_gate.py" "$@"
