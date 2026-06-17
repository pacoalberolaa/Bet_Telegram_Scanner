"""Entry point: `python -m bettelegramscanner`.

CLI -> export ingest -> particion por meses con N workers -> pipeline -> Excel.
"""
from __future__ import annotations

import asyncio
import logging
from calendar import monthrange
from datetime import datetime

from dotenv import load_dotenv
from pydantic import TypeAdapter

load_dotenv()

from .analytics import build_report
from .cli import collect_request
from .config import REPORTS_DIR, WORKERS, setup_logging
from .ingest_export import ExportIngest
from .models import PickDocument
from .pipeline import process_candidate
from .reports import export_xlsx
from .resolver_tennis import TennisExplorerClient
from .storage import store_from_env
from .vision import VisionExtractor

log = logging.getLogger("bettelegramscanner")

_PICK_ADAPTER = TypeAdapter(PickDocument)


def month_chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Parte la ventana en trozos mensuales (ambos extremos inclusivos)."""
    chunks: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor <= end:
        last_day = monthrange(cursor.year, cursor.month)[1]
        month_end = cursor.replace(day=last_day, hour=23, minute=59, second=59, microsecond=0)
        chunk_end = min(month_end, end)
        chunks.append((cursor, chunk_end))
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1, day=1,
                                    hour=0, minute=0, second=0, microsecond=0)
        else:
            cursor = cursor.replace(month=cursor.month + 1, day=1,
                                    hour=0, minute=0, second=0, microsecond=0)
    return chunks


async def _run_chunks(
    chunks: list[tuple[datetime, datetime]],
    ingest: ExportIngest,
    store,
    vision: VisionExtractor,
    te: TennisExplorerClient,
    concurrency: int,
) -> tuple[int, int]:
    sem = asyncio.Semaphore(concurrency)
    totals = [0, 0]  # [seen, persisted]
    totals_lock = asyncio.Lock()

    async def worker(chunk: tuple[datetime, datetime]) -> None:
        label = f"{chunk[0].date()}..{chunk[1].date()}"
        async with sem:
            log.info("[%s] arrancando", label)
            seen = persisted = 0
            for candidate in ingest.iter_in_window(*chunk):
                seen += 1
                pick = await process_candidate(candidate, store, vision, te)
                if pick is not None:
                    persisted += 1
            async with totals_lock:
                totals[0] += seen
                totals[1] += persisted
            log.info("[%s] fin vistos=%d persistidos=%d", label, seen, persisted)

    await asyncio.gather(*(worker(c) for c in chunks))
    return totals[0], totals[1]


async def _run() -> None:
    req = collect_request()

    store = store_from_env()
    await store.ensure_indexes()
    vision = VisionExtractor()
    te = TennisExplorerClient(store)
    ingest = ExportIngest(req.export_folder, tipster=req.tipster)

    chunks = month_chunks(req.start, req.end)
    log.info("Ventana partida en %d meses; workers=%d", len(chunks), WORKERS)

    try:
        seen, persisted = await _run_chunks(chunks, ingest, store, vision, te, WORKERS)
        log.info("TOTAL vistos=%d persistidos=%d", seen, persisted)

        raw_docs = await store.iter_picks(req.tipster)
        picks_all = [_PICK_ADAPTER.validate_python(d) for d in raw_docs]
        picks = [p for p in picks_all if req.start <= p.date_utc <= req.end]
        report = build_report(req.tipster, picks)
        log.info("REPORT %s", report.render())
        print("\n" + report.render())

        safe = req.tipster.replace("/", "_").replace("\\", "_").replace(" ", "_")
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        xlsx_path = REPORTS_DIR / f"{safe}_{req.start.date()}_{req.end.date()}_{stamp}.xlsx"
        export_xlsx(xlsx_path, report, picks)
        print(f"Excel: {xlsx_path}")
    finally:
        await te.aclose()
        store.close()


def main() -> None:
    setup_logging()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.warning("Interrumpido por el usuario.")


if __name__ == "__main__":
    main()
