"""Capa de persistencia MongoDB Atlas vía Motor (async)."""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING

from .models import PickDocument, PickResolution

log = logging.getLogger(__name__)

PICKS_COLLECTION = "picks"
TE_MATCHES_COLLECTION = "te_matches"
API_BASKETBALL_COLLECTION = "api_basketball_games"
ESPN_BASKET_COLLECTION = "espn_basket_games"
ESPN_BASKET_BOXSCORE_COLLECTION = "espn_basket_boxscores"
BREF_BASKET_COLLECTION = "bref_basket_games"


class PickStore:
    def __init__(self, uri: str, db_name: str) -> None:
        self._client: AsyncIOMotorClient = AsyncIOMotorClient(uri)
        self._db: AsyncIOMotorDatabase = self._client[db_name]
        self._col: AsyncIOMotorCollection = self._db[PICKS_COLLECTION]
        self._te: AsyncIOMotorCollection = self._db[TE_MATCHES_COLLECTION]
        self._basket: AsyncIOMotorCollection = self._db[API_BASKETBALL_COLLECTION]
        self._espn_basket: AsyncIOMotorCollection = self._db[ESPN_BASKET_COLLECTION]
        self._espn_basket_box: AsyncIOMotorCollection = self._db[ESPN_BASKET_BOXSCORE_COLLECTION]
        self._bref_basket: AsyncIOMotorCollection = self._db[BREF_BASKET_COLLECTION]

    async def ensure_indexes(self) -> None:
        await self._col.create_index(
            [("tipster", ASCENDING), ("message_id", ASCENDING)],
            unique=True, name="uq_tipster_message",
        )
        await self._col.create_index(
            [("tipster", ASCENDING), ("date_utc", DESCENDING)],
            name="ix_tipster_date",
        )
        await self._col.create_index(
            [("tipster", ASCENDING), ("phash", ASCENDING)],
            name="ix_tipster_phash",
        )

    # ---- picks ----

    async def exists(self, tipster: str, message_id: int) -> bool:
        doc = await self._col.find_one(
            {"tipster": tipster, "message_id": message_id},
            projection={"_id": 1},
        )
        return doc is not None

    async def get_pick(self, tipster: str, message_id: int) -> dict[str, Any] | None:
        return await self._col.find_one({"tipster": tipster, "message_id": message_id})

    async def update_payload(self, tipster: str, message_id: int, payload: dict[str, Any]) -> None:
        await self._col.update_one(
            {"tipster": tipster, "message_id": message_id},
            {"$set": {"payload": payload}},
        )

    async def replace_pick_identity(
        self, tipster: str, old_message_id: int,
        new_message_id: int, new_date_utc: datetime,
    ) -> None:
        """Sustituye message_id y date_utc de un pick existente por los del candidato
        más antiguo. Usado por el dedup semántico para preservar la fecha del pendiente
        cuando llega después un recap que es el mismo pick."""
        await self._col.update_one(
            {"tipster": tipster, "message_id": old_message_id},
            {"$set": {"message_id": new_message_id, "date_utc": new_date_utc}},
        )

    async def candidates_for_dedup(
        self, tipster: str, around: datetime, window_hours: int,
    ) -> list[dict[str, Any]]:
        delta = timedelta(hours=window_hours)
        cursor = self._col.find(
            {
                "tipster": tipster,
                "date_utc": {"$gte": around - delta, "$lte": around + delta},
            },
            projection={"phash": 1, "message_id": 1, "date_utc": 1},
        )
        return [doc async for doc in cursor]

    async def recent_picks_full(
        self, tipster: str, around: datetime, window_hours: int,
    ) -> list[dict[str, Any]]:
        """Devuelve picks completos del tipster en ventana ±window_hours.
        Usado para dedup semántico post-visión (mismo jugador/mercado/línea)."""
        delta = timedelta(hours=window_hours)
        cursor = self._col.find(
            {
                "tipster": tipster,
                "date_utc": {"$gte": around - delta, "$lte": around + delta},
            },
            projection={"message_id": 1, "date_utc": 1, "payload": 1},
        )
        return [doc async for doc in cursor]

    async def insert_pick(self, pick: PickDocument) -> None:
        await self._col.update_one(
            {"tipster": pick.tipster, "message_id": pick.message_id},
            {"$setOnInsert": pick.model_dump(mode="python")},
            upsert=True,
        )

    async def update_resolution(
        self,
        tipster: str,
        message_id: int,
        resolution: PickResolution,
        profit_units: float | None,
    ) -> None:
        await self._col.update_one(
            {"tipster": tipster, "message_id": message_id},
            {"$set": {
                "resolution": resolution.model_dump(mode="python"),
                "profit_units": profit_units,
            }},
        )

    async def iter_picks(self, tipster: str) -> list[dict[str, Any]]:
        cursor = self._col.find({"tipster": tipster})
        return [doc async for doc in cursor]

    # ---- cache de Tennis Explorer ----

    async def get_te_matches(self, d: date) -> list[dict[str, Any]] | None:
        doc = await self._te.find_one({"_id": d.isoformat()})
        if doc is None:
            return None
        return doc.get("matches", [])

    async def save_te_matches(self, d: date, matches: list[dict[str, Any]]) -> None:
        # Mongo no acepta `date`; convertimos a ISO string para serializar limpio.
        for m in matches:
            md = m.get("match_date")
            if isinstance(md, date) and not isinstance(md, datetime):
                m["match_date"] = md.isoformat()
        await self._te.update_one(
            {"_id": d.isoformat()},
            {"$set": {"matches": matches, "fetched_at": datetime.utcnow()}},
            upsert=True,
        )

    # ---- cache de API-Sports Basketball ----

    async def get_api_basketball_games(self, d: date) -> list[dict[str, Any]] | None:
        doc = await self._basket.find_one({"_id": d.isoformat()})
        if doc is None:
            return None
        return doc.get("games", [])

    async def save_api_basketball_games(self, d: date, games: list[dict[str, Any]]) -> None:
        for g in games:
            md = g.get("match_date")
            if isinstance(md, date) and not isinstance(md, datetime):
                g["match_date"] = md.isoformat()
        await self._basket.update_one(
            {"_id": d.isoformat()},
            {"$set": {"games": games, "fetched_at": datetime.utcnow()}},
            upsert=True,
        )

    # ---- cache ESPN basketball ----

    async def get_espn_basket_games(self, d: date) -> list[dict[str, Any]] | None:
        doc = await self._espn_basket.find_one({"_id": d.isoformat()})
        if doc is None:
            return None
        return doc.get("games", [])

    async def save_espn_basket_games(self, d: date, games: list[dict[str, Any]]) -> None:
        for g in games:
            md = g.get("match_date")
            if isinstance(md, date) and not isinstance(md, datetime):
                g["match_date"] = md.isoformat()
        await self._espn_basket.update_one(
            {"_id": d.isoformat()},
            {"$set": {"games": games, "fetched_at": datetime.utcnow()}},
            upsert=True,
        )

    # ---- cache ESPN boxscore por evento ----

    async def get_espn_basket_boxscore(
        self, league: str, event_id: int,
    ) -> list[dict[str, Any]] | None:
        doc = await self._espn_basket_box.find_one({"_id": f"{league}:{event_id}"})
        if doc is None:
            return None
        return doc.get("players", [])

    async def save_espn_basket_boxscore(
        self, league: str, event_id: int, players: list[dict[str, Any]],
    ) -> None:
        await self._espn_basket_box.update_one(
            {"_id": f"{league}:{event_id}"},
            {"$set": {"players": players, "fetched_at": datetime.utcnow()}},
            upsert=True,
        )

    # ---- cache Basketball-Reference ----

    async def get_bref_basket_games(self, d: date) -> list[dict[str, Any]] | None:
        doc = await self._bref_basket.find_one({"_id": d.isoformat()})
        if doc is None:
            return None
        return doc.get("games", [])

    async def save_bref_basket_games(self, d: date, games: list[dict[str, Any]]) -> None:
        for g in games:
            md = g.get("match_date")
            if isinstance(md, date) and not isinstance(md, datetime):
                g["match_date"] = md.isoformat()
        await self._bref_basket.update_one(
            {"_id": d.isoformat()},
            {"$set": {"games": games, "fetched_at": datetime.utcnow()}},
            upsert=True,
        )

    def close(self) -> None:
        self._client.close()


def store_from_env() -> PickStore:
    uri = os.environ["MONGO_URI"]
    db_name = os.environ.get("MONGO_DB", "bettelegramscanner")
    return PickStore(uri, db_name)
