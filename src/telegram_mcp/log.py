"""Logging setup. Everything goes to stderr: stdout belongs to the MCP stdio transport."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    numeric_level = logging.getLevelNamesMapping()[level]
    logging.basicConfig(stream=sys.stderr, level=numeric_level, format="%(message)s", force=True)
    # Telethon is chatty at INFO (every connect and reconnect).
    logging.getLogger("telethon").setLevel(max(numeric_level, logging.WARNING))
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
