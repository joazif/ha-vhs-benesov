"""Diagnostika přihlášení: zkusí několik variant a vypíše, co portál odpoví.

    export VHS_LOGIN='...' VHS_PASSWORD='...'
    .venv/bin/python tools/diagnose_login.py

Nevypisuje hodnoty cookies ani hesla, jen stavy, přesměrování a názvy cookies.
"""

import asyncio
import importlib.util
import os
import pathlib
import sys

import aiohttp

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "vhs_api", ROOT / "custom_components" / "vhs_benesov" / "api.py"
)
api = importlib.util.module_from_spec(spec)
sys.modules["vhs_api"] = api
spec.loader.exec_module(api)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
VARIANTS = {
    "A: současný klient": {},
    "B: UA jako Chrome": {"User-Agent": BROWSER_UA},
    "C: UA Chrome + Origin + Accept": {
        "User-Agent": BROWSER_UA,
        "Origin": "https://cz-sitr.suezsmartsolutions.com",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    },
}


async def attempt(name: str, extra: dict, login: str, password: str) -> None:
    headers = {"User-Agent": api.USER_AGENT, "Accept-Language": "cs-CZ,cs;q=0.9"}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(api.LOGIN_URL) as r:
            form = await r.text()
            print(f"\n[{name}] GET stav {r.status}, cookies: {[c.key for c in s.cookie_jar]}")
        payload = api.parse_hidden_fields(form)
        payload[api.FIELD_LOGIN] = login
        payload[api.FIELD_PASSWORD] = password
        payload[api.FIELD_RESOLUTION] = "1512x982"
        print("  odesílaná pole:", sorted(k.split("$")[-1] for k in payload))
        hdrs = {"Referer": api.LOGIN_URL, **extra}
        async with s.post(
            api.LOGIN_URL, data=payload, headers=hdrs, allow_redirects=False
        ) as r:
            body = await r.text()
            print(
                f"  POST stav {r.status}, Location: {r.headers.get('Location')}, "
                f"Set-Cookie: {[c.split('=')[0] for c in r.headers.getall('Set-Cookie', [])]}, "
                f"pole hesla v odpovědi: {api.is_login_page(body)}, {len(body)} znaků"
            )


async def follow(login: str, password: str) -> None:
    """Přihlásit a ručně projít řetěz přesměrování, u každého kroku vypsat stav."""
    from urllib.parse import urljoin

    headers = {"User-Agent": api.USER_AGENT, "Accept-Language": "cs-CZ,cs;q=0.9"}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(api.LOGIN_URL) as r:
            payload = api.parse_hidden_fields(await r.text())
        payload[api.FIELD_LOGIN] = login
        payload[api.FIELD_PASSWORD] = password
        payload[api.FIELD_RESOLUTION] = "1512x982"

        print("\n[D: ruční průchod přesměrováním]")
        method, url, data = "POST", api.LOGIN_URL, payload
        for hop in range(1, 8):
            async with s.request(
                method, url, data=data, allow_redirects=False,
                headers={"Referer": api.LOGIN_URL},
            ) as r:
                body = await r.text()
                print(
                    f"  {hop}. {method} {url.replace(api.BASE, '')} -> {r.status}, "
                    f"Location: {r.headers.get('Location')}, login stránka: "
                    f"{api.is_login_page(body)}, cookies v jar: "
                    f"{sorted({c.key for c in s.cookie_jar})}"
                )
                loc = r.headers.get("Location")
                if r.status in (301, 302, 303, 307) and loc:
                    method, url, data = "GET", urljoin(url, loc), None
                    continue
                break
        print("  text stránky:", api.page_text(body)[:300])
        async with s.get(api.HOME_URL) as r:
            body = await r.text()
            print(f"  GET Site.aspx -> {r.status}, login stránka: {api.is_login_page(body)}")


async def main() -> None:
    login, password = os.environ["VHS_LOGIN"], os.environ["VHS_PASSWORD"]
    for name, extra in VARIANTS.items():
        await attempt(name, extra, login, password)
    await follow(login, password)


asyncio.run(main())
