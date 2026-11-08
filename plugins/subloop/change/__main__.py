"""Start only the change subloop; no transport or listener at import time."""
from __future__ import annotations


def main() -> int:
    import os
    from pathlib import Path
    import uvicorn
    from axcommon import creds
    from runtime.subloop.app import build_app
    from .integration.runtime import build_executor

    root = Path(os.environ.get("AX_HOME_IN_CONTAINER", "/ax"))
    ex, flow, errors = build_executor(root=root)
    app = build_app(ex, apply_token=creds.read_secret_optional("control-apply-token"),
                    run_loops=not errors, graph_errors=errors or None)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("AX_HEALTH_PORT", "8080")), log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
