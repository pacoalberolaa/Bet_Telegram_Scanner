"""CLI: input interactivo del canal y rango de fechas (YYYY-MM-DD)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

DATE_FMT = "%Y-%m-%d"


@dataclass(frozen=True)
class ScanRequest:
    channel: str
    start: datetime
    end: datetime


def _parse_date(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), DATE_FMT)


def _prompt_date(label: str) -> datetime:
    while True:
        raw = input(f"{label} (YYYY-MM-DD): ")
        try:
            return _parse_date(raw)
        except ValueError:
            print("  -> formato inválido, se esperaba YYYY-MM-DD")


def _prompt_channel() -> str:
    while True:
        raw = input("Canal (@username o ID numérico): ").strip()
        if raw:
            return raw
        print("  -> el canal no puede estar vacío")


def collect_request() -> ScanRequest:
    channel = _prompt_channel()
    start = _prompt_date("Fecha inicio")
    end = _prompt_date("Fecha fin")
    if end < start:
        raise ValueError("fecha_fin no puede ser anterior a fecha_inicio")
    # Inclusivo: empuja `end` al final del día.
    end = end.replace(hour=23, minute=59, second=59)
    return ScanRequest(channel=channel, start=start, end=end)
