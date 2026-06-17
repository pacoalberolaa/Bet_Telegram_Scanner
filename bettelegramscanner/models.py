"""Esquemas canónicos de picks multi-deporte + resolución verificada externamente."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Resolución (compartida por todos los deportes)
# ---------------------------------------------------------------------------

ResolutionStatus = Literal["ganada", "perdida", "void", "no_verificable"]


class LegResolution(BaseModel):
    """Resultado real de una pierna, verificado externamente."""

    status: ResolutionStatus
    motivo: str | None = None
    marcador: str | None = Field(default=None, description="Marcador real del encuentro.")


class PickResolution(BaseModel):
    """Resultado real del boleto completo. Una sola pierna no_verificable -> todo no_verificable."""

    status: ResolutionStatus
    motivo: str | None = None
    legs: list[LegResolution] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# TENIS
# ---------------------------------------------------------------------------

TennisMercado = Literal[
    "moneyline",              # ganador del partido (1X2 a 2 vías)
    "handicap_games",         # +/- juegos. linea = handicap (ej: -3.5)
    "handicap_sets",          # +/- sets. linea = handicap (ej: -1.5)
    "over_under_games",       # total de juegos del partido. linea = total (ej: 22.5)
    "over_under_sets",        # total de sets. linea = total (ej: 2.5)
    "set_betting",            # marcador exacto de sets. seleccion = "2-0", "2-1", "1-2", "0-2"
    "first_set_winner",       # ganador del primer set. seleccion = nombre del jugador
    "over_under_games_set1",  # total juegos del primer set. linea = valor (ej: 10.5)
    "over_under_aces",        # total aces del partido. linea = valor (ej: 20.5)
    "over_under_aces_jugador",# aces de un jugador concreto. linea = valor; seleccion = nombre jugador
    "tiebreak_yn",            # ¿habrá tie-break en el partido? seleccion = "si" | "no"
    "first_set_ou_games",     # alias: igual que over_under_games_set1 (algunas bookies lo nombran así)
]


class TennisLeg(BaseModel):
    """Una pierna de tenis."""

    jugador_1: str = Field(description="Primer jugador tal y como aparece en el boleto.")
    jugador_2: str = Field(description="Segundo jugador tal y como aparece en el boleto.")
    torneo: str | None = Field(default=None, description="Torneo si se ve. null si no.")
    fecha_evento: datetime | None = Field(
        default=None,
        description="Fecha del partido en formato YYYY-MM-DD si es visible. null si no aparece.",
    )
    mercado: TennisMercado
    seleccion: str = Field(
        description=(
            "Para moneyline/handicap_games/handicap_sets/first_set_winner: nombre del jugador apostado. "
            "Para over_under_*: 'over' o 'under'. "
            "Para set_betting: '2-0', '2-1', '1-2' o '0-2'. "
            "Para over_under_aces_jugador: nombre del jugador. "
            "Para tiebreak_yn: 'si' o 'no'."
        ),
    )
    linea: float | None = Field(
        default=None,
        description=(
            "Línea numérica para handicap (-3.5, +1.5), totales de juegos (22.5), "
            "sets (2.5), aces (20.5), juegos del primer set (10.5). "
            "null para moneyline, set_betting, first_set_winner y tiebreak_yn."
        ),
    )
    cuota_individual: float | None = Field(default=None, description="Cuota individual si se ve. null si no.")


class TennisBetPayload(BaseModel):
    """Datos extraídos por el LLM desde un boleto de tenis."""

    sport: Literal["tennis"] = "tennis"
    casa_apuestas: str = Field(description="Bookmaker visible. 'desconocida' si no se ve.")
    legs: list[TennisLeg] = Field(default_factory=list)
    cuota_total: float = Field(description="Cuota combinada final (>=1.0).")
    stake_indicado: float | None = Field(default=None, description="Stake en unidades si el tipster lo declara. null si no.")
    es_pick: bool = Field(description="true si la imagen es un boleto real. false si es ruido.")


# Alias de compatibilidad con código anterior
BetPayload = TennisBetPayload


# ---------------------------------------------------------------------------
# FÚTBOL
# ---------------------------------------------------------------------------

FootballMercado = Literal[
    "1x2",                      # resultado final: seleccion = "1", "X" o "2"
    "doble_oportunidad",        # seleccion = "1X", "X2" o "12"
    "ambos_marcan",             # seleccion = "si" | "no"
    "over_under_goles",         # total goles del partido, linea = valor
    "over_under_goles_primera", # total goles primer tiempo, linea = valor
    "handicap_asiatico",        # linea = valor (ej: -0.5, +1.5), seleccion = equipo apostado
    "handicap_europeo",         # linea = valor entero (ej: +1, -2), seleccion = equipo o "empate"
    "marcador_exacto",          # seleccion = "1-0", "2-1", etc.
    "resultado_descanso_final", # seleccion = "1/1", "X/2", etc.
    "goles_equipo_ou",          # goles de un equipo concreto, linea = valor
    "primera_mitad_1x2",        # resultado al descanso
    "tarjetas_ou",              # total tarjetas, linea = valor
    "corners_ou",               # total corners, linea = valor
]


class FootballLeg(BaseModel):
    """Una pierna de fútbol."""

    equipo_local: str = Field(description="Nombre del equipo local tal como aparece.")
    equipo_visitante: str = Field(description="Nombre del equipo visitante tal como aparece.")
    competicion: str | None = Field(default=None, description="Liga/copa si es visible. null si no.")
    fecha_evento: datetime | None = Field(
        default=None,
        description="Fecha del partido en formato YYYY-MM-DD si es visible. null si no aparece.",
    )
    mercado: FootballMercado
    seleccion: str = Field(
        description=(
            "Para 1x2: '1', 'X' o '2'. Para doble_oportunidad: '1X', 'X2' o '12'. "
            "Para ambos_marcan: 'si' o 'no'. Para over/under: 'over' o 'under'. "
            "Para handicap: nombre del equipo apostado. Para marcador_exacto: '1-0', '2-1', etc. "
            "Para resultado_descanso_final: 'X/1', '1/2', etc. Para primera_mitad_1x2: '1', 'X' o '2'."
        ),
    )
    linea: float | None = Field(
        default=None,
        description=(
            "Línea numérica para over/under y handicap. "
            "null para 1x2, doble_oportunidad, ambos_marcan, marcador_exacto y resultado_descanso_final."
        ),
    )
    cuota_individual: float | None = Field(default=None, description="Cuota individual si se ve. null si no.")


class FootballBetPayload(BaseModel):
    """Datos extraídos por el LLM desde un boleto de fútbol."""

    sport: Literal["football"] = "football"
    casa_apuestas: str = Field(description="Bookmaker visible. 'desconocida' si no se ve.")
    legs: list[FootballLeg] = Field(default_factory=list)
    cuota_total: float = Field(description="Cuota combinada final (>=1.0).")
    stake_indicado: float | None = Field(default=None, description="Stake en unidades si el tipster lo declara. null si no.")
    es_pick: bool = Field(description="true si la imagen es un boleto real. false si es ruido.")


# ---------------------------------------------------------------------------
# DARDOS
# ---------------------------------------------------------------------------

DartsMercado = Literal[
    "moneyline",        # ganador del partido
    "handicap_legs",    # hándicap en legs, linea = valor (ej: -1.5, +2.5)
    "over_under_legs",  # total de legs jugadas, linea = valor
    "set_betting",      # marcador exacto en sets, seleccion = "3-0", "3-1", "3-2", etc.
    "180s_match",       # total de 180s en el partido, linea = valor
    "checkout_mayor",   # checkout más alto del partido (over/under), linea = valor
    "primera_pierna",   # ganador de la primera leg
]


class DartsLeg(BaseModel):
    """Una pierna de dardos."""

    jugador_1: str = Field(description="Primer jugador tal como aparece en el boleto.")
    jugador_2: str = Field(description="Segundo jugador tal como aparece en el boleto.")
    competicion: str | None = Field(
        default=None,
        description="Competición si es visible (PDC, Premier League Darts, World Championship…). null si no.",
    )
    fecha_evento: datetime | None = Field(
        default=None,
        description="Fecha del partido en formato YYYY-MM-DD si es visible. null si no aparece.",
    )
    mercado: DartsMercado
    seleccion: str = Field(
        description=(
            "Para moneyline/primera_pierna: nombre del jugador apostado. "
            "Para over_under_legs/180s_match/checkout_mayor: 'over' o 'under'. "
            "Para handicap_legs: nombre del jugador apostado (la línea va en `linea`). "
            "Para set_betting: marcador exacto, ej: '3-1', '3-2', '2-3'."
        ),
    )
    linea: float | None = Field(
        default=None,
        description=(
            "Línea numérica: handicap (ej: -1.5), totales de legs (ej: 5.5), "
            "180s (ej: 6.5), checkout (ej: 100.5). null para moneyline, primera_pierna y set_betting."
        ),
    )
    cuota_individual: float | None = Field(default=None, description="Cuota individual si se ve. null si no.")


class DartsBetPayload(BaseModel):
    """Datos extraídos por el LLM desde un boleto de dardos."""

    sport: Literal["darts"] = "darts"
    casa_apuestas: str = Field(description="Bookmaker visible. 'desconocida' si no se ve.")
    legs: list[DartsLeg] = Field(default_factory=list)
    cuota_total: float = Field(description="Cuota combinada final (>=1.0).")
    stake_indicado: float | None = Field(default=None, description="Stake en unidades si el tipster lo declara. null si no.")
    es_pick: bool = Field(description="true si la imagen es un boleto real. false si es ruido.")


# ---------------------------------------------------------------------------
# BALONCESTO
# ---------------------------------------------------------------------------

BasketballMercado = Literal[
    "moneyline",                # ganador del partido (NBA no tiene empate)
    "handicap_puntos",          # spread / hándicap, ej: -5.5, +7.5
    "over_under_puntos",        # total puntos del partido
    "over_under_puntos_equipo", # total puntos de UN equipo (team total). seleccion = equipo
    "over_under_mitad",         # total puntos de una mitad concreta (linea = valor)
    "over_under_cuarto",        # total puntos de un cuarto concreto
    "ganador_mitad",            # ganador del 1er/2do tiempo (seleccion = equipo)
    "ganador_cuarto",           # ganador de un cuarto concreto
    "handicap_mitad",           # hándicap de una mitad concreta
    "puntos_jugador",           # over/under puntos de un jugador (player prop)
    "rebotes_jugador",          # over/under rebotes de un jugador
    "asistencias_jugador",      # over/under asistencias de un jugador
    "triples_jugador",          # over/under triples anotados por un jugador
    "asistencias_rebotes_jugador",         # A+R combinados de un jugador
    "puntos_rebotes_jugador",              # P+R combinados de un jugador
    "puntos_asistencias_jugador",          # P+A combinados de un jugador
    "puntos_rebotes_asistencias_jugador",  # P+R+A (PRA) combinados de un jugador
    "doble_doble_jugador",      # si un jugador hace doble-doble (seleccion = "si"/"no")
    "triple_doble_jugador",     # si un jugador hace triple-doble
    "race_to_puntos",           # primer equipo en llegar a X puntos (linea = X)
]


BasketballPeriodo = Literal["Q1", "Q2", "Q3", "Q4", "H1", "H2", "OT", "full"]


class BasketballLeg(BaseModel):
    """Una pierna de baloncesto."""

    equipo_local: str = Field(description="Nombre del equipo local tal como aparece.")
    equipo_visitante: str = Field(description="Nombre del equipo visitante tal como aparece.")
    competicion: str | None = Field(
        default=None,
        description="NBA, ACB, Euroliga, Eurocup, NCAA, etc. null si no se ve.",
    )
    fecha_evento: datetime | None = Field(
        default=None,
        description="Fecha del partido en formato YYYY-MM-DD si es visible. null si no aparece.",
    )
    mercado: BasketballMercado
    seleccion: str = Field(
        description=(
            "Para moneyline/ganador_mitad/ganador_cuarto/race_to_puntos: nombre del equipo apostado. "
            "Para handicap_*: nombre del equipo apostado (la línea va en `linea`). "
            "Para over_under_puntos/mitad/cuarto: 'over' o 'under'. "
            "Para over_under_puntos_equipo: nombre del equipo (el over/under va en `over_under`). "
            "Para puntos/rebotes/asistencias/triples_jugador y combos "
            "(asistencias_rebotes / puntos_rebotes / puntos_asistencias / puntos_rebotes_asistencias): "
            "nombre del jugador. "
            "Para doble_doble/triple_doble_jugador: nombre del jugador."
        ),
    )
    linea: float | None = Field(
        default=None,
        description=(
            "Línea numérica: handicap (-5.5, +7.5), totales del partido (210.5), "
            "team total (103.5), puntos/rebotes/asistencias/triples y combos (24.5, 12.5, 18.5, 35.5), "
            "race_to (20). null para moneyline, ganador_mitad/cuarto, "
            "doble_doble y triple_doble. "
            "Para mercados expresados como 'X+' (ej: '10+ asistencias') normaliza a X-0.5 con over_under='over'."
        ),
    )
    over_under: Literal["over", "under"] | None = Field(
        default=None,
        description=(
            "Para mercados de jugador (puntos/rebotes/asistencias/triples y sus combos), "
            "over_under_puntos_equipo y doble_doble/triple_doble: 'over' o 'under' "
            "(en doble/triple_doble equivale a 'si'/'no'). null para mercados donde "
            "basta seleccion + linea."
        ),
    )
    periodo: BasketballPeriodo | None = Field(
        default=None,
        description=(
            "Periodo del partido al que se refiere la apuesta cuando es distinto del partido completo: "
            "'Q1'/'Q2'/'Q3'/'Q4' (cuartos), 'H1'/'H2' (mitades), 'OT' (prórroga), 'full' (partido completo). "
            "Útil sobre todo en player props por cuarto/mitad (ej: 'Paolo Banchero 5+ puntos 1º cuarto' → 'Q1'). "
            "null cuando la apuesta se refiere al partido completo de forma implícita."
        ),
    )
    prorroga_incluida: bool | None = Field(
        default=None,
        description=(
            "true si el boleto indica explícitamente 'Prórroga incluida' / 'OT included'. "
            "false si indica 'Sin prórroga' / 'Regular time only'. null si no se menciona."
        ),
    )
    cuota_individual: float | None = Field(default=None, description="Cuota individual si se ve. null si no.")


class BasketballBetPayload(BaseModel):
    """Datos extraídos por el LLM desde un boleto de baloncesto."""

    sport: Literal["basketball"] = "basketball"
    casa_apuestas: str = Field(description="Bookmaker visible. 'desconocida' si no se ve.")
    legs: list[BasketballLeg] = Field(default_factory=list)
    cuota_total: float = Field(description="Cuota combinada final (>=1.0).")
    stake_indicado: float | None = Field(default=None, description="Stake en unidades si el tipster lo declara. null si no.")
    es_pick: bool = Field(description="true si la imagen es un boleto real. false si es ruido.")


# ---------------------------------------------------------------------------
# Unión discriminada
# ---------------------------------------------------------------------------

AnyBetPayload = Annotated[
    TennisBetPayload | FootballBetPayload | DartsBetPayload | BasketballBetPayload,
    Field(discriminator="sport"),
]


# ---------------------------------------------------------------------------
# Documento persistido en MongoDB
# ---------------------------------------------------------------------------

class PickDocument(BaseModel):
    """Documento persistido en MongoDB."""

    tipster: str
    message_id: int
    date_utc: datetime
    phash: str
    phash_bits: int = 64
    text_raw: str
    payload: AnyBetPayload
    resolution: PickResolution | None = None
    profit_units: float | None = None  # None si no_verificable

    @model_validator(mode="before")
    @classmethod
    def _backfill_sport(cls, values: dict) -> dict:
        """Compatibilidad hacia atrás: picks previos sin campo `sport` son de tenis."""
        payload = values.get("payload")
        if isinstance(payload, dict) and "sport" not in payload:
            values["payload"] = {**payload, "sport": "tennis"}
        return values
