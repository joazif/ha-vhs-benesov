"""Ověření stažení celé historie proti živému portálu (bez zápisu do HA).

    export VHS_LOGIN='...' VHS_PASSWORD='...'
    .venv/bin/python tools/live_history.py

Projde všechny měsíce, které portál nabízí (asi 3-4 minuty, mezi požadavky je
pauza kolem 1,5 s), a vypíše rozsah dat, počet dnů, nejstarší a nejnovější stav
měřidla a kolik hodinových řádků by se zapsalo do statistik.
"""

import asyncio
import importlib.util
import os
import pathlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "custom_components" / "vhs_benesov" / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = load("vhs_api", "api.py")
history = load("vhs_history", "history.py")


async def main() -> None:
    client = api.VhsBenesovClient(os.environ["VHS_LOGIN"], os.environ["VHS_PASSWORD"])
    started = datetime.now()
    try:
        data = await client.async_fetch_history(
            progress=lambda done, total: print(f"  měsíc {done}/{total}", end="\r", flush=True)
        )
    finally:
        await client.async_close()
    print(f"\nhotovo za {(datetime.now() - started).seconds} s")

    days = sorted(data.index_end)
    print(f"měsíce ve výběru: {data.months} ({data.first_month} .. {data.last_month})")
    print(f"dnů se stavem měřidla: {len(days)}")
    if days:
        print(f"nejstarší: {days[0]} = {data.index_end[days[0]]} m³")
        print(f"nejnovější: {days[-1]} = {data.index_end[days[-1]]} m³")
        first_nonzero = next(
            (d for a, d in zip(days, days[1:]) if data.index_end[d] > data.index_end[a]), None
        )
        print(f"první den s nárůstem stavu: {first_nonzero}")
    print(f"bodů křivky po 6 h: {len(data.curve_liters)}")

    rows = history.build_hourly(data.index_end, data.curve_liters, ZoneInfo("Europe/Prague"))
    print(f"hodinových řádků pro statistiky: {len(rows)}")
    if rows:
        print(f"od {rows[0].start} do {rows[-1].start} (UTC)")
        total = sum(r.increase for r in rows)
        print(f"celková spotřeba v řádcích: {total:.3f} m³")
        gaps = sum(1 for a, b in zip(rows, rows[1:]) if (b.start - a.start).total_seconds() > 3600)
        print(f"mezer v datech: {gaps}")


asyncio.run(main())
