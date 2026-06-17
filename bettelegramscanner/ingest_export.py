"""Lectura de exports de Telegram Desktop (carpeta ChatExport_*)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageCandidate:
    """Mensaje pre-filtrado que sí merece análisis de imagen."""
    channel: str            # etiqueta del tipster (clave de agregación)
    message_id: int
    date_utc_naive: datetime
    raw_text: str
    photo_path: Path        # ruta absoluta al jpg/png del export


def _extract_text(text) -> str:
    """El campo `text` puede ser string o lista de entidades (bold, link...)."""
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts: list[str] = []
        for t in text:
            if isinstance(t, str):
                parts.append(t)
            elif isinstance(t, dict):
                parts.append(t.get("text", ""))
        return "".join(parts)
    return ""


def _to_datetime(msg: dict) -> datetime | None:
    """Prefiere `date_unixtime` (UTC) sobre `date` (hora local del exportador)."""
    unix = msg.get("date_unixtime")
    if unix is not None:
        try:
            return datetime.utcfromtimestamp(int(unix))
        except (TypeError, ValueError):
            pass
    raw = msg.get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class ExportIngest:
    """Carga `result.json` en memoria y expone iteración filtrada por ventana."""

    def __init__(self, folder: Path, tipster: str) -> None:
        self.folder = folder
        self.tipster = tipster
        self._messages: list[dict] = []
        self._load()

    @property
    def detected_name(self) -> str:
        return self._detected_name

    def _load(self) -> None:
        result_path = self.folder / "result.json"
        if not result_path.exists():
            raise FileNotFoundError(
                f"No encuentro {result_path}. ¿Es la carpeta correcta del export?"
            )
        with result_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self._detected_name = data.get("name", self.folder.name)
        self._messages = data.get("messages", [])
        log.info(
            "Export cargado: tipster=%s mensajes_totales=%d carpeta=%s",
            self.tipster, len(self._messages), self.folder,
        )

    def iter_in_window(self, start: datetime, end: datetime) -> Iterator[MessageCandidate]:
        seen = emitted = 0
        discarded = {"no_message": 0, "fwd": 0, "reply": 0, "no_photo": 0, "fuera_ventana": 0, "foto_missing": 0}

        for msg in self._messages:
            if msg.get("type") != "message":
                discarded["no_message"] += 1
                continue
            if msg.get("forwarded_from") is not None:
                discarded["fwd"] += 1
                continue
            if msg.get("reply_to_message_id") is not None:
                discarded["reply"] += 1
                continue
            photo_rel = msg.get("photo")
            if not photo_rel:
                discarded["no_photo"] += 1
                continue

            dt = _to_datetime(msg)
            if dt is None or dt < start or dt > end:
                discarded["fuera_ventana"] += 1
                continue

            photo_path = (self.folder / photo_rel).resolve()
            if not photo_path.exists():
                discarded["foto_missing"] += 1
                log.debug("Foto declarada en JSON pero no en disco: %s", photo_path)
                continue

            seen += 1
            emitted += 1
            yield MessageCandidate(
                channel=self.tipster,
                message_id=int(msg["id"]),
                date_utc_naive=dt,
                raw_text=_extract_text(msg.get("text", "")),
                photo_path=photo_path,
            )

        log.info(
            "Ventana [%s..%s] tipster=%s emitidos=%d descartados=%s",
            start.date(), end.date(), self.tipster, emitted, discarded,
        )
