"""Zjistí, jak daleko do minulosti portál data má.

    export VHS_LOGIN='...' VHS_PASSWORD='...'
    .venv/bin/python tools/probe_history.py

Denní stránky (spotřeba, stav měřidla, křivka) berou parametry ``Annee`` a
``Mois``; výběr měsíců na webu začíná květnem 2022. Skript stáhne nejstarší
nabízený měsíc a roční přehled a vypíše první nenulové hodnoty.
"""

import asyncio
import importlib.util
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "vhs_api", ROOT / "custom_components" / "vhs_benesov" / "api.py"
)
api = importlib.util.module_from_spec(spec)
sys.modules["vhs_api"] = api
spec.loader.exec_module(api)

E_ = api.ENERGY_URL + "?Affichage="


async def main() -> None:
    client = api.VhsBenesovClient(os.environ["VHS_LOGIN"], os.environ["VHS_PASSWORD"])
    try:
        body = await client.async_get(E_ + "ConsoAn")
        block = api._jqplot_block(body)
        rows = api.RE_ROW_LABELLED.findall(block[1]) if block else []
        print("roční spotřeba (m³):", [(r[3], float(r[1])) for r in rows])

        for label, query in (
            ("denní spotřeba", "ConsoJour&Annee=2022&Mois=5"),
            ("stav měřidla", "IndexJour&IndexesSepares=true&PeriodeComplete=false&Annee=2022&Mois=5"),
        ):
            data = api.parse_day_series(await client.async_get(E_ + query))
            nonzero = [d for d in data if d.value > 0]
            print(f"{label}: {len(data)} dní v 05/2022, první nenulový:",
                  nonzero[0] if nonzero else None)
        curve = api.parse_curve(await client.async_get(E_ + "CourbeMois&Annee=2022&Mois=5"))
        print("křivka 05/2022:", len(curve), "bodů, první:", curve[:1])
    finally:
        await client.async_close()


asyncio.run(main())
