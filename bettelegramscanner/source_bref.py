"""Fuente: Basketball-Reference.com (scraping HTML, gratis).

Cubre NBA (full history desde 1947) + WNBA. Página índice diaria:
    https://www.basketball-reference.com/boxscores/?month=M&day=D&year=Y
    https://www.basketball-reference.com/wnba/boxscores/?month=M&day=D&year=Y

Cada `div.game_summary` lista 1 partido: ambos equipos + score final.
NO incluye scores por cuarto en la página índice (eso requeriría una
request adicional por partido al detalle). Para MVP nos quedamos con
totales: cubre moneyline, handicap_puntos full, over_under_puntos full,
team total. Mercados por cuarto/mitad → no_verificable desde BR.

BR pide max ~20 req/min: usamos rate de 3.2s por request para mantenernos
muy holgados. Cacheamos por fecha en Mongo (colección bref_basket_games).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from .api_basketball import ApiBasketGame
from .config import API_BASKETBALL_DAY_WINDOW, API_BASKETBALL_FUZZY_THRESHOLD

log = logging.getLogger(__name__)

BREF_BASE = "https://www.basketball-reference.com"
BREF_RATE_SECONDS = 3.2  # < 20 req/min
BREF_USER_AGENT = "BetTelegramScannerBot/0.1 (research; respect robots.txt)"


def _parse_index_page(html: str, d: date, league_label: str) -> list[ApiBasketGame]:
    soup = BeautifulSoup(html, "html.parser")
    games: list[ApiBasketGame] = []
    for i, summary in enumerate(soup.select("div.game_summary")):
        try:
            rows = summary.select("table.teams tr")
            entries: list[tuple[str, int]] = []
            for row in rows:
                a = row.select_one("a")
                score_cell = row.select_one("td.right")
                if a is None or score_cell is None:
                    continue
                name = a.get_text(strip=True)
                score_text = score_cell.get_text(strip=True)
                try:
                    score = int(score_text)
                except ValueError:
                    continue
                entries.append((name, score))
            if len(entries) < 2:
                continue
            # En BR el orden de filas es: visitante primero, local segundo
            away_name, away_score = entries[0]
            home_name, home_score = entries[1]

            # game_id estable: fecha + abreviatura del enlace al detalle
            detail = summary.select_one('a[href*="/boxscores/"]')
            game_slug = detail["href"].rsplit("/", 1)[-1].replace(".html", "") if detail else f"{d.isoformat()}-{i}"
            game_id = hash(game_slug) & 0x7FFFFFFF

            games.append(ApiBasketGame(
                game_id=game_id,
                league=league_label,
                status="FT",  # la página índice solo lista partidos finalizados
                team_home=home_name,
                team_away=away_name,
                home_q=[None, None, None, None],
                away_q=[None, None, None, None],
                home_ot=None, away_ot=None,
                home_total=home_score,
                away_total=away_score,
                match_date=d,
            ))
        except Exception:
            log.exception("Fallo parseando game_summary BR (%s, idx %d)", d, i)
    return games


def _game_from_dict(d: dict[str, Any]) -> ApiBasketGame:
    md = d.get("match_date")
    if isinstance(md, str):
        try:
            md = date.fromisoformat(md)
        except ValueError:
            md = None
    return ApiBasketGame(
        game_id=int(d.get("game_id", 0)),
        league=d.get("league", ""),
        status=d.get("status", ""),
        team_home=d.get("team_home", ""),
        team_away=d.get("team_away", ""),
        home_q=list(d.get("home_q", [None] * 4)),
        away_q=list(d.get("away_q", [None] * 4)),
        home_ot=d.get("home_ot"),
        away_ot=d.get("away_ot"),
        home_total=d.get("home_total"),
        away_total=d.get("away_total"),
        match_date=md,
    )


class BrefBasketClient:
    def __init__(self, store, rate_seconds: float = BREF_RATE_SECONDS) -> None:
        self._store = store
        self._rate = rate_seconds
        self._client = httpx.AsyncClient(
            base_url=BREF_BASE,
            headers={"User-Agent": BREF_USER_AGENT},
            timeout=20.0,
            follow_redirects=True,
        )
        self._memory: dict[date, list[ApiBasketGame]] = {}
        self._fetch_lock = asyncio.Lock()
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

    async def games_on(self, d: date) -> list[ApiBasketGame]:
        if d in self._memory:
            return self._memory[d]
        date_lock = await self._date_lock(d)
        async with date_lock:
            if d in self._memory:
                return self._memory[d]
            cached = await self._store.get_bref_basket_games(d)
            if cached is not None:
                games = [_game_from_dict(g) for g in cached]
                self._memory[d] = games
                return games

            games: list[ApiBasketGame] = []
            fetch_ok_any = False
            for path, label in (
                ("/boxscores/", "BR/NBA"),
                ("/wnba/boxscores/", "BR/WNBA"),
            ):
                try:
                    async with self._fetch_lock:
                        await asyncio.sleep(self._rate)
                        r = await self._client.get(
                            path, params={"month": d.month, "day": d.day, "year": d.year},
                        )
                    if r.status_code == 404:
                        # BR devuelve 404 cuando no hay partidos esa fecha (típico
                        # en off-season). Es una respuesta estable; la cacheamos vacía.
                        fetch_ok_any = True
                        continue
                    if r.status_code != 200:
                        log.warning("BR %s %s -> HTTP %s", label, d, r.status_code)
                        continue
                    games.extend(_parse_index_page(r.text, d, label))
                    fetch_ok_any = True
                except Exception:
                    log.exception("Fallo BR %s %s", label, d)

            if fetch_ok_any:
                await self._store.save_bref_basket_games(d, [asdict(g) for g in games])
                self._memory[d] = games
            return games

    async def find_game(
        self,
        team_a: str,
        team_b: str,
        around: date,
        day_window: int = API_BASKETBALL_DAY_WINDOW,
    ) -> ApiBasketGame | None:
        best: tuple[int, ApiBasketGame] | None = None
        for offset in range(-day_window, day_window + 1):
            d = around + timedelta(days=offset)
            for g in await self.games_on(d):
                score = _pair_score(team_a, team_b, g.team_home, g.team_away)
                if score >= API_BASKETBALL_FUZZY_THRESHOLD and (best is None or score > best[0]):
                    best = (score, g)
        return best[1] if best else None


def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _team_score(a: str, b: str) -> int:
    return int(fuzz.token_set_ratio(_normalize(a), _normalize(b)))


def _pair_score(bet_a: str, bet_b: str, home: str, away: str) -> int:
    s_direct = (_team_score(bet_a, home) + _team_score(bet_b, away)) // 2
    s_swap = (_team_score(bet_a, away) + _team_score(bet_b, home)) // 2
    return max(s_direct, s_swap)
