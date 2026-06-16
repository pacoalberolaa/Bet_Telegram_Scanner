"""Resolver de picks de tenis contra Tennis Explorer (scraping respetuoso).

OJO: Tennis Explorer no tiene API pública. Esto scrapea su HTML público con
rate-limit conservador y cache en MongoDB. Si TE cambia el HTML los selectores
de `_parse_results` son los puntos a tocar.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from .config import TE_DAY_WINDOW, TE_FUZZY_THRESHOLD, TE_RATE_SECONDS, TE_USER_AGENT
from .models import LegResolution, PickResolution, TennisLeg

log = logging.getLogger(__name__)

BASE_URL = "https://www.tennisexplorer.com"
RESULT_TYPES = ("atp-single", "wta-single", "ch-single", "atp-double", "wta-double")


@dataclass
class TEMatch:
    tournament: str
    player_1: str
    player_2: str
    sets: list[tuple[int, int]] = field(default_factory=list)  # [(p1_games, p2_games), ...]
    status: str = "finished"  # finished | retired | walkover | scheduled | unknown
    match_date: date | None = None


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class TennisExplorerClient:
    def __init__(self, store, rate_seconds: float = TE_RATE_SECONDS) -> None:
        self._store = store
        self._rate = rate_seconds
        self._client = httpx.AsyncClient(
            headers={"User-Agent": TE_USER_AGENT},
            timeout=20.0,
            follow_redirects=True,
        )
        self._memory: dict[date, list[TEMatch]] = {}
        # Lock global de fetch: garantiza rate-limit GLOBAL aunque haya N workers.
        self._fetch_lock = asyncio.Lock()
        # Lock por fecha: evita que 2 workers scrapeen el mismo día a la vez.
        self._date_locks: dict[date, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _date_lock(self, d: date) -> asyncio.Lock:
        async with self._locks_lock:
            lock = self._date_locks.get(d)
            if lock is None:
                lock = asyncio.Lock()
                self._date_locks[d] = lock
            return lock

    async def matches_on(self, d: date) -> list[TEMatch]:
        if d in self._memory:
            return self._memory[d]

        date_lock = await self._date_lock(d)
        async with date_lock:
            if d in self._memory:
                return self._memory[d]
            cached = await self._store.get_te_matches(d)
            if cached is not None:
                matches = [_match_from_dict(m) for m in cached]
                self._memory[d] = matches
                return matches

            matches: list[TEMatch] = []
            for rtype in RESULT_TYPES:
                url = f"{BASE_URL}/results/?type={rtype}&year={d.year}&month={d.month:02d}&day={d.day:02d}"
                try:
                    async with self._fetch_lock:
                        await asyncio.sleep(self._rate)
                        r = await self._client.get(url)
                    if r.status_code != 200:
                        log.warning("TE %s -> HTTP %s", url, r.status_code)
                        continue
                    matches.extend(_parse_results(r.text, d))
                except Exception:
                    log.exception("Fallo scrapeando TE %s", url)

            await self._store.save_te_matches(d, [asdict(m) for m in matches])
            self._memory[d] = matches
            return matches

    async def find_match(
        self,
        player_1: str,
        player_2: str,
        around: date,
        day_window: int = TE_DAY_WINDOW,
    ) -> TEMatch | None:
        best: tuple[int, TEMatch] | None = None
        for offset in range(-day_window, day_window + 1):
            d = around + timedelta(days=offset)
            for m in await self.matches_on(d):
                score = _pair_score(player_1, player_2, m.player_1, m.player_2)
                if score >= TE_FUZZY_THRESHOLD and (best is None or score > best[0]):
                    best = (score, m)
        return best[1] if best else None


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

_SET_RE = re.compile(r"^(\d{1,2})(?:\D|$)")


def _parse_results(html: str, d: date) -> list[TEMatch]:
    soup = BeautifulSoup(html, "html.parser")
    matches: list[TEMatch] = []
    for table in soup.select("table.result"):
        current_tournament = "?"
        rows = table.find_all("tr", recursive=False) or table.find_all("tr")
        i = 0
        while i < len(rows):
            row = rows[i]
            head = row.select_one("td.head, td.t-tour, th.tour")
            if head and not row.select_one("td.t-name"):
                current_tournament = head.get_text(" ", strip=True)
                i += 1
                continue
            name_cell = row.select_one("td.t-name")
            if not name_cell:
                i += 1
                continue
            if i + 1 >= len(rows):
                break
            next_row = rows[i + 1]
            next_name = next_row.select_one("td.t-name")
            if not next_name:
                i += 1
                continue
            try:
                p1 = name_cell.get_text(" ", strip=True)
                p2 = next_name.get_text(" ", strip=True)
                sets, status = _extract_sets(row, next_row)
                matches.append(TEMatch(
                    tournament=current_tournament,
                    player_1=p1, player_2=p2,
                    sets=sets, status=status, match_date=d,
                ))
            except Exception:
                log.exception("Fallo parseando match row en TE")
            i += 2
    return matches


def _extract_sets(row_a, row_b) -> tuple[list[tuple[int, int]], str]:
    """Saca set scores. Estados típicos en TE: vacío/'-' = scheduled; 'ret.' = retired; 'w/o' = walkover."""
    raw_a = [c.get_text(" ", strip=True) for c in row_a.find_all("td")]
    raw_b = [c.get_text(" ", strip=True) for c in row_b.find_all("td")]
    text_blob = " ".join(raw_a + raw_b).lower()
    if "w/o" in text_blob or "walkover" in text_blob:
        return [], "walkover"
    if "ret." in text_blob or "retired" in text_blob:
        status_after = _pair_set_cells(raw_a, raw_b)
        return status_after, "retired"
    sets = _pair_set_cells(raw_a, raw_b)
    if not sets:
        return [], "scheduled"
    return sets, "finished"


def _pair_set_cells(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """Empareja columnas con números (sets). TE pone los sets en columnas consecutivas."""
    sets: list[tuple[int, int]] = []
    for ca, cb in zip(a, b):
        ma = _SET_RE.match(ca.strip())
        mb = _SET_RE.match(cb.strip())
        if ma and mb:
            ga, gb = int(ma.group(1)), int(mb.group(1))
            if 0 <= ga <= 30 and 0 <= gb <= 30 and (ga, gb) != (0, 0):
                sets.append((ga, gb))
    return sets


def _match_from_dict(d: dict) -> TEMatch:
    md = d.get("match_date")
    if isinstance(md, str):
        try:
            md = date.fromisoformat(md)
        except ValueError:
            md = None
    elif isinstance(md, datetime):
        md = md.date()
    return TEMatch(
        tournament=d.get("tournament", "?"),
        player_1=d.get("player_1", ""),
        player_2=d.get("player_2", ""),
        sets=[tuple(s) for s in d.get("sets", [])],
        status=d.get("status", "unknown"),
        match_date=md,
    )


# ---------------------------------------------------------------------------
# Fuzzy match jugadores
# ---------------------------------------------------------------------------

def _player_score(a: str, b: str) -> int:
    return int(fuzz.token_set_ratio(_normalize(a), _normalize(b)))


def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9 /]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _pair_score(bet_p1: str, bet_p2: str, te_p1: str, te_p2: str) -> int:
    s_direct = (_player_score(bet_p1, te_p1) + _player_score(bet_p2, te_p2)) // 2
    s_swap = (_player_score(bet_p1, te_p2) + _player_score(bet_p2, te_p1)) // 2
    return max(s_direct, s_swap)


# ---------------------------------------------------------------------------
# Resolución de mercados
# ---------------------------------------------------------------------------

def _orient(leg: TennisLeg, match: TEMatch) -> tuple[list[tuple[int, int]], bool] | None:
    """Devuelve (sets orientados a leg.jugador_1 primero, hubo swap?). None si no encajan los jugadores."""
    score_direct = _player_score(leg.jugador_1, match.player_1)
    score_swap = _player_score(leg.jugador_1, match.player_2)
    if max(score_direct, score_swap) < TE_FUZZY_THRESHOLD:
        return None
    if score_direct >= score_swap:
        return list(match.sets), False
    return [(b, a) for a, b in match.sets], True


def _format_marker(sets: list[tuple[int, int]]) -> str:
    return " ".join(f"{a}-{b}" for a, b in sets) if sets else ""


def _sel_matches_player(selection: str, player_1: str, player_2: str) -> int:
    """Devuelve 1 si la selección apuesta a player_1, 2 si a player_2, 0 si no claro."""
    s1 = _player_score(selection, player_1)
    s2 = _player_score(selection, player_2)
    if max(s1, s2) < TE_FUZZY_THRESHOLD:
        return 0
    return 1 if s1 >= s2 else 2


def resolve_leg(leg: TennisLeg, match: TEMatch) -> LegResolution:
    marker = _format_marker(match.sets)

    if match.status == "walkover":
        return LegResolution(status="void", motivo="walkover", marcador=marker)
    if match.status == "scheduled":
        return LegResolution(status="no_verificable", motivo="partido sin resultado")
    if match.status == "retired" and not match.sets:
        return LegResolution(status="void", motivo="retirada sin sets completados", marcador=marker)

    oriented = _orient(leg, match)
    if oriented is None:
        return LegResolution(status="no_verificable", motivo="jugadores no encajan", marcador=marker)
    sets, _ = oriented  # sets están orientados con leg.jugador_1 primero

    p1_games = sum(a for a, _ in sets)
    p2_games = sum(b for _, b in sets)
    p1_sets = sum(1 for a, b in sets if a > b)
    p2_sets = sum(1 for a, b in sets if b > a)

    # Para handicap/over_under en sets/games un retiro deja el resultado parcial:
    # política conservadora -> void si hubo retiro.
    if match.status == "retired" and leg.mercado != "moneyline":
        return LegResolution(status="void", motivo="retirada en mercado de games/sets", marcador=marker)

    if leg.mercado == "moneyline":
        if p1_sets == p2_sets:
            return LegResolution(status="no_verificable", motivo="sin ganador claro", marcador=marker)
        winner_is_p1 = p1_sets > p2_sets
        which = _sel_matches_player(leg.seleccion, leg.jugador_1, leg.jugador_2)
        if which == 0:
            return LegResolution(status="no_verificable", motivo="selección no identifica jugador", marcador=marker)
        bet_on_p1 = (which == 1)
        won = (bet_on_p1 and winner_is_p1) or (not bet_on_p1 and not winner_is_p1)
        if match.status == "retired":
            return LegResolution(status="ganada" if won else "perdida", motivo="resuelto con retiro", marcador=marker)
        return LegResolution(status="ganada" if won else "perdida", marcador=marker)

    if leg.mercado == "handicap_games":
        if leg.linea is None:
            return LegResolution(status="no_verificable", motivo="línea ausente", marcador=marker)
        which = _sel_matches_player(leg.seleccion, leg.jugador_1, leg.jugador_2)
        if which == 0:
            return LegResolution(status="no_verificable", motivo="selección no identifica jugador", marcador=marker)
        bet_p1 = (which == 1)
        diff = (p1_games - p2_games) if bet_p1 else (p2_games - p1_games)
        adj = diff + leg.linea
        if abs(adj) < 1e-9:
            return LegResolution(status="void", motivo="push", marcador=marker)
        return LegResolution(status="ganada" if adj > 0 else "perdida", marcador=marker)

    if leg.mercado == "handicap_sets":
        if leg.linea is None:
            return LegResolution(status="no_verificable", motivo="línea ausente", marcador=marker)
        which = _sel_matches_player(leg.seleccion, leg.jugador_1, leg.jugador_2)
        if which == 0:
            return LegResolution(status="no_verificable", motivo="selección no identifica jugador", marcador=marker)
        bet_p1 = (which == 1)
        diff = (p1_sets - p2_sets) if bet_p1 else (p2_sets - p1_sets)
        adj = diff + leg.linea
        if abs(adj) < 1e-9:
            return LegResolution(status="void", motivo="push", marcador=marker)
        return LegResolution(status="ganada" if adj > 0 else "perdida", marcador=marker)

    if leg.mercado == "over_under_games":
        if leg.linea is None:
            return LegResolution(status="no_verificable", motivo="línea ausente", marcador=marker)
        total = p1_games + p2_games
        sel = leg.seleccion.lower().strip()
        if sel not in ("over", "under"):
            return LegResolution(status="no_verificable", motivo="selección no es over/under", marcador=marker)
        if abs(total - leg.linea) < 1e-9:
            return LegResolution(status="void", motivo="push", marcador=marker)
        is_over = total > leg.linea
        won = (sel == "over" and is_over) or (sel == "under" and not is_over)
        return LegResolution(status="ganada" if won else "perdida", marcador=marker)

    if leg.mercado == "over_under_sets":
        if leg.linea is None:
            return LegResolution(status="no_verificable", motivo="línea ausente", marcador=marker)
        total = p1_sets + p2_sets
        sel = leg.seleccion.lower().strip()
        if sel not in ("over", "under"):
            return LegResolution(status="no_verificable", motivo="selección no es over/under", marcador=marker)
        if abs(total - leg.linea) < 1e-9:
            return LegResolution(status="void", motivo="push", marcador=marker)
        is_over = total > leg.linea
        won = (sel == "over" and is_over) or (sel == "under" and not is_over)
        return LegResolution(status="ganada" if won else "perdida", marcador=marker)

    if leg.mercado == "set_betting":
        which = _sel_matches_player(leg.seleccion, leg.jugador_1, leg.jugador_2)
        # set_betting puede expresarse como "Alcaraz 2-0" o "2-0". Probamos a parsear "X-Y" del final.
        m = re.search(r"(\d)\s*-\s*(\d)", leg.seleccion)
        if not m:
            return LegResolution(status="no_verificable", motivo="seleccion sin marcador", marcador=marker)
        sel_p1, sel_p2 = int(m.group(1)), int(m.group(2))
        # Si la selección menciona al jugador 2, invertir el marcador antes de comparar
        if which == 2:
            sel_p1, sel_p2 = sel_p2, sel_p1
        won = (p1_sets, p2_sets) == (sel_p1, sel_p2)
        return LegResolution(status="ganada" if won else "perdida", marcador=marker)

    return LegResolution(status="no_verificable", motivo=f"mercado no soportado: {leg.mercado}", marcador=marker)


# ---------------------------------------------------------------------------
# Resolución de boleto completo (parlay)
# ---------------------------------------------------------------------------

async def resolve_pick(
    legs: Iterable[TennisLeg],
    around: date,
    te: TennisExplorerClient,
) -> PickResolution:
    leg_resolutions: list[LegResolution] = []
    for leg in legs:
        event_date = (leg.fecha_evento.date() if leg.fecha_evento else around)
        match = await te.find_match(leg.jugador_1, leg.jugador_2, event_date)
        if match is None:
            leg_resolutions.append(LegResolution(
                status="no_verificable", motivo="partido no encontrado en TE",
            ))
            continue
        leg_resolutions.append(resolve_leg(leg, match))

    # Política: cualquier pierna no_verificable -> boleto entero no_verificable.
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
