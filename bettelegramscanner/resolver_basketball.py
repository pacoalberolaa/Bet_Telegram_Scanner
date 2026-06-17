"""Resolver de picks de baloncesto: orquesta múltiples fuentes en cascada.

Fuentes (en orden de prueba para cada partido):
  1. ESPN unofficial scoreboard — NBA, WNBA, NCAAM, NCAAW. JSON, gratis, histórico ilimitado.
  2. Basketball-Reference — NBA + WNBA. Scraping HTML, totales por partido (no por cuarto).
  3. API-Sports v1.basketball — fallback multi-liga (Euroliga/ACB/BSL/LNB/LKL),
     pero free tier solo permite ±1 día desde hoy.

La primera fuente que devuelva match con score fuzzy suficiente gana. Si ninguna
encuentra el partido, la pierna queda `no_verificable` con el motivo agregado de
todas las fuentes intentadas, para que el usuario pueda revisarlo manualmente
en el Excel.

Soporta mercados de equipo: moneyline, handicap (full/mitad/cuarto), totales
(full/mitad/cuarto), team total, ganador mitad/cuarto. Mercados de jugador y
race_to_puntos siguen siendo no_verificable (no hay box score por jugador en
las fuentes gratis sin requests adicionales caras).
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Iterable

from rapidfuzz import fuzz

from .api_basketball import ApiBasketGame, ApiBasketballClient
from .models import BasketballBetPayload, BasketballLeg, LegResolution, PickResolution
from .source_bref import BrefBasketClient
from .source_espn_basket import EspnBasketClient, EspnPlayerStat

log = logging.getLogger(__name__)

# Estados según schema interno (ApiBasketGame.status)
_NOT_PLAYED = {"NS", "PST", "CANC", "ABD", "AWD"}
_LIVE = {"Q1", "Q2", "Q3", "Q4", "OT", "BT", "HT"}
_FINISHED = {"FT", "AOT"}

_PLAYER_MARKETS = {
    "puntos_jugador", "rebotes_jugador", "asistencias_jugador", "triples_jugador",
    "asistencias_rebotes_jugador", "puntos_rebotes_jugador",
    "puntos_asistencias_jugador", "puntos_rebotes_asistencias_jugador",
    "doble_doble_jugador", "triple_doble_jugador",
}

# Mercados que requieren scores por cuarto/mitad. BR (índice) no los provee.
_NEEDS_QUARTER_DATA = {
    "over_under_mitad", "over_under_cuarto",
    "ganador_mitad", "ganador_cuarto",
    "handicap_mitad",
}


# ---------------------------------------------------------------------------
# Cliente orquestador
# ---------------------------------------------------------------------------

class BasketballResultsClient:
    """Wrapper que prueba ESPN → BR → API-Sports en cascada."""

    def __init__(self, store) -> None:
        self.espn = EspnBasketClient(store)
        self.bref = BrefBasketClient(store)
        self.api_sports = ApiBasketballClient(store)
        # Para diagnóstico: cuántas piernas resolvió cada fuente
        self.hits: dict[str, int] = {"espn": 0, "bref": 0, "api_sports": 0, "miss": 0}

    async def aclose(self) -> None:
        await self.espn.aclose()
        await self.bref.aclose()
        await self.api_sports.aclose()

    async def find_game(
        self,
        team_a: str,
        team_b: str,
        around: date,
        needs_quarters: bool = False,
    ) -> tuple[ApiBasketGame | None, str | None]:
        """Devuelve (game, source_name). source_name=None si no se encontró."""
        # 1. ESPN
        g = await self.espn.find_game(team_a, team_b, around)
        if g is not None:
            self.hits["espn"] += 1
            return g, "espn"
        # 2. BR — saltar si el mercado necesita datos por cuarto (BR índice no los tiene)
        if not needs_quarters:
            g = await self.bref.find_game(team_a, team_b, around)
            if g is not None:
                self.hits["bref"] += 1
                return g, "bref"
        # 3. API-Sports (rico en datos por cuarto pero ±1d en free)
        g = await self.api_sports.find_game(team_a, team_b, around)
        if g is not None:
            self.hits["api_sports"] += 1
            return g, "api_sports"
        self.hits["miss"] += 1
        return None, None

    async def fetch_players(
        self, game: ApiBasketGame, source: str,
    ) -> list[EspnPlayerStat] | None:
        """Devuelve box score por jugador. Solo ESPN lo expone gratis."""
        if source != "espn":
            return None
        # game.league viene como "ESPN/nba", "ESPN/wnba", ...
        if not game.league.startswith("ESPN/"):
            return None
        league_key = game.league.split("/", 1)[1]
        return await self.espn.fetch_boxscore(game.game_id, league_key)

    def miss_motivo(self, leg: BasketballLeg, around: date) -> str:
        """Compone un motivo informativo cuando ninguna fuente encontró el partido."""
        reasons: list[str] = []
        if self.api_sports.unavailable_reason():
            reasons.append(f"API-Sports: {self.api_sports.unavailable_reason()}")
        else:
            reasons.append("API-Sports: sin match")
        reasons.append("ESPN: sin match (cubre NBA/WNBA/NCAA)")
        reasons.append("Basketball-Reference: sin match (cubre NBA/WNBA)")
        return (
            f"partido no encontrado en fuentes gratis: {leg.equipo_local} vs "
            f"{leg.equipo_visitante} ({around}). " + " | ".join(reasons)
        )


# ---------------------------------------------------------------------------
# Helpers de selección y orientación (idéntico a la versión anterior)
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _team_score(a: str, b: str) -> int:
    return int(fuzz.token_set_ratio(_normalize(a), _normalize(b)))


def _orient_home(leg: BasketballLeg, game: ApiBasketGame) -> bool:
    return _team_score(leg.equipo_local, game.team_home) >= _team_score(leg.equipo_local, game.team_away)


def _sel_matches_team(sel: str, home: str, away: str, threshold: int = 70) -> int:
    s_home = _team_score(sel, home)
    s_away = _team_score(sel, away)
    if max(s_home, s_away) < threshold:
        return 0
    return 1 if s_home >= s_away else 2


def _marker(game: ApiBasketGame) -> str:
    if game.home_total is None or game.away_total is None:
        return f"{game.team_home} ? - ? {game.team_away}"
    return f"{game.team_home} {game.home_total}-{game.away_total} {game.team_away}"


def _include_ot(leg: BasketballLeg) -> bool:
    if leg.prorroga_incluida is False:
        return False
    return True


def _sum_period(parts: list[int | None]) -> int | None:
    if any(p is None for p in parts):
        return None
    return sum(parts)


def _team_pts_for_period(
    game: ApiBasketGame, periodo: str | None, include_ot: bool,
) -> tuple[int | None, int | None]:
    p = periodo or "full"
    if p == "full":
        if include_ot:
            return game.home_total, game.away_total
        return _sum_period(game.home_q), _sum_period(game.away_q)
    if p == "Q1": return game.home_q[0], game.away_q[0]
    if p == "Q2": return game.home_q[1], game.away_q[1]
    if p == "Q3": return game.home_q[2], game.away_q[2]
    if p == "Q4": return game.home_q[3], game.away_q[3]
    if p == "H1":
        return _sum_period(game.home_q[:2]), _sum_period(game.away_q[:2])
    if p == "H2":
        return _sum_period(game.home_q[2:4]), _sum_period(game.away_q[2:4])
    if p == "OT":
        return game.home_ot, game.away_ot
    return None, None


def _player_stat_value(p: EspnPlayerStat, mercado: str) -> int | None:
    """Devuelve el valor numérico apropiado del jugador para el mercado, o None
    si falta algún componente."""
    if mercado == "puntos_jugador":
        return p.points
    if mercado == "rebotes_jugador":
        return p.rebounds
    if mercado == "asistencias_jugador":
        return p.assists
    if mercado == "triples_jugador":
        return p.threes_made
    if mercado == "asistencias_rebotes_jugador":
        if p.assists is None or p.rebounds is None: return None
        return p.assists + p.rebounds
    if mercado == "puntos_rebotes_jugador":
        if p.points is None or p.rebounds is None: return None
        return p.points + p.rebounds
    if mercado == "puntos_asistencias_jugador":
        if p.points is None or p.assists is None: return None
        return p.points + p.assists
    if mercado == "puntos_rebotes_asistencias_jugador":
        if p.points is None or p.rebounds is None or p.assists is None: return None
        return p.points + p.rebounds + p.assists
    return None


def _find_player(players: list[EspnPlayerStat], name: str,
                 home: str, away: str, threshold: int = 75) -> EspnPlayerStat | None:
    """Fuzzy match del jugador apostado contra el roster del partido."""
    name_n = _normalize(name)
    if not name_n:
        return None
    best: tuple[int, EspnPlayerStat] | None = None
    for p in players:
        score = int(fuzz.token_set_ratio(name_n, _normalize(p.name)))
        # Bonus si el equipo del jugador casa con local/visitante
        if _team_score(p.team, home) > 80 or _team_score(p.team, away) > 80:
            score += 3
        if score >= threshold and (best is None or score > best[0]):
            best = (score, p)
    return best[1] if best else None


def _resolve_player_leg(
    leg: BasketballLeg, players: list[EspnPlayerStat],
    home: str, away: str, marker: str,
) -> LegResolution:
    mercado = leg.mercado
    player = _find_player(players, leg.seleccion, home, away)
    if player is None:
        return LegResolution(
            status="no_verificable",
            motivo=f"jugador '{leg.seleccion}' no encontrado en boxscore",
            marcador=marker,
        )

    # doble_doble / triple_doble: si/no según over_under
    if mercado in ("doble_doble_jugador", "triple_doble_jugador"):
        needed = 2 if mercado == "doble_doble_jugador" else 3
        # Si no tiene MIN o jugó 0, contamos como "no ocurrió"
        achieved = player.double_count() >= needed
        sel = (leg.over_under or "").lower()
        if sel not in ("over", "under"):
            return LegResolution(
                status="no_verificable",
                motivo=f"{mercado}: over_under ausente o inválido",
                marcador=marker,
            )
        bet_yes = (sel == "over")
        won = (bet_yes and achieved) or (not bet_yes and not achieved)
        return LegResolution(
            status="ganada" if won else "perdida",
            motivo=f"{player.name}: pts={player.points} reb={player.rebounds} ast={player.assists}",
            marcador=marker,
        )

    # Player props over/under
    if leg.linea is None:
        return LegResolution(status="no_verificable", motivo="línea ausente", marcador=marker)
    sel = (leg.over_under or "").lower()
    if sel not in ("over", "under"):
        return LegResolution(
            status="no_verificable",
            motivo=f"{mercado}: over_under ausente o inválido",
            marcador=marker,
        )

    value = _player_stat_value(player, mercado)
    if value is None:
        return LegResolution(
            status="no_verificable",
            motivo=f"{player.name}: stat para {mercado} no disponible",
            marcador=marker,
        )

    if abs(value - leg.linea) < 1e-9:
        return LegResolution(
            status="void", motivo=f"push ({player.name} {value})", marcador=marker,
        )
    is_over = value > leg.linea
    won = (sel == "over" and is_over) or (sel == "under" and not is_over)
    return LegResolution(
        status="ganada" if won else "perdida",
        motivo=f"{player.name}: {value} vs {leg.linea} ({sel})",
        marcador=marker,
    )


def resolve_leg(
    leg: BasketballLeg, game: ApiBasketGame, source: str,
    players: list[EspnPlayerStat] | None = None,
) -> LegResolution:
    marker = _marker(game)

    if game.status in _NOT_PLAYED:
        return LegResolution(status="void", motivo=f"partido no jugado ({game.status}) [{source}]", marcador=marker)
    if game.status in _LIVE:
        return LegResolution(status="no_verificable", motivo=f"partido en curso ({game.status}) [{source}]", marcador=marker)
    if game.status not in _FINISHED:
        return LegResolution(status="no_verificable", motivo=f"estado desconocido ({game.status}) [{source}]", marcador=marker)

    if game.home_total is None or game.away_total is None:
        return LegResolution(status="no_verificable", motivo=f"sin marcador final [{source}]", marcador=marker)

    include_ot = _include_ot(leg)
    mercado = leg.mercado

    if mercado in _PLAYER_MARKETS:
        if players is None:
            return LegResolution(
                status="no_verificable",
                motivo=f"mercado de jugador '{mercado}' requiere box por jugador (fuente {source} no lo expone)",
                marcador=marker,
            )
        return _resolve_player_leg(leg, players, game.team_home, game.team_away, marker)

    if mercado == "race_to_puntos":
        return LegResolution(
            status="no_verificable",
            motivo="race_to_puntos requiere play-by-play (no disponible)",
            marcador=marker,
        )

    # moneyline
    if mercado == "moneyline":
        which = _sel_matches_team(leg.seleccion, game.team_home, game.team_away)
        if which == 0:
            return LegResolution(status="no_verificable", motivo="selección no identifica equipo", marcador=marker)
        h, a = _team_pts_for_period(game, "full", include_ot)
        if h is None or a is None:
            return LegResolution(status="no_verificable", motivo="sin marcador", marcador=marker)
        if h == a:
            return LegResolution(status="void", motivo="empate", marcador=marker)
        home_won = h > a
        bet_home = (which == 1)
        won = (bet_home and home_won) or (not bet_home and not home_won)
        return LegResolution(status="ganada" if won else "perdida", marcador=marker)

    # handicap
    if mercado in ("handicap_puntos", "handicap_mitad"):
        if leg.linea is None:
            return LegResolution(status="no_verificable", motivo="línea ausente", marcador=marker)
        which = _sel_matches_team(leg.seleccion, game.team_home, game.team_away)
        if which == 0:
            return LegResolution(status="no_verificable", motivo="selección no identifica equipo", marcador=marker)
        period = leg.periodo or ("full" if mercado == "handicap_puntos" else None)
        if period is None:
            return LegResolution(status="no_verificable", motivo="handicap_mitad sin periodo", marcador=marker)
        h, a = _team_pts_for_period(game, period, include_ot)
        if h is None or a is None:
            return LegResolution(status="no_verificable", motivo=f"sin datos para periodo {period} [{source}]", marcador=marker)
        bet_home = (which == 1)
        diff = (h - a) if bet_home else (a - h)
        adj = diff + leg.linea
        if abs(adj) < 1e-9:
            return LegResolution(status="void", motivo="push", marcador=marker)
        return LegResolution(status="ganada" if adj > 0 else "perdida", marcador=marker)

    # over/under totales
    if mercado in ("over_under_puntos", "over_under_mitad", "over_under_cuarto"):
        if leg.linea is None:
            return LegResolution(status="no_verificable", motivo="línea ausente", marcador=marker)
        sel = leg.seleccion.lower().strip()
        if sel not in ("over", "under"):
            return LegResolution(status="no_verificable", motivo="selección no es over/under", marcador=marker)
        period = leg.periodo or ("full" if mercado == "over_under_puntos" else None)
        if period is None:
            return LegResolution(status="no_verificable", motivo=f"{mercado} sin periodo", marcador=marker)
        h, a = _team_pts_for_period(game, period, include_ot)
        if h is None or a is None:
            return LegResolution(status="no_verificable", motivo=f"sin datos para periodo {period} [{source}]", marcador=marker)
        total = h + a
        if abs(total - leg.linea) < 1e-9:
            return LegResolution(status="void", motivo="push", marcador=marker)
        is_over = total > leg.linea
        won = (sel == "over" and is_over) or (sel == "under" and not is_over)
        return LegResolution(status="ganada" if won else "perdida", marcador=marker)

    # team total
    if mercado == "over_under_puntos_equipo":
        if leg.linea is None:
            return LegResolution(status="no_verificable", motivo="línea ausente", marcador=marker)
        if leg.over_under is None:
            return LegResolution(status="no_verificable", motivo="over_under ausente", marcador=marker)
        which = _sel_matches_team(leg.seleccion, game.team_home, game.team_away)
        if which == 0:
            return LegResolution(status="no_verificable", motivo="selección no identifica equipo", marcador=marker)
        period = leg.periodo or "full"
        h, a = _team_pts_for_period(game, period, include_ot)
        team_pts = h if which == 1 else a
        if team_pts is None:
            return LegResolution(status="no_verificable", motivo=f"sin datos para periodo {period} [{source}]", marcador=marker)
        if abs(team_pts - leg.linea) < 1e-9:
            return LegResolution(status="void", motivo="push", marcador=marker)
        is_over = team_pts > leg.linea
        sel = leg.over_under
        won = (sel == "over" and is_over) or (sel == "under" and not is_over)
        return LegResolution(status="ganada" if won else "perdida", marcador=marker)

    # ganador de mitad / cuarto
    if mercado in ("ganador_mitad", "ganador_cuarto"):
        which = _sel_matches_team(leg.seleccion, game.team_home, game.team_away)
        if which == 0:
            return LegResolution(status="no_verificable", motivo="selección no identifica equipo", marcador=marker)
        period = leg.periodo
        if period is None:
            return LegResolution(status="no_verificable", motivo=f"{mercado} sin periodo", marcador=marker)
        h, a = _team_pts_for_period(game, period, include_ot=False)
        if h is None or a is None:
            return LegResolution(status="no_verificable", motivo=f"sin datos para periodo {period} [{source}]", marcador=marker)
        if h == a:
            return LegResolution(status="void", motivo=f"{period} empatado", marcador=marker)
        home_won = h > a
        bet_home = (which == 1)
        won = (bet_home and home_won) or (not bet_home and not home_won)
        return LegResolution(status="ganada" if won else "perdida", marcador=marker)

    return LegResolution(status="no_verificable", motivo=f"mercado no soportado: {mercado}", marcador=marker)


async def resolve_basketball_pick(
    payload: BasketballBetPayload,
    event_date: date,
    api: BasketballResultsClient,
) -> PickResolution:
    leg_resolutions: list[LegResolution] = []
    for leg in payload.legs:
        # La IA alucina años cuando el boleto solo indica "2/5". Si la fecha que extrajo
        # se aleja > 7 días de la del mensaje, ignorarla y usar event_date.
        ev_date = event_date
        if leg.fecha_evento is not None:
            candidate = leg.fecha_evento.date()
            if abs((candidate - event_date).days) <= 7:
                ev_date = candidate
        needs_quarters = leg.mercado in _NEEDS_QUARTER_DATA
        game, source = await api.find_game(
            leg.equipo_local, leg.equipo_visitante, ev_date,
            needs_quarters=needs_quarters,
        )
        if game is None:
            leg_resolutions.append(LegResolution(
                status="no_verificable",
                motivo=api.miss_motivo(leg, ev_date),
            ))
            continue
        players: list[EspnPlayerStat] | None = None
        if leg.mercado in _PLAYER_MARKETS:
            players = await api.fetch_players(game, source or "")
        leg_resolutions.append(resolve_leg(leg, game, source or "?", players=players))

    if not leg_resolutions:
        return PickResolution(status="no_verificable", motivo="boleto sin piernas", legs=[])
    if any(r.status == "no_verificable" for r in leg_resolutions):
        return PickResolution(
            status="no_verificable",
            motivo="al menos una pierna no verificable",
            legs=leg_resolutions,
        )
    if any(r.status == "perdida" for r in leg_resolutions):
        return PickResolution(status="perdida", legs=leg_resolutions)
    if all(r.status == "void" for r in leg_resolutions):
        return PickResolution(status="void", motivo="todas las piernas void", legs=leg_resolutions)
    return PickResolution(status="ganada", legs=leg_resolutions)
