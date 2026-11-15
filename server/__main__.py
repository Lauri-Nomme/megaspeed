"""Local dev server entrypoint: ``python3 -m server``.

    python3 -m server --host 127.0.0.1 --port 8765
    python3 -m server --reload            # for UI work

Serves the read-only API, the WebSocket eval/screenshot bridge, and ``app/``.
"""

from __future__ import annotations

import argparse
import os

DEFAULT_PORT = int(os.environ.get("MEGASPEED_PORT", "8765"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m server", description="megaspeed dev server")
    ap.add_argument("--host", default=os.environ.get("MEGASPEED_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--reload", action="store_true", help="auto-reload on code changes (UI/API work)")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args(argv)

    import uvicorn

    if args.reload:
        uvicorn.run(
            "server.app:app",
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=["server", "ingest"],
            log_level=args.log_level,
        )
    else:
        from server.app import app

        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
