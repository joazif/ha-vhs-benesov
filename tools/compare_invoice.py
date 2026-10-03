"""Porovnání stavů měřidla na faktuře se stavy z portálu.

    export VHS_LOGIN='...' VHS_PASSWORD='...'
    .venv/bin/python tools/compare_invoice.py 2026-05-22 817 2026-08-24 884

Argumenty jsou dvojice "datum stav v m³" (faktura: stav předchozí a nový). Stáhne celou
historii (asi 3-4 minuty). Faktura uvádí stav k začátku data, proto se porovnává se stavem
portálu ke konci předchozího dne; vypíše se i okolí tří dnů a rozdíl proti faktuře.
Faktura má celé m³ (desetinná místa odříznutá), proto se rozdíl posuzuje v rozmezí 0 až +1.
"""

import asyncio
import importlib.util
import os
import pathlib
import sys
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "custom_components" / "vhs_benesov" / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = load("vhs_api", "api.py")


async def main() -> None:
    args = sys.argv[1:]
    pairs = [(date.fromisoformat(args[i]), float(args[i + 1])) for i in range(0, len(args), 2)]
    client = api.VhsBenesovClient(os.environ["VHS_LOGIN"], os.environ["VHS_PASSWORD"])
    try:
        data = await client.async_fetch_history()
    finally:
        await client.async_close()
    index = data.index_end
    print(f"Dnů se stavem: {len(index)}, od {min(index)} do {max(index)}")
    for day, invoice in pairs:
        print(f"\nFaktura {day}: {invoice:.0f} m³")
        for shift in range(-3, 4):
            d = day + timedelta(days=shift)
            value = index.get(d - timedelta(days=1))  # začátek dne d = konec předchozího
            shown = "-" if value is None else f"{value:.3f} m³ ({value - invoice:+.3f} proti faktuře)"
            mark = "  <== datum z faktury" if shift == 0 else ""
            print(f"  začátek {d}  {shown}{mark}")
    if len(pairs) == 2:
        (d1, v1), (d2, v2) = pairs
        a, b = index.get(d1 - timedelta(days=1)), index.get(d2 - timedelta(days=1))
        if a is not None and b is not None:
            days = (d2 - d1).days
            print(f"\nPortál {d1}..{d2}: {b - a:.3f} m³ za {days} dní = {(b - a) * 1000 / days:.0f} l/den")
            print(f"Faktura:              {v2 - v1:.0f} m³ za {days} dní = {(v2 - v1) * 1000 / days:.0f} l/den")
            print(f"Rozdíl: {(b - a) - (v2 - v1):+.3f} m³ (faktura je zaokrouhlená na celá m³)")


asyncio.run(main())
