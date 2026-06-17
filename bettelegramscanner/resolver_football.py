"""Resolver de picks de fútbol.

TODO: integrar fuente externa de resultados (API-Football, FlashScore, etc.)
      para verificación automática. Hasta entonces devuelve no_verificable.
"""
from __future__ import annotations

import logging
from datetime import date

from .models import FootballBetPayload, LegResolution, PickResolution

log = logging.getLogger(__name__)


async def resolve_football_pick(
    payload: FootballBetPayload,
    event_date: date,
) -> PickResolution:
    """Resuelve un pick de fútbol.

    Actualmente marca todo como no_verificable hasta que se integre
    una fuente de resultados (API-Football, Flashscore scraping…).
    """
    log.info(
        "Fútbol no_verificable: %s vs %s (%s)",
        payload.legs[0].equipo_local if payload.legs else "?",
        payload.legs[0].equipo_visitante if payload.legs else "?",
        event_date,
    )
    legs = [
        LegResolution(status="no_verificable", motivo="resolver de fútbol pendiente de integración")
        for _ in payload.legs
    ]
    return PickResolution(
        status="no_verificable",
        motivo="resolver de fútbol pendiente de integración",
        legs=legs,
    )
