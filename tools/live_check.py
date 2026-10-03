"""Ověření klienta proti živému portálu.

    export VHS_LOGIN='...'
    export VHS_PASSWORD='...'
    .venv/bin/python tools/live_check.py [--dump]

--dump uloží stažené stránky jako tools/*.local.html (v .gitignore) - hodí se
k přepsání testovacích fixtures podle skutečného markupu.
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


async def main() -> None:
    client = api.VhsBenesovClient(os.environ["VHS_LOGIN"], os.environ["VHS_PASSWORD"])
    try:
        if "--dump" in sys.argv:
            pages = {
                "home": api.HOME_URL,
                "consojour": f"{api.ENERGY_URL}?Affichage=ConsoJour",
                "consomois": f"{api.ENERGY_URL}?Affichage=ConsoMois",
                "courbe": f"{api.ENERGY_URL}?Affichage=CourbeMois",
                "indexjour": f"{api.ENERGY_URL}?Affichage=IndexJour"
                "&IndexesSepares=true&PeriodeComplete=false",
            }
            for name, url in pages.items():
                (ROOT / "tools" / f"{name}.local.html").write_text(
                    await client.async_get(url), "utf-8"
                )
        data = await client.async_fetch_all()
        print("měřidlo:      ", data.meter_id)
        print("poslední odeč.:", data.last_reading)
        print("stav (m³):    ", data.index_m3, "k", data.index_day)
        print("dnes/včera (l):", data.daily_liters[-1:], data.daily_liters[-2:-1])
        print("měsíce:       ", len(data.monthly_m3), "poslední", data.monthly_m3[-1:])
        print("křivka bodů:  ", len(data.curve_liters))
    except api.InvalidAuth:
        print("Přihlášení odmítnuto. Text stránky po odeslání:")
        print(client.last_login_text)
    finally:
        await client.async_close()


asyncio.run(main())
