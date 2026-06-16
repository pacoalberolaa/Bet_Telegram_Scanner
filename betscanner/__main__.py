"""Entry point: `python -m betscanner`.

CLI -> extractor histórico -> pipeline (pHash + IA + resolver TE + Mongo) -> Excel.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from pydantic import TypeAdapter
from telethon import TelegramClient

from .analytics import build_report
from .cli import collect_request
from .config import REPORTS_DIR, setup_logging
from .extractor import iter_historical_candidates
from .models import PickDocument
from .pipeline import process_candidate
from .reports import export_xlsx
from .resolver_tennis import TennisExplorerClient
from .storage import store_from_env
from .vision import VisionExtractor

log = logging.getLogger("betscanner")

_PICK_ADAPTER = TypeAdapter(PickDocument)


async def _run() -> None:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session = os.environ.get("TG_SESSION", "betscanner")

    request = collect_request()

    store = store_from_env()
    await store.ensure_indexes()
    vision = VisionExtractor()
    te = TennisExplorerClient(store)

    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    try:
        seen = persisted = 0
        async for candidate in iter_historical_candidates(client, request):
            seen += 1
            pick = await process_candidate(candidate, store, vision, te)
            if pick is not None:
                persisted += 1
        log.info("Candidatos vistos=%d persistidos=%d", seen, persisted)

        raw_docs = await store.iter_picks(request.channel)
        picks = [_PICK_ADAPTER.validate_python(d) for d in raw_docs]
        report = build_report(request.channel, picks)
        log.info("REPORT %s", report.render())
        print("\n" + report.render())

        safe_channel = request.channel.replace("@", "").replace("/", "_")
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        xlsx_path = REPORTS_DIR / f"{safe_channel}_{stamp}.xlsx"
        export_xlsx(xlsx_path, report, picks)
        print(f"Excel: {xlsx_path}")
    finally:
        await client.disconnect()
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
