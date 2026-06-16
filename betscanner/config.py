"""Configuración centralizada de logging y constantes de rate-limit."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

TELEGRAM_PAUSE_SECONDS: float = 1.2
LLM_PAUSE_SECONDS: float = 0.8
BATCH_LOG_EVERY: int = 25

# Dedup visual
DEDUP_WINDOW_HOURS: int = int(os.environ.get("BETSCANNER_DEDUP_WINDOW_HOURS", "72"))
PHASH_HAMMING_MAX: int = int(os.environ.get("BETSCANNER_PHASH_HAMMING_MAX", "8"))

# Tennis Explorer
TE_RATE_SECONDS: float = float(os.environ.get("BETSCANNER_TE_RATE_SECONDS", "2.5"))
TE_FUZZY_THRESHOLD: int = int(os.environ.get("BETSCANNER_TE_FUZZY_THRESHOLD", "80"))
TE_DAY_WINDOW: int = int(os.environ.get("BETSCANNER_TE_DAY_WINDOW", "2"))
TE_USER_AGENT: str = os.environ.get(
    "BETSCANNER_TE_USER_AGENT",
    "BetScannerBot/0.1 (research; +https://example.local)",
)

# Output
REPORTS_DIR: Path = Path(os.environ.get("BETSCANNER_REPORTS_DIR", "reports"))

# Concurrencia (workers que procesan meses en paralelo)
WORKERS: int = int(os.environ.get("BETSCANNER_WORKERS", "3"))


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
