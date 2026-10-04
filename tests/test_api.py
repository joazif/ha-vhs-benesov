"""Testy parserů a klienta proti markupu opsanému ze skutečného portálu."""

import pathlib
from datetime import date, datetime, timedelta

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from conftest import api, fixture

# Test klienta jede proti lokálnímu falešnému serveru; HA plugin sockety blokuje.
pytestmark = pytest.mark.usefixtures("socket_enabled")


def test_hidden_fields_unescape_and_button():
    f = api.parse_hidden_fields(fixture("login"))
    assert f["__VIEWSTATE"] == "/wEPDwUJTEST+abc=="
    assert f["__EVENTVALIDATION"] == "/wEdAAQvalid&x"
    assert f[api.FIELD_BUTTON] == "Přihlásit"
    assert f["ctl00$PHZonePrincipale$Langues$DropDownListLangues"] == "cs"
    assert api.is_login_page(fixture("login"))
    assert not api.is_login_page(fixture("home"))


def test_home():
    body = fixture("home")
    assert api.parse_meter_id(body) == "12345-XX-0000001"
    assert api.parse_last_reading(body) == datetime(2026, 9, 29, 17, 42)
    assert api.parse_meter_reading(body) == 912.4
    assert api.find_index_url(body) == (
        api.BASE + "Site_Energie.aspx?Affichage=IndexJour"
        "&IndexesSepares=true&PeriodeComplete=false"
    )


def test_daily_liters_handles_both_quote_styles_and_ignores_trailing_arrays():
    rows = api.parse_day_series(fixture("consojour"))
    assert [(r.day, r.value) for r in rows][:2] == [
        (date(2026, 9, 1), 559.0),
        (date(2026, 9, 2), 645.0),
    ]
    assert rows[-1] == api.DayValue(date(2026, 9, 29), 234.0)
    assert len(rows) == 6


def test_monthly_uses_czech_month_names():
    rows = api.parse_month_series(fixture("consomois"))
    assert rows[0] == api.DayValue(date(2024, 10, 1), 11.485)
    assert rows[-1] == api.DayValue(date(2025, 9, 1), 20.0)
    assert len(rows) == 5


def test_index_and_curve():
    idx = api.parse_day_series(fixture("indexjour"))
    assert idx[-1] == api.DayValue(date(2026, 9, 29), 912.333)
    curve = api.parse_curve(fixture("courbe"))
    assert curve[1] == api.PointValue(datetime(2026, 9, 1, 6, 0), 165.0)
    assert len(curve) == 4


def test_missing_graph_gives_empty():
    assert api.parse_day_series("<html></html>") == []
    assert api.parse_curve("<html></html>") == []


class FakePortal:
    """Falešný portál: přihlášení nastaví cookie, bez ní vrací stránku s heslem."""

    def __init__(self, password="heslo"):
        self.password = password
        self.sessions: set[str] = set()
        self.posts: list[dict] = []
        self.month_requests: list[tuple] = []
        self.consojour_html: str | None = None  # přepis hlavní denní stránky
        self.fail_month_pages = 0        # tolikrát odmítnout stránku měsíce
        self.fail_status = 429
        self.retry_after: str | None = None
        self.logins = 0
        self.user_agents: set[str] = set()
        self.empty_current_index = False      # nový měsíc bez prvního odečtu
        self.energy_requests = 0         # kolikrát si klient vyžádal stránky se spotřebou

    @staticmethod
    def _raw_cookies(request) -> dict[str, str]:
        # Cookie hlavičku čteme ručně: hodnota autentizační cookie je nestandardní.
        pairs = (p.strip().partition("=") for p in request.headers.get("Cookie", "").split(";"))
        return {k: v for k, _, v in pairs if k}

    def _authed(self, request):
        return self._raw_cookies(request).get("SE_Pilote_Cookie") in self.sessions

    def _page(self, request, name):
        self.user_agents.add(request.headers.get("User-Agent", ""))
        if not self._authed(request):
            # Jako skutečný portál: bez session přesměruje na přihlášení s ReturnUrl.
            return web.Response(
                status=302,
                headers={"Location": f"Login.aspx?ReturnUrl=%2fe%2f{request.path_qs.split('/')[-1]}"},
            )
        return web.Response(text=fixture(name), content_type="text/html")

    async def login_get(self, request):
        if "ReturnUrl" in request.query:
            # Otevření přihlašovací stránky s ReturnUrl session ukončí (jako na portálu).
            self.sessions.discard(self._raw_cookies(request).get("SE_Pilote_Cookie"))
        return web.Response(text=fixture("login"), content_type="text/html")

    async def login_post(self, request):
        form = dict(await request.post())
        self.posts.append(form)
        if form.get(api.FIELD_PASSWORD) != self.password:
            return web.Response(text=fixture("login"), content_type="text/html")
        self.logins += 1
        # Jako skutečný portál: cookie se nastaví dvakrát (smazání, pak hodnota),
        # hodnota má čárku, mezeru a "=" - aiohttp CookieJar by ji zahodil.
        sid = f"s{self.logins},x y=="
        self.sessions.add(sid)
        resp = web.Response(status=302, headers={"Location": "default.aspx"})
        resp.headers.add(
            "Set-Cookie",
            "SE_Pilote_Cookie=; expires=Thu, 01-Jan-1970 00:00:00 GMT; path=/",
        )
        resp.headers.add("Set-Cookie", f"SE_Pilote_Cookie={sid}; path=/; HttpOnly")
        return resp

    async def default(self, request):
        if not self._authed(request):
            return web.Response(
                status=302, headers={"Location": "Login.aspx?ReturnUrl=%2fe%2fdefault.aspx"}
            )
        return web.Response(status=302, headers={"Location": "Site.aspx"})

    async def home(self, request):
        return self._page(request, "home")

    async def energie(self, request):
        self.energy_requests += 1
        mode = request.query.get("Affichage")
        name = {
            "ConsoJour": "consojour", "ConsoMois": "consomois",
            "CourbeMois": "courbe", "IndexJour": "indexjour",
        }[mode]
        if mode == "IndexJour" and "Annee" not in request.query:
            assert request.query["IndexesSepares"] == "true"
            if self.empty_current_index and self._authed(request):
                return web.Response(text="<html><body>bez dat</body></html>", content_type="text/html")
        if "Annee" in request.query and self._authed(request):
            return self._month(request, mode)
        if mode == "ConsoJour" and self.consojour_html and self._authed(request):
            return web.Response(text=self.consojour_html, content_type="text/html")
        return self._page(request, name)

    def _month(self, request, mode):
        """Stránka konkrétního měsíce: stav roste o 0,5 m³ denně od 100."""
        if self.fail_month_pages > 0 and mode == "IndexJour":
            self.fail_month_pages -= 1
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return web.Response(status=self.fail_status, headers=headers, text="zpomal")
        self.month_requests.append((mode, request.query["Annee"], request.query["Mois"]))
        year, month = int(request.query["Annee"]), int(request.query["Mois"])
        days = [
            date(year, month, 1) + timedelta(days=i)
            for i in range((date(year + (month == 12), month % 12 + 1, 1) - date(year, month, 1)).days)
        ]
        if mode == "ConsoJour":
            rows = ",".join(
                f"[{i}, 100, '{d:%d.%m.%Y}', '100 l']" for i, d in enumerate(days, 1)
            )
        elif mode == "IndexJour":
            rows = ",".join(
                f"[{i}, {100 + 0.5 * (d - date(2026, 6, 30)).days}, '{d:%d.%m.%Y}', 'Odběrová křivka ']"
                for i, d in enumerate(days, 1)
            )
        else:
            rows = ",".join(
                f"['{d:%Y-%m-%d} {h:02d}:00', 125]" for d in days for h in (0, 6, 12, 18)
            )
        return web.Response(
            text=f"<html><script>$.jqplot('x', [[{rows}]], {{}});</script></html>",
            content_type="text/html",
        )


def make_client(password="heslo"):
    # Stejně jako ostrý klient: cookies drží klient sám, jar je vypnutý.
    session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
    return api.VhsBenesovClient("login", password, session=session), session


@pytest.fixture
async def portal(monkeypatch):
    fake = FakePortal()
    app = web.Application()
    app.router.add_get("/e/Login.aspx", fake.login_get)
    app.router.add_post("/e/Login.aspx", fake.login_post)
    app.router.add_get("/e/default.aspx", fake.default)
    app.router.add_get("/e/Site.aspx", fake.home)
    app.router.add_get("/e/Site_Energie.aspx", fake.energie)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("/e/"))
    for name, path in (
        ("BASE", ""), ("LOGIN_URL", "Login.aspx"), ("HOME_URL", "Site.aspx"),
        ("ENERGY_URL", "Site_Energie.aspx"),
    ):
        monkeypatch.setattr(api, name, base + path)
    yield fake
    await server.close()


async def test_requests_identify_the_integration_and_link_to_its_repository(portal):
    client = api.VhsBenesovClient("login", "heslo")     # vlastní session jako ostrý klient
    try:
        await client.async_probe()
    finally:
        await client.async_close()
    assert portal.user_agents == {api.USER_AGENT}
    assert "HomeAssistant vhs_benesov" in api.USER_AGENT
    assert "https://github.com/joazif/ha-vhs-benesov" in api.USER_AGENT


async def test_new_month_without_a_reading_falls_back_to_the_previous_months_index(portal):
    portal.empty_current_index = True
    client, session = make_client()
    data = await client.async_fetch_all()
    await session.close()
    assert data.daily_index_m3, "stavy se mají vzít z měsíce posledního odečtu"
    assert max(d.day for d in data.daily_index_m3).month == data.last_reading.month
    assert data.index_m3 is not None


async def test_probe_reads_only_the_home_page(portal):
    client, session = make_client()
    probe = await client.async_probe()
    await session.close()
    assert probe.known
    assert probe.reading_m3 == 912.4
    assert probe.last_reading is not None
    assert portal.energy_requests == 0          # jedna stránka, žádná data o spotřebě


async def test_fetch_all(portal):
    client, session = make_client()
    data = await client.async_fetch_all()
    await session.close()

    sent = portal.posts[0]
    assert sent[api.FIELD_LOGIN] == "login"
    assert sent["__VIEWSTATE"] == "/wEPDwUJTEST+abc=="
    assert sent["ctl00$PHZonePrincipale$Langues$DropDownListLangues"] == "cs"
    assert sent[api.FIELD_RESOLUTION] == "1920x1080"
    assert sent["__EVENTVALIDATION"] == "/wEdAAQvalid&x"
    assert data.meter_id == "12345-XX-0000001"
    assert data.reading_m3 == 912.4
    assert data.index_m3 == 912.4             # číselník je novější než poslední bod denní řady
    assert data.daily_index_m3[-1].value == 912.333
    assert data.index_day == date(2026, 9, 29)  # den posledního odečtu
    assert data.liters_on(date(2026, 9, 29)) == 234.0
    assert data.liters_on(date(2020, 1, 1)) is None
    assert data.last_reading == datetime(2026, 9, 29, 17, 42)
    # Křivka má jen jeden den, takže se dotáhl i srpen (31 dní × 4 kroky).
    assert len(data.monthly_m3) == 5 and len(data.curve_liters) == 4 + 31 * 4


async def test_bad_credentials(portal):
    client, session = make_client("spatne")
    with pytest.raises(api.InvalidAuth):
        await client.async_validate()
    await session.close()


async def test_relogin_when_session_expires(portal):
    client, session = make_client()
    await client.async_fetch_all()
    portal.sessions.clear()  # server zapomněl session
    data = await client.async_fetch_all()
    await session.close()
    assert data.index_m3 == 912.4   # číselník z hlavní stránky
    assert portal.logins == 2


# Skutečné stránky uložené přes tools/live_check.py --dump (v .gitignore,
# v repu nejsou); bez nich se testy přeskočí.
_LOCAL = pathlib.Path(__file__).resolve().parents[1] / "tools"


def _local(name):
    path = _LOCAL / f"{name}.local.html"
    if not path.exists():
        pytest.skip("chybí uložená skutečná stránka (tools/live_check.py --dump)")
    return path.read_text("utf-8")


def test_real_pages_parse():
    home = _local("home")
    assert api.parse_meter_id(home)
    assert api.parse_last_reading(home)
    assert api.parse_meter_reading(home) is not None
    assert api.find_index_url(home)

    daily = api.parse_day_series(_local("consojour"))
    assert len(daily) >= 28 and all(d.value >= 0 for d in daily)
    index = api.parse_day_series(_local("indexjour"))
    assert index == sorted(index, key=lambda d: d.day)
    # Stav měřidla nikdy neklesá.
    assert all(a.value <= b.value for a, b in zip(index, index[1:]))
    assert len(api.parse_month_series(_local("consomois"))) >= 12
    assert len(api.parse_curve(_local("courbe"))) >= 4 * 20


def test_month_options():
    assert api.parse_month_options(fixture("consojour")) == [
        date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1),
    ]
    assert api.parse_month_options("<html></html>") == []


async def test_fetch_history_walks_every_offered_month(portal):
    client, session = make_client()
    steps = []
    history = await client.async_fetch_history(delay=0, progress=lambda a, b: steps.append((a, b)))
    await session.close()

    assert steps == [(1, 3), (2, 3), (3, 3)]
    assert history.first_month == date(2026, 7, 1) and history.last_month == date(2026, 9, 1)
    assert len(history.index_end) == 31 + 31 + 30
    assert history.index_end[date(2026, 7, 1)] == 100.5
    assert history.index_end[date(2026, 9, 30)] == 100 + 0.5 * 92
    assert len(history.curve_liters) == 92 * 4
    assert history.curve_liters[datetime(2026, 8, 1, 6, 0)] == 125
    assert sorted({(y, m) for _, y, m in portal.month_requests}) == [
        ("2026", "7"), ("2026", "8"), ("2026", "9"),
    ]
    assert len(portal.month_requests) == 6  # dvě stránky na měsíc


async def test_fetch_history_fails_without_month_picker(portal, monkeypatch):
    monkeypatch.setattr(api, "parse_month_options", lambda body: [])
    client, session = make_client()
    with pytest.raises(api.ParseError):
        await client.async_fetch_history(delay=0)
    await session.close()


def _consojour_page(days):
    rows = ",".join(f"[{i}, 50, '{d:%d.%m.%Y}', '50 l']" for i, d in enumerate(days, 1))
    return f"<html><script>$.jqplot('x', [[{rows}]], {{}});</script></html>"


async def test_week_crossing_month_pulls_previous_month(portal):
    """Úterý 1. 9. 2026: pondělí 31. 8. je v předchozím měsíci, musí se dotáhnout."""
    portal.consojour_html = _consojour_page([date(2026, 9, 1), date(2026, 9, 2)])
    client, session = make_client()
    data = await client.async_fetch_all()
    await session.close()

    by_day = {d.day: d.value for d in data.daily_liters}
    assert by_day[date(2026, 8, 31)] == 100.0            # z předchozího měsíce
    assert by_day[date(2026, 9, 1)] == 50.0               # aktuální měsíc má přednost
    assert ("ConsoJour", "2026", "8") in portal.month_requests
    assert [d.day for d in data.daily_liters] == sorted(d.day for d in data.daily_liters)


async def test_week_inside_month_needs_no_extra_request(portal):
    portal.consojour_html = _consojour_page([date(2026, 9, 8), date(2026, 9, 9)])  # úterý
    client, session = make_client()
    await client.async_fetch_all()
    await session.close()
    assert not [r for r in portal.month_requests if r[0] == "ConsoJour"]


def test_complete_days_ignores_unfinished_day():
    def pt(day, hour, value):
        return api.PointValue(datetime(2026, 9, day, hour), value)

    curve = [pt(28, h, 10 + h) for h in (0, 6, 12, 18)] + [pt(29, 0, 1), pt(29, 6, 2)]
    days = api.complete_days(curve)
    assert list(days) == [date(2026, 9, 28)]
    assert days[date(2026, 9, 28)] == {0: 10, 6: 16, 12: 22, 18: 28}
    assert api.complete_days([]) == {}


async def test_curve_context_is_fetched_only_when_few_complete_days(portal):
    client, session = make_client()
    data = await client.async_fetch_all()  # fixture: 1 úplný den -> dotáhne srpen
    assert len(api.complete_days(data.curve_liters)) == 1 + 31
    assert ("CourbeMois", "2026", "8") in portal.month_requests
    await session.close()


async def test_curve_context_skipped_with_enough_days(portal, monkeypatch):
    monkeypatch.setattr(api, "MIN_CONTEXT_DAYS", 1)
    portal.month_requests.clear()
    client, session = make_client()
    await client.async_fetch_all()
    await session.close()
    assert not [r for r in portal.month_requests if r[0] == "CourbeMois"]


# ---------------------------------------------------------------------------
# Zpomalené stahování historie
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402


async def test_pause_has_jitter_and_zero_delay_does_not_wait(monkeypatch):
    waited = []
    real_sleep = asyncio.sleep

    async def record(delay, *args):
        waited.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", record)
    for _ in range(200):
        await api._pause(1.5)
    await api._pause(0)

    assert len(waited) == 200                      # nulová pauza nečeká
    low, high = 1.5 * (1 - api.HISTORY_JITTER), 1.5 * (1 + api.HISTORY_JITTER)
    assert all(low <= w <= high for w in waited)
    assert len({round(w, 6) for w in waited}) > 100  # opravdu náhodné, ne metronom


def test_default_pause_is_slower_than_before():
    assert api.HISTORY_DELAY >= 1.5   # dřív 0,5 s
    assert api.HISTORY_ATTEMPTS == 3


async def test_history_pauses_before_every_request(portal, monkeypatch):
    calls = []

    async def fake_pause(delay):
        calls.append(delay)

    monkeypatch.setattr(api, "_pause", fake_pause)
    client, session = make_client()
    await client.async_fetch_history(backoff=0)      # výchozí pauza z konstanty
    await session.close()
    assert calls == [api.HISTORY_DELAY] * 6           # 3 měsíce × 2 stránky


async def _sleeps(monkeypatch):
    waited = []
    real_sleep = asyncio.sleep

    async def record(delay, *args):
        if delay > 0.5:               # vlastní čekání klienta, ne vnitřní yield aiohttp
            waited.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", record)
    return waited


async def test_rate_limit_waits_with_growing_backoff_then_succeeds(portal, monkeypatch):
    waited = await _sleeps(monkeypatch)
    portal.fail_month_pages = 2                       # dvakrát 429, potom projde
    client, session = make_client()
    history = await client.async_fetch_history(delay=0, backoff=10)
    await session.close()
    assert waited == [10, 20]                         # backoff × pořadí pokusu
    assert len(history.index_end) == 92               # nic nechybí


async def test_retry_after_header_is_honoured(portal, monkeypatch):
    waited = await _sleeps(monkeypatch)
    portal.fail_month_pages, portal.retry_after = 2, "7"
    client, session = make_client()
    await client.async_fetch_history(delay=0, backoff=10)
    await session.close()
    assert waited == [7, 7]


async def test_retry_after_is_capped(portal, monkeypatch):
    waited = await _sleeps(monkeypatch)
    portal.fail_month_pages, portal.retry_after = 1, "99999"
    client, session = make_client()
    await client.async_fetch_history(delay=0, backoff=10)
    await session.close()
    assert waited == [api.HISTORY_MAX_WAIT]


async def test_service_unavailable_is_treated_like_rate_limit(portal, monkeypatch):
    waited = await _sleeps(monkeypatch)
    portal.fail_month_pages, portal.fail_status = 1, 503
    client, session = make_client()
    await client.async_fetch_history(delay=0, backoff=10)
    await session.close()
    assert waited == [10]


async def test_other_errors_retry_after_short_pause(portal, monkeypatch):
    waited = await _sleeps(monkeypatch)
    portal.fail_month_pages, portal.fail_status = 1, 500
    client, session = make_client()
    await client.async_fetch_history(delay=0, backoff=10)
    await session.close()
    assert waited == [1.0]                            # ne dlouhé čekání jako u 429


async def test_gives_up_after_three_attempts_with_http_error(portal, monkeypatch):
    waited = await _sleeps(monkeypatch)
    portal.fail_month_pages = 99
    client, session = make_client()
    with pytest.raises(api.VhsHttpError) as info:
        await client.async_fetch_history(delay=0, backoff=10)
    await session.close()
    assert info.value.status == 429
    assert waited == [10, 20]                         # po třetím pokusu už nečeká


# ---------------------------------------------------------------------------
# Změna oproti loňsku
# ---------------------------------------------------------------------------


def _months(start, values):
    """Měsíční řada od `start` (rok, měsíc) s danými hodnotami."""
    out = []
    year, month = start
    for value in values:
        out.append(api.DayValue(date(year, month, 1), float(value)))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def test_year_over_year_compares_last_complete_months_with_same_months_a_year_ago():
    # 24 měsíců od 10/2024: loni po 10 m³, letos po 15 m³; poslední (9/2026) je neúplný.
    monthly = _months((2024, 10), [10] * 12 + [15] * 12)
    result = api.year_over_year(monthly, date(2026, 9, 29), months=3)
    assert (result["from"], result["to"]) == (date(2026, 6, 1), date(2026, 8, 1))
    assert result["now_m3"] == 45 and result["then_m3"] == 30
    assert result["change_percent"] == pytest.approx(50.0)


def test_year_over_year_excludes_the_month_of_the_latest_reading():
    monthly = _months((2024, 10), [10] * 12 + [15] * 11 + [999])   # 9/2026 je jen 999
    result = api.year_over_year(monthly, date(2026, 9, 29), months=3)
    assert result["now_m3"] == 45                                    # 999 se nepočítá


def test_year_over_year_across_new_year():
    monthly = _months((2024, 10), [10] * 12 + [20] * 12)
    result = api.year_over_year(monthly, date(2026, 1, 5), months=3)   # 10-12/2025 vs 10-12/2024
    assert (result["from"], result["to"]) == (date(2025, 10, 1), date(2025, 12, 1))
    # 10-12/2025 po 20 m³ (60) proti 10-12/2024 po 10 m³ (30): dvojnásobek.
    assert result["now_m3"] == 60 and result["then_m3"] == 30
    assert result["change_percent"] == pytest.approx(100.0)


def test_year_over_year_needs_last_years_data_and_nonzero_base():
    young = _months((2026, 1), [10] * 9)                             # jen letošek
    assert api.year_over_year(young, date(2026, 9, 29)) is None
    zero = _months((2024, 10), [0] * 12 + [15] * 12)
    assert api.year_over_year(zero, date(2026, 9, 29)) is None       # dělení nulou
    assert api.year_over_year([], date(2026, 9, 29)) is None
    assert api.year_over_year(_months((2024, 10), [1] * 24), None) is None


def test_year_over_year_gap_in_months_gives_none():
    monthly = _months((2024, 10), [10] * 24)
    monthly = [m for m in monthly if m.day != date(2026, 7, 1)]      # chybí červenec 2026
    assert api.year_over_year(monthly, date(2026, 9, 29), months=3) is None


# ---------------------------------------------------------------------------
# Stav z číselníku na hlavní stránce
# ---------------------------------------------------------------------------


def _meter(reading, series_last, last_reading=datetime(2026, 9, 30, 5, 42)):
    return api.MeterData(
        last_reading=last_reading,
        reading_m3=reading,
        daily_index_m3=[api.DayValue(date(2026, 9, 28), 911.28), api.DayValue(date(2026, 9, 29), series_last)],
    )


def test_odometer_value_is_preferred_and_belongs_to_day_of_last_reading():
    data = _meter(reading=912.027, series_last=911.988)        # skutečný případ z 30. 9.
    assert data.index_m3 == 912.027
    assert data.index_day == date(2026, 9, 30)


def test_series_is_used_when_odometer_missing_or_older():
    assert _meter(reading=None, series_last=911.988).index_m3 == 911.988
    assert _meter(reading=None, series_last=911.988).index_day == date(2026, 9, 29)
    stale = _meter(reading=911.5, series_last=911.988)           # číselník by ukázal pokles
    assert stale.index_m3 == 911.988 and stale.index_day == date(2026, 9, 29)


def test_odometer_without_last_reading_time_keeps_series_day():
    data = _meter(reading=912.027, series_last=911.988, last_reading=None)
    assert data.index_m3 == 912.027
    assert data.index_day == date(2026, 9, 29)


@pytest.mark.parametrize("body", [
    "<html>žádný číselník</html>",
    "if (val > 911.69500) val = 123.0;",          # různé hodnoty: nejde o poslední odečet
])
def test_odometer_parser_rejects_garbage(body):
    assert api.parse_meter_reading(body) is None


def test_odometer_parser_reads_integer_and_decimal_values():
    assert api.parse_meter_reading("if (val > 912) val = 912;") == 912.0
    assert api.parse_meter_reading("if (val > 912.02700) val = 912.02700;") == 912.027


def test_format_period_single_month_same_year_and_across_new_year():
    def period(first, last):
        return {"from": first, "to": last}

    assert api.format_period(period(date(2026, 8, 1), date(2026, 8, 1))) == "8/2026 vs 8/2025"
    assert api.format_period(period(date(2026, 6, 1), date(2026, 8, 1))) == "6–8/2026 vs 6–8/2025"
    assert api.format_period(period(date(2025, 11, 1), date(2026, 1, 1))) == "11/2025–1/2026 vs 11/2024–1/2025"
    # Sedí na skutečný výsledek srovnání.
    monthly = _months((2024, 10), [10] * 12 + [15] * 12)
    result = api.year_over_year(monthly, date(2026, 9, 29), months=3)
    assert api.format_period(result) == "6–8/2026 vs 6–8/2025"


def test_parse_retry_after():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    from custom_components.vhs_benesov.api import _parse_retry_after

    assert _parse_retry_after(None) is None
    assert _parse_retry_after("abc") is None
    assert _parse_retry_after("12") == 12.0
    assert _parse_retry_after("-5") == 0.0
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
    assert 80 <= _parse_retry_after(when) <= 90
