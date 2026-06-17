"""Detector de extracciones dudosas + logger JSONL para revisión manual.

Cada vez que la IA extrae un boleto, comprobamos heurísticas genéricas
y específicas por deporte. Si algo huele mal, lo escribimos en
`reports/low_confidence.jsonl` para que el usuario pueda revisar la imagen
junto al payload y refinar el prompt/schema más adelante.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from .ingest_export import MessageCandidate
from .models import (
    AnyBetPayload,
    BasketballBetPayload,
    DartsBetPayload,
    FootballBetPayload,
    TennisBetPayload,
)

log = logging.getLogger(__name__)


_TENNIS_MERCADOS_LINEA = {
    "handicap_games", "handicap_sets", "over_under_games", "over_under_sets",
    "over_under_games_set1", "first_set_ou_games", "over_under_aces",
    "over_under_aces_jugador",
}
_FOOTBALL_MERCADOS_LINEA = {
    "over_under_goles", "over_under_goles_primera", "handicap_asiatico",
    "handicap_europeo", "goles_equipo_ou", "tarjetas_ou", "corners_ou",
}
_DARTS_MERCADOS_LINEA = {
    "handicap_legs", "over_under_legs", "180s_match", "checkout_mayor",
}
_BASKETBALL_MERCADOS_LINEA = {
    "handicap_puntos", "over_under_puntos", "over_under_puntos_equipo",
    "over_under_mitad", "over_under_cuarto", "handicap_mitad",
    "puntos_jugador", "rebotes_jugador", "asistencias_jugador", "triples_jugador",
    "asistencias_rebotes_jugador", "puntos_rebotes_jugador",
    "puntos_asistencias_jugador", "puntos_rebotes_asistencias_jugador",
    "race_to_puntos",
}


def _placeholder(value: str | None) -> bool:
    if not value:
        return True
    v = value.strip()
    return v in {"", "?", "-", "n/a", "N/A"}


def detect_low_confidence(payload: AnyBetPayload) -> list[str]:
    """Devuelve la lista de razones por las que el payload parece dudoso.

    Lista vacía = extracción aparentemente fiable.
    """
    reasons: list[str] = []

    if not payload.es_pick:
        return reasons

    if not payload.legs:
        reasons.append("es_pick=true pero legs vacías")
    if payload.casa_apuestas.strip().lower() == "desconocida":
        reasons.append("casa de apuestas no identificada")
    if payload.cuota_total <= 1.0:
        reasons.append("cuota_total <=1.0 (probable ilegible)")

    if isinstance(payload, TennisBetPayload):
        linea_set = _TENNIS_MERCADOS_LINEA
        for i, leg in enumerate(payload.legs):
            if _placeholder(leg.jugador_1):
                reasons.append(f"leg{i+1}: jugador_1 vacío/placeholder")
            if _placeholder(leg.jugador_2):
                reasons.append(f"leg{i+1}: jugador_2 vacío/placeholder")
            if _placeholder(leg.seleccion):
                reasons.append(f"leg{i+1}: seleccion vacía")
            if leg.mercado in linea_set and leg.linea is None:
                reasons.append(f"leg{i+1}: mercado '{leg.mercado}' requiere línea")
    elif isinstance(payload, FootballBetPayload):
        linea_set = _FOOTBALL_MERCADOS_LINEA
        for i, leg in enumerate(payload.legs):
            if _placeholder(leg.equipo_local):
                reasons.append(f"leg{i+1}: equipo_local vacío/placeholder")
            if _placeholder(leg.equipo_visitante):
                reasons.append(f"leg{i+1}: equipo_visitante vacío/placeholder")
            if _placeholder(leg.seleccion):
                reasons.append(f"leg{i+1}: seleccion vacía")
            if leg.mercado in linea_set and leg.linea is None:
                reasons.append(f"leg{i+1}: mercado '{leg.mercado}' requiere línea")
    elif isinstance(payload, DartsBetPayload):
        linea_set = _DARTS_MERCADOS_LINEA
        for i, leg in enumerate(payload.legs):
            if _placeholder(leg.jugador_1):
                reasons.append(f"leg{i+1}: jugador_1 vacío/placeholder")
            if _placeholder(leg.jugador_2):
                reasons.append(f"leg{i+1}: jugador_2 vacío/placeholder")
            if _placeholder(leg.seleccion):
                reasons.append(f"leg{i+1}: seleccion vacía")
            if leg.mercado in linea_set and leg.linea is None:
                reasons.append(f"leg{i+1}: mercado '{leg.mercado}' requiere línea")
    elif isinstance(payload, BasketballBetPayload):
        linea_set = _BASKETBALL_MERCADOS_LINEA
        ou_required = {
            "puntos_jugador", "rebotes_jugador", "asistencias_jugador", "triples_jugador",
            "asistencias_rebotes_jugador", "puntos_rebotes_jugador",
            "puntos_asistencias_jugador", "puntos_rebotes_asistencias_jugador",
            "over_under_puntos_equipo", "doble_doble_jugador", "triple_doble_jugador",
        }
        for i, leg in enumerate(payload.legs):
            if _placeholder(leg.equipo_local):
                reasons.append(f"leg{i+1}: equipo_local vacío/placeholder")
            if _placeholder(leg.equipo_visitante):
                reasons.append(f"leg{i+1}: equipo_visitante vacío/placeholder")
            if _placeholder(leg.seleccion):
                reasons.append(f"leg{i+1}: seleccion vacía")
            if leg.mercado in linea_set and leg.linea is None:
                reasons.append(f"leg{i+1}: mercado '{leg.mercado}' requiere línea")
            if leg.mercado in ou_required and leg.over_under is None:
                reasons.append(f"leg{i+1}: mercado '{leg.mercado}' requiere over_under")

    return reasons


class LowConfidenceLogger:
    """Append-only JSONL writer protegido por un asyncio.Lock."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def log(
        self,
        candidate: MessageCandidate,
        payload: AnyBetPayload,
        reasons: list[str],
    ) -> None:
        entry = {
            "logged_at_utc": datetime.utcnow().isoformat(timespec="seconds"),
            "tipster": candidate.channel,
            "message_id": candidate.message_id,
            "date_utc": candidate.date_utc_naive.isoformat(timespec="seconds"),
            "photo_path": str(candidate.photo_path),
            "raw_text": candidate.raw_text,
            "sport": payload.sport,
            "reasons": reasons,
            "payload": payload.model_dump(mode="json"),
        }
        line = json.dumps(entry, ensure_ascii=False)
        async with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        log.info(
            "Low-confidence tipster=%s msg=%s sport=%s reasons=%d",
            candidate.channel, candidate.message_id, payload.sport, len(reasons),
        )
