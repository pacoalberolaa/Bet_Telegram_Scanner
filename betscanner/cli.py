"""CLI: carpeta del export + rango de fechas. Hard cap 6 meses."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DATE_FMT = "%Y-%m-%d"
MAX_WINDOW_DAYS = 183  # ~6 meses


@dataclass(frozen=True)
class ScanRequest:
    export_folder: Path
    start: datetime
    end: datetime
    tipster: str


def _parse_date(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), DATE_FMT)


def _prompt_date(label: str) -> datetime:
    while True:
        raw = input(f"{label} (YYYY-MM-DD): ")
        try:
            return _parse_date(raw)
        except ValueError:
            print("  -> formato inválido, se esperaba YYYY-MM-DD")


def _prompt_folder() -> Path:
    while True:
        raw = input("Carpeta del export (ChatExport_*): ").strip().strip('"').strip("'")
        if not raw:
            print("  -> ruta vacía")
            continue
        p = Path(raw).expanduser()
        if not p.is_dir():
            print(f"  -> no existe: {p}")
            continue
        if not (p / "result.json").exists():
            print("  -> esa carpeta no tiene result.json (¿es el export correcto?)")
            continue
        return p


def _suggested_tipster(folder: Path) -> str:
    try:
        data = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        return str(data.get("name") or "").strip()
    except Exception:
        return ""


def _prompt_tipster(suggested: str) -> str:
    while True:
        prompt = f"Etiqueta del tipster [{suggested}]: " if suggested else "Etiqueta del tipster: "
        raw = input(prompt).strip() or suggested
        if raw:
            return raw
        print("  -> no puede estar vacía")


def collect_request() -> ScanRequest:
    folder = _prompt_folder()
    suggested = _suggested_tipster(folder)
    tipster = _prompt_tipster(suggested)
    start = _prompt_date("Fecha inicio")
    end = _prompt_date("Fecha fin")
    if end < start:
        raise ValueError("fecha_fin no puede ser anterior a fecha_inicio")
    end = end.replace(hour=23, minute=59, second=59)
    span_days = (end - start).days
    if span_days > MAX_WINDOW_DAYS:
        raise ValueError(
            f"Ventana de {span_days} días excede el máximo de {MAX_WINDOW_DAYS} "
            "(~6 meses). Lanza la consulta en dos ejecuciones."
        )
    return ScanRequest(export_folder=folder, start=start, end=end, tipster=tipster)
