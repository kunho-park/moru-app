"""Sidecar entrypoint: ``python -m moru_engine.server --port N --token T``.

Spawned by the Electron main process, which picks a free port and issues a
fresh session token per run. Binds 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from ..utils.log import setup_logging
from .app import create_app

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="moru-engine-server",
        description="Moru engine local API server (desktop sidecar).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address; loopback only by design (default: %(default)s).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=43110,
        help="TCP port to listen on (default: %(default)s).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Session token; falls back to the MORU_SESSION_TOKEN env var.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Root log level (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    token = args.token or os.environ.get("MORU_SESSION_TOKEN")
    if not token:
        parser.error("--token is required (or set MORU_SESSION_TOKEN)")

    setup_logging(getattr(logging, args.log_level))
    app = create_app(token=token)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_config=None)
    server = uvicorn.Server(config)
    # POST /shutdown flips server.should_exit for a graceful drain.
    app.state.uvicorn_server = server
    logger.info("Moru engine API listening on %s:%d", args.host, args.port)
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
