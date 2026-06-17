"""Deduplicación visual de boletos vía pHash + Hamming."""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime

import imagehash
from PIL import Image

from .storage import PickStore

log = logging.getLogger(__name__)


def compute_phash(image_bytes: bytes, hash_size: int = 8) -> str:
    """pHash de 64 bits devuelto como hex (Mongo no admite uint64)."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        img = img.convert("RGB")
        h = imagehash.phash(img, hash_size=hash_size)
    return str(h)


def hamming_distance(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


@dataclass(frozen=True)
class DedupHit:
    message_id: int
    date_utc: datetime
    distance: int


async def find_duplicate(
    store: PickStore,
    tipster: str,
    phash: str,
    date_utc: datetime,
    window_hours: int,
    max_distance: int,
) -> DedupHit | None:
    """Busca un pick previo del mismo tipster con pHash similar.

    Devuelve el hit más cercano si su distancia <= max_distance.
    """
    candidates = await store.candidates_for_dedup(tipster, date_utc, window_hours)
    best: DedupHit | None = None
    for doc in candidates:
        raw = doc.get("phash")
        if raw is None:
            continue
        other = raw if isinstance(raw, str) else format(int(raw), "016x")
        d = hamming_distance(phash, other)
        if d > max_distance:
            continue
        if best is None or d < best.distance:
            best = DedupHit(
                message_id=int(doc["message_id"]),
                date_utc=doc["date_utc"],
                distance=d,
            )
    return best
