"""
Configure structured JSON logging.
Every log line is a valid JSON object — parseable by any log aggregator.
"""
import logging
import sys

from pythonjsonlogger.jsonlogger import JsonFormatter


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    fmt = JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        rename_fields={"asctime": "timestamp", "name": "logger", "levelname": "level"},
    )
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("celery").setLevel(logging.INFO)
    logging.getLogger("asyncio").setLevel(logging.WARNING)