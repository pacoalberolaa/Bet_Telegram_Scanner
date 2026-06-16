"""Esquema canónico de pick (tenis) + resolución verificada externamente."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

TennisMercado = Literal[
    "moneyline",          # ganador del partido (1X2 a 2 vías)
    "handicap_games",     # +/- juegos. linea = handicap (ej: -3.5)
    "handicap_sets",      # +/- sets. linea = handicap (ej: -1.5)
    "over_under_games",   # total de juegos. linea = total (ej: 22.5)
    "over_under_sets",    # total de sets. linea = total (ej: 2.5)
    "set_betting",        # marcador exacto de sets. seleccion = "2-0", "2-1", "1-2", "0-2"
]

ResolutionStatus = Literal["ganada", "perdida", "void", "no_verificable"]


class TennisLeg(BaseModel):
    """Una pierna del boleto. Para sencillos solo habrá una."""

    jugador_1: str = Field(description="Primer jugador tal y como aparece en el boleto (apellido o nombre completo).")
    jugador_2: str = Field(description="Segundo jugador tal y como aparece en el boleto.")
    torneo: str | None = Field(default=None, description="Torneo si se ve (ej: 'Roland Garros', 'Madrid ATP'). null si no.")
    fecha_evento: datetime | None = Field(
        default=None,
        description="Fecha del partido en formato YYYY-MM-DD si es visible. null si no aparece.",
    )
    mercado: TennisMercado
    seleccion: str = Field(
        description=(
            "Para moneyline/handicap: nombre exacto del jugador apostado tal como aparece. "
            "Para over_under: 'over' o 'under'. Para set_betting: '2-0', '2-1', '1-2' o '0-2'."
        ),
    )
    linea: float | None = Field(
        default=None,
        description="Línea numérica para handicap (-3.5, +1.5) o totales (22.5, 2.5). null para moneyline y set_betting.",
    )
    cuota_individual: float | None = Field(default=None, description="Cuota individual de la pierna si se ve. null si no.")


class BetPayload(BaseModel):
    """Datos extraídos por el LLM desde la imagen del boleto (tenis)."""

    casa_apuestas: str = Field(description="Bookmaker visible (Bet365, Bwin, etc). 'desconocida' si no se ve.")
    legs: list[TennisLeg] = Field(default_factory=list, description="Una pierna por mercado. Para sencillos, una sola.")
    cuota_total: float = Field(description="Cuota combinada final (>=1.0).")
    stake_indicado: float | None = Field(
        default=None,
        description="Stake en unidades si el tipster lo declara (1u, 0.5u, 'stake 2'). null si no.",
    )
    es_pick: bool = Field(
        description=(
            "true si la imagen es un boleto/pick de apuestas. "
            "false si es ruido (stats, meme, captura promocional, racha, picture sin boleto)."
        ),
    )


class LegResolution(BaseModel):
    """Resultado real de una pierna, verificado externamente."""

    status: ResolutionStatus
    motivo: str | None = None
    marcador: str | None = Field(default=None, description="Marcador real del partido, ej: '6-4 6-2'.")


class PickResolution(BaseModel):
    """Resultado real del boleto completo. Una sola pierna no_verificable -> todo no_verificable."""

    status: ResolutionStatus
    motivo: str | None = None
    legs: list[LegResolution] = Field(default_factory=list)


class PickDocument(BaseModel):
    """Documento persistido en MongoDB."""

    tipster: str
    message_id: int
    date_utc: datetime
    phash: int
    phash_bits: int = 64
    text_raw: str
    payload: BetPayload
    resolution: PickResolution | None = None
    profit_units: float | None = None  # None si no_verificable
