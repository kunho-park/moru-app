"""Shared colorlog setup for entrypoints (CLI, server, tools)."""

from __future__ import annotations

import logging

import colorlog


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging with colorlog.

    Idempotent: repeated calls do not stack handlers.

    Args:
        level: Root log level.
    """
    root = colorlog.getLogger()
    if any(isinstance(h, colorlog.StreamHandler) for h in root.handlers):
        root.setLevel(level)
        return

    handler = colorlog.StreamHandler()
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(levelname)-8s%(reset)s %(name)s: %(message)s",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red,bg_white",
            },
        )
    )
    root.addHandler(handler)
    root.setLevel(level)
