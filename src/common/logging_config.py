"""Shared logging setup so every entry point logs consistently."""
from __future__ import annotations

import logging
from typing import Optional

DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO, *, fmt: Optional[str] = None) -> None:
    """Configure the root logger once (idempotent, safe to call repeatedly)."""
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return
    logging.basicConfig(level=level, format=fmt or DEFAULT_FORMAT)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
