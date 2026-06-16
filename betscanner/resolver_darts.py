"""Resolver de picks de dardos.

TODO: integrar fuente externa de resultados (FlashScore, Darts Database, etc.)
      para verificación automática. Hasta entonces devuelve no_verificable.
"""
from __future__ import annotations

import logging
from datetime import date

from .models import DartsBetPayload, LegResolution, PickResolution

log = logging.getLogger(__name__)


async def resolve_darts_pick(
    payload: DartsBetPayload,
    event_date: date,
) -> PickResolution:
    """Resuelve un pick de dardos.

    Actualmente marca todo como no_verificable hasta que se integre
    una fuente de resultados (FlashScore scraping, Darts Database API…).
    """
    log.info(
        "Dardos no_verificable: %s vs %s (%s)",
        payload.legs[0].jugador_1 if payload.legs else "?",
        payload.legs[0].jugador_2 if payload.legs else "?",
        event_date,
    )
    legs = [
        LegResolution(status="no_verificable", motivo="resolver de dardos pendiente de integración")
        for _ in payload.legs
    ]
    return PickResolution(
        status="no_verificable",
        motivo="resolver de dardos pendiente de integración",
        legs=legs,
    )
