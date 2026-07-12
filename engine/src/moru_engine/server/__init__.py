"""Local FastAPI sidecar for the Moru desktop app.

Run as ``python -m moru_engine.server --port N --token T`` (spawned by
Electron main) or embed via :func:`create_app`.
"""

from __future__ import annotations

from .__main__ import main
from .app import create_app

__all__ = ["create_app", "main"]
