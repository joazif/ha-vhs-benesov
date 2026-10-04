"""API klient pro portál dálkových odečtů VHS Benešov (eMIS / Suez Smart Solutions).

Portál je ASP.NET WebForms. Přihlášení je formulář ``Login.aspx`` se skrytými
poli ``__VIEWSTATE`` a ``__EVENTVALIDATION``, potom drží session cookie.
Žádné API ani XHR neexistuje - grafy jsou jqPlot a jejich data jsou vložená
přímo v HTML jako JS pole, takže na každý údaj stačí jeden GET a regex:

    $.jqplot('ctl00_..._CourbeJour_Eau', [[[1, 559, '01.09.2026', '559 l'], ...

Stránky ``Site_Energie.aspx?Affichage=<režim>``:

* ``ConsoJour``  - denní spotřeba v litrech (aktuální měsíc)
* ``ConsoMois``  - měsíční spotřeba v m³
* ``IndexJour``  - stav měřidla v m³ po dnech
* ``CourbeMois`` - spotřeba po 6 hodinách v litrech
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE = "https://cz-sitr.suezsmartsolutions.com/eMIS.SE_VHS-Benesov/"
LOGIN_URL = BASE + "Login.aspx"
HOME_URL = BASE + "Site.aspx"
ENERGY_URL = BASE + "Site_Energie.aspx"
# Hlavička říká, že jde o automat a kde se o něm dozvědět víc (správce portálu ji vidí v logu).
USER_AGENT = (
    "Mozilla/5.0 (compatible; HomeAssistant vhs_benesov; "
    "+https://github.com/joazif/ha-vhs-benesov)"
)
TIMEOUT = aiohttp.ClientTimeout(total=45)

FIELD_LOGIN = "ctl00$PHZonePrincipale$TextBoxIdentifiant"
FIELD_PASSWORD = "ctl00$PHZonePrincipale$TextBoxMotDePasse"
FIELD_BUTTON = "ctl00$PHZonePrincipale$ButtonConnexion"
FIELD_RESOLUTION = "ctl00$PHZonePrincipale$HiddenResolution"

RE_INPUT = re.compile(r"<input\b[^>]*>", re.I)
RE_SELECT = re.compile(r"<select\b([^>]*)>(.*?)</select>", re.S | re.I)
RE_OPTION = re.compile(r"<option\b([^>]*)>", re.I)
RE_ATTR = re.compile(r'([\w:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
RE_PASSWORD_FIELD = re.compile(r'name="ctl00\$PHZonePrincipale\$TextBoxMotDePasse"')
RE_TAG = re.compile(r"<[^>]+>")
RE_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
RE_METER_ID = re.compile(r"\b\d{5}-[A-Z]{2}-\d{7}\b")
# Číselník na hlavní stránce: stav se v JS omezuje na poslední odečet,
# "if (val > 911.69500) val = 911.69500;". To je skutečný poslední stav měřidla.
RE_ODOMETER = re.compile(r"if\s*\(\s*val\s*>\s*([0-9]+(?:\.[0-9]+)?)\s*\)\s*val\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*;")
RE_LAST_READING = re.compile(
    r"Poslední odečet z\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})"
)
RE_JQPLOT = re.compile(r"\$\.jqplot\(\s*'([^']+)'\s*,\s*\[\[")
# Řádek dat s popiskem: [1, 559, '01.09.2026', '559 l']
RE_ROW_LABELLED = re.compile(
    r"\[\s*(\d+)\s*,\s*(-?[\d.]+)\s*,\s*(['\"])(.*?)\3\s*(?:,\s*(['\"])(.*?)\5\s*)?\]"
)
# Řádek křivky: ['2026-09-01 06:00', 165]
RE_ROW_CURVE = re.compile(
    r"\[\s*'(\d{4}-\d{2}-\d{2} \d{2}:\d{2})'\s*,\s*(-?[\d.]+)\s*\]"
)
RE_CZ_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
RE_MONTH_LABEL = re.compile(r"^([^\W\d_]+)\s+(\d{4})$")
RE_INDEX_LINK = re.compile(
    r'href="([^"]*Site_Energie\.aspx\?[^"]*Affichage=IndexJour[^"]*)"', re.I
)

# Stahování historie: pauza mezi požadavky (s náhodnou odchylkou), čekání po
# odmítnutí serverem, počet pokusů a strop čekání.
HISTORY_DELAY = 1.5
HISTORY_BACKOFF = 30.0
HISTORY_ATTEMPTS = 3
HISTORY_MAX_WAIT = 120.0
HISTORY_JITTER = 0.35  # ±35 % kolem HISTORY_DELAY

# Kolik úplných dnů má mít křivka, aby se dalo srovnávat části dne.
MIN_CONTEXT_DAYS = 14

RE_MONTH_SELECT = re.compile(
    r'<select[^>]*name="[^"]*DropDownListSelectionMois"[^>]*>(.*?)</select>', re.S | re.I
)
RE_MONTH_OPTION = re.compile(r'<option[^>]*value="(\d{2})\.(\d{2})\.(\d{4})"', re.I)

CZ_MONTHS = {
    "leden": 1, "únor": 2, "březen": 3, "duben": 4, "květen": 5, "červen": 6,
    "červenec": 7, "srpen": 8, "září": 9, "říjen": 10, "listopad": 11,
    "prosinec": 12,
}


class VhsError(Exception):
    """Obecná chyba komunikace s portálem."""


class VhsHttpError(VhsError):
    """Server odpověděl chybovým HTTP kódem (429, 503, ...)."""

    def __init__(self, message: str, status: int, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class InvalidAuth(VhsError):
    """Neplatné přihlašovací údaje."""


class ParseError(VhsError):
    """Stránka nemá očekávanou podobu."""


@dataclass(slots=True, frozen=True)
class DayValue:
    """Hodnota k jednomu dni (litry spotřeby, nebo stav měřidla v m³)."""

    day: date
    value: float


@dataclass(slots=True, frozen=True)
class PointValue:
    """Hodnota k časovému okamžiku (křivka spotřeby)."""

    at: datetime
    value: float


@dataclass(slots=True)
class MeterData:
    """Vše, co integrace z portálu vytáhne v jednom obnovení."""

    meter_id: str | None = None
    last_reading: datetime | None = None
    daily_liters: list[DayValue] = field(default_factory=list)
    daily_index_m3: list[DayValue] = field(default_factory=list)
    monthly_m3: list[DayValue] = field(default_factory=list)
    curve_liters: list[PointValue] = field(default_factory=list)
    # Stav z číselníku na hlavní stránce (poslední odečet), pokud se ho povedlo přečíst.
    reading_m3: float | None = None

    @property
    def _reading_is_current(self) -> bool:
        """Číselník platí, jen když není starší než denní řada (jinak by stav klesl)."""
        if self.reading_m3 is None:
            return False
        return not self.daily_index_m3 or self.reading_m3 >= self.daily_index_m3[-1].value

    @property
    def index_m3(self) -> float | None:
        """Poslední známý stav měřidla v m³: číselník, jinak poslední bod denní řady."""
        if self._reading_is_current:
            return self.reading_m3
        return self.daily_index_m3[-1].value if self.daily_index_m3 else None

    @property
    def index_day(self) -> date | None:
        """Den, ke kterému stav patří (den posledního odečtu, jinak poslední den řady)."""
        if self._reading_is_current and self.last_reading is not None:
            return self.last_reading.date()
        return self.daily_index_m3[-1].day if self.daily_index_m3 else None

    def liters_on(self, day: date) -> float | None:
        for item in self.daily_liters:
            if item.day == day:
                return item.value
        return None


# --------------------------------------------------------------------------
# Parsery - čisté funkce nad HTML, testují se proti uloženým stránkám.
# --------------------------------------------------------------------------


def parse_hidden_fields(body: str) -> dict[str, str]:
    """Všechna pole ``<input>`` formuláře (skrytá i tlačítka) jako name -> value."""
    fields: dict[str, str] = {}
    for tag in RE_INPUT.findall(body):
        attrs = _attrs(tag)
        name = attrs.get("name")
        if name:
            fields[name] = attrs.get("value", "")

    # Formulář má i <select> (jazyk); bez něj server přihlášení nepřijme.
    for select in RE_SELECT.finditer(body):
        name = _attrs(select.group(1)).get("name")
        if not name:
            continue
        options = [_attrs(o) for o in RE_OPTION.findall(select.group(2))]
        chosen = next((o for o in options if "selected" in o), options[0] if options else None)
        if chosen is not None:
            fields[name] = chosen.get("value", "")
    return fields


def _attrs(tag_body: str) -> dict[str, str]:
    """Atributy tagu; bezhodnotové (``selected``) mají hodnotu prázdný řetězec."""
    attrs = {
        m.group(1).lower(): html_lib.unescape(m.group(2) or m.group(3) or "")
        for m in RE_ATTR.finditer(tag_body)
    }
    for word in re.findall(r"(?<![\w=\"'-])(selected)(?![\w=-])", tag_body, re.I):
        attrs.setdefault(word.lower(), "")
    return attrs


def is_login_page(body: str) -> bool:
    """Přihlašovací stránka poznáme podle pole pro heslo."""
    return RE_PASSWORD_FIELD.search(body) is not None


def page_text(body: str) -> str:
    """Text stránky bez skriptů a značek, s normalizovanými mezerami."""
    text = RE_TAG.sub(" ", RE_SCRIPT_STYLE.sub(" ", body))
    return " ".join(html_lib.unescape(text).replace("\xa0", " ").split())


def parse_meter_id(body: str) -> str | None:
    """Číslo měřidla, např. ``12345-XX-0000001``."""
    match = RE_METER_ID.search(page_text(body))
    return match.group(0) if match else None


def parse_last_reading(body: str) -> datetime | None:
    """Čas posledního odečtu z úvodní stránky ("Poslední odečet z 29.09.2026 17:42")."""
    match = RE_LAST_READING.search(page_text(body))
    if not match:
        return None
    day, month, year, hour, minute = (int(g) for g in match.groups())
    return datetime(year, month, day, hour, minute)


def parse_meter_reading(body: str) -> float | None:
    """Poslední skutečný stav měřidla v m³ z číselníku na hlavní stránce.

    Denní řada stavů (``IndexJour``) končí stavem na konci posledního dne,
    takže je o poslední odečet starší. Číselník ukazuje stejné číslo jako
    portál na první stránce.
    """
    match = RE_ODOMETER.search(body)
    if not match or match.group(1) != match.group(2):
        return None
    return float(match.group(1))


def find_index_url(body: str) -> str | None:
    """Odkaz na stav měřidla po dnech z menu.

    Nese ještě parametry ``IndexesSepares`` a ``PeriodeComplete``, jejichž
    správné hodnoty jsou jen v menu - proto se odkaz bere odtamtud.
    """
    match = RE_INDEX_LINK.search(body)
    if not match:
        return None
    return urljoin(BASE, html_lib.unescape(match.group(1)))


def _jqplot_block(body: str, element_id_part: str | None = None) -> tuple[str, str] | None:
    """(id grafu, text pole dat) prvního grafu, jehož id obsahuje ``element_id_part``."""
    for match in RE_JQPLOT.finditer(body):
        element_id = match.group(1)
        if element_id_part and element_id_part not in element_id:
            continue
        # Pole dat končí párovými závorkami; hledáme uzavření vnějšího [[ ... ]].
        start = match.end() - 2
        depth = 0
        in_str: str | None = None
        for pos in range(start, len(body)):
            char = body[pos]
            if in_str:
                if char == in_str:
                    in_str = None
                continue
            if char in "'\"":
                in_str = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return element_id, body[start : pos + 1]
    return None


def _cz_date(label: str) -> date | None:
    match = RE_CZ_DATE.match(label.strip())
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_day_series(body: str, element_id_part: str | None = None) -> list[DayValue]:
    """Graf s denními hodnotami: popisek je datum ``dd.mm.rrrr``."""
    block = _jqplot_block(body, element_id_part)
    if not block:
        return []
    result: dict[date, float] = {}
    for match in RE_ROW_LABELLED.finditer(block[1]):
        day = _cz_date(match.group(4))
        if day is not None:
            result[day] = float(match.group(2))
    return [DayValue(day, value) for day, value in sorted(result.items())]


def parse_month_series(body: str, element_id_part: str | None = None) -> list[DayValue]:
    """Graf s měsíčními hodnotami: popisek je "říjen 2024", den je první v měsíci."""
    block = _jqplot_block(body, element_id_part)
    if not block:
        return []
    result: dict[date, float] = {}
    for match in RE_ROW_LABELLED.finditer(block[1]):
        label = RE_MONTH_LABEL.match(match.group(4).strip().lower())
        if not label or label.group(1) not in CZ_MONTHS:
            continue
        result[date(int(label.group(2)), CZ_MONTHS[label.group(1)], 1)] = float(
            match.group(2)
        )
    return [DayValue(day, value) for day, value in sorted(result.items())]


def parse_curve(body: str) -> list[PointValue]:
    """Křivka spotřeby po 6 hodinách."""
    block = _jqplot_block(body)
    if not block:
        return []
    return [
        PointValue(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M"), float(m.group(2)))
        for m in RE_ROW_CURVE.finditer(block[1])
    ]


def parse_month_options(body: str) -> list[date]:
    """Měsíce, které portál nabízí ve výběru (první den každého), od nejstaršího."""
    select = RE_MONTH_SELECT.search(body)
    if not select:
        return []
    months = {
        date(int(y), int(m), 1)
        for d, m, y in RE_MONTH_OPTION.findall(select.group(1))
    }
    return sorted(months)


@dataclass(slots=True, frozen=True)
class ReadingProbe:
    """Co ukazuje úvodní stránka: čas posledního odečtu a stav z číselníku."""

    last_reading: datetime | None
    reading_m3: float | None

    @property
    def known(self) -> bool:
        return self.last_reading is not None or self.reading_m3 is not None


@dataclass(slots=True)
class HistoryData:
    """Celá historie z portálu: stavy měřidla na konci dne a křivka po 6 hodinách."""

    index_end: dict[date, float] = field(default_factory=dict)
    curve_liters: dict[datetime, float] = field(default_factory=dict)
    months: int = 0
    first_month: date | None = None
    last_month: date | None = None


DAY_PART_HOURS = (0, 6, 12, 18)


async def _pause(delay: float) -> None:
    """Pauza s náhodnou odchylkou; nulová pauza (testy) nečeká vůbec."""
    if delay > 0:
        await asyncio.sleep(delay * random.uniform(1 - HISTORY_JITTER, 1 + HISTORY_JITTER))


def complete_days(curve: list[PointValue]) -> dict[date, dict[int, float]]:
    """Dny, u kterých portál zná všechny čtyři šestihodinové kroky.

    Klíč vnitřního slovníku je hodina začátku kroku (0, 6, 12, 18).
    Nejnovější den bývá neúplný (portál zveřejňuje jen dokončené kroky).
    """
    days: dict[date, dict[int, float]] = {}
    for point in curve:
        if point.at.hour in DAY_PART_HOURS and point.at.minute == 0:
            days.setdefault(point.at.date(), {})[point.at.hour] = point.value
    return {
        day: parts
        for day, parts in sorted(days.items())
        if all(hour in parts for hour in DAY_PART_HOURS)
    }


def latest_complete_day(curve: list[PointValue]) -> tuple[date, float] | None:
    """Poslední den, který portál zná celý, a jeho spotřeba v litrech (součet kroků)."""
    days = complete_days(curve)
    if not days:
        return None
    day = max(days)
    return day, sum(days[day].values())


def year_over_year(
    monthly: list[DayValue], latest_day: date | None, months: int = 3
) -> dict | None:
    """Spotřeba posledních ``months`` úplných měsíců proti stejným měsícům o rok dřív.

    ``monthly`` jsou měsíční součty v m³ (klíč je první den měsíce). Měsíc,
    ve kterém je poslední odečet (``latest_day``), je neúplný a nepočítá se.
    Bez souvislých měsíců nebo bez loňských dat (nebo s nulovou loňskou
    spotřebou) vrací ``None``.
    """
    if latest_day is None:
        return None
    by_month = {item.day.replace(day=1): item.value for item in monthly}
    current = latest_day.replace(day=1)

    def shift(month: date, back: int) -> date:
        index = month.year * 12 + month.month - 1 - back
        return date(index // 12, index % 12 + 1, 1)

    last = [shift(current, back) for back in range(months, 0, -1)]
    before = [m.replace(year=m.year - 1) for m in last]
    if not all(m in by_month for m in last + before):
        return None
    now_m3 = sum(by_month[m] for m in last)
    then_m3 = sum(by_month[m] for m in before)
    if then_m3 <= 0:
        return None
    return {
        "change_percent": (now_m3 / then_m3 - 1) * 100,
        "now_m3": now_m3,
        "then_m3": then_m3,
        "from": last[0],
        "to": last[-1],
    }


def format_period(result: dict) -> str:
    """Období srovnání čitelně, např. "6–8/2026 vs 6–8/2025" (letos proti loňsku)."""

    def span(first: date, last: date) -> str:
        if first == last:
            return f"{first.month}/{first.year}"
        if first.year == last.year:
            return f"{first.month}–{last.month}/{first.year}"
        return f"{first.month}/{first.year}–{last.month}/{last.year}"

    first, last = result["from"], result["to"]
    prev_first = first.replace(year=first.year - 1)
    prev_last = last.replace(year=last.year - 1)
    return f"{span(first, last)} vs {span(prev_first, prev_last)}"


# --------------------------------------------------------------------------
# Klient
# --------------------------------------------------------------------------


def _parse_retry_after(value: str | None) -> float | None:
    """Hlavička Retry-After: počet sekund, nebo HTTP datum."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class VhsBenesovClient:
    """Klient držící přihlášenou session."""

    def __init__(
        self,
        login: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._login = login
        self._password = password
        self._owns_session = session is None
        self._session = session or aiohttp.ClientSession(
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "cs-CZ,cs;q=0.9"},
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        self._lock = asyncio.Lock()
        self._logged_in = False
        # Cookies spravujeme sami: aiohttp CookieJar autentizační cookie
        # portálu (SE_Pilote_Cookie) nepřijme, prohlížeč ano.
        self._cookies: dict[str, str] = {}
        # Text stránky po posledním neúspěšném přihlášení (pro diagnostiku).
        self.last_login_text = ""

    async def async_close(self) -> None:
        """Zavřít vlastní session."""
        if self._owns_session and not self._session.closed:
            await self._session.close()

    async def async_login(self) -> None:
        """Přihlásit se přes ASP.NET formulář.

        Neúspěšné přihlášení vrací znovu přihlašovací stránku (bez chyby v HTTP),
        proto se úspěch pozná podle toho, že na výsledné stránce už není pole
        pro heslo.
        """
        self._cookies.clear()
        form_page = await self._request("GET", LOGIN_URL, relogin=False)
        payload = parse_hidden_fields(form_page)
        if FIELD_PASSWORD not in payload:
            raise ParseError("Přihlašovací formulář nemá očekávaná pole")

        payload[FIELD_LOGIN] = self._login
        payload[FIELD_PASSWORD] = self._password
        payload.setdefault(FIELD_BUTTON, "Přihlásit")
        # Rozlišení doplňuje na webu JS při odeslání.
        payload[FIELD_RESOLUTION] = payload.get(FIELD_RESOLUTION) or "1920x1080"

        body = await self._request(
            "POST", LOGIN_URL, data=payload, headers={"Referer": LOGIN_URL},
            relogin=False,
        )
        if is_login_page(body):
            self.last_login_text = page_text(body)[:400]
            raise InvalidAuth("Portál přihlášení odmítl")
        self._logged_in = True

    def _absorb_cookies(self, set_cookie_headers: list[str]) -> None:
        """Zpracovat hlavičky ``Set-Cookie`` po jedné, v pořadí, v jakém přišly.

        Portál posílá ``SE_Pilote_Cookie`` dvakrát (nejdřív smazání, pak
        nová hodnota), takže pořadí rozhoduje.
        """
        for header in set_cookie_headers:
            first, *attrs = [part.strip() for part in header.split(";")]
            name, sep, value = first.partition("=")
            name = name.strip()
            if not sep or not name:
                continue
            expired = value == ""
            for attr in attrs:
                key, _, attr_value = attr.partition("=")
                key = key.strip().lower()
                if key == "max-age":
                    expired = expired or attr_value.strip() in ("0", "-1") or (
                        attr_value.strip().lstrip("-").isdigit()
                        and int(attr_value) <= 0
                    )
                elif key == "expires":
                    try:
                        when = parsedate_to_datetime(attr_value.strip())
                    except (TypeError, ValueError):
                        continue
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=UTC)
                    expired = expired or when <= datetime.now(UTC)
            if expired:
                self._cookies.pop(name, None)
            else:
                self._cookies[name] = value

    async def _request(
        self,
        method: str,
        url: str,
        *,
        relogin: bool = True,
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> str:
        """Požadavek s ručním držením cookies a následováním přesměrování."""
        # Smyčka přesměrování přepisuje method/url/headers/kwargs. Po vypršelé
        # session portál přesměruje na Login.aspx?ReturnUrl=...; opakovat se po
        # novém přihlášení musí původní požadavek, ne tenhle - otevření
        # přihlašovací stránky by novou session hned zrušilo.
        orig_method, orig_url = method, url
        orig_headers, orig_kwargs = headers, dict(kwargs)
        body = ""
        try:
            for _ in range(8):
                request_headers = dict(headers or {})
                if self._cookies:
                    request_headers["Cookie"] = "; ".join(
                        f"{k}={v}" for k, v in self._cookies.items()
                    )
                async with self._session.request(
                    method, url, headers=request_headers,
                    allow_redirects=False, **kwargs,
                ) as resp:
                    self._absorb_cookies(resp.headers.getall("Set-Cookie", []))
                    location = resp.headers.get("Location")
                    if resp.status in (301, 302, 303, 307, 308) and location:
                        url = urljoin(str(resp.url), location)
                        if resp.status in (301, 302, 303):
                            method, kwargs = "GET", {}
                            headers = {
                                k: v for k, v in (headers or {}).items()
                                if k.lower() not in ("content-type", "origin")
                            }
                        continue
                    resp.raise_for_status()
                    body = await resp.text()
                    break
            else:
                raise VhsError(f"Příliš mnoho přesměrování u {url}")
        except aiohttp.ClientResponseError as err:
            retry_after = _parse_retry_after((err.headers or {}).get("Retry-After"))
            raise VhsHttpError(
                f"{method} {url} selhal: {err}", err.status, retry_after
            ) from err
        except aiohttp.ClientError as err:
            raise VhsError(f"{method} {url} selhal: {err}") from err

        if relogin and is_login_page(body):
            # Session vypršela; přihlásit znovu a zkusit ještě jednou.
            _LOGGER.debug("Session vypršela, přihlašuji znovu")
            await self.async_login()
            return await self._request(
                orig_method, orig_url, relogin=False, headers=orig_headers, **orig_kwargs
            )
        return body

    async def async_get(self, url: str) -> str:
        """GET stránky za přihlášením; podle potřeby se nejdřív přihlásí."""
        async with self._lock:
            if not self._logged_in:
                await self.async_login()
            return await self._request("GET", url)

    async def async_validate(self) -> str | None:
        """Ověřit údaje (pro config flow) a vrátit číslo měřidla."""
        async with self._lock:
            await self.async_login()
            return parse_meter_id(await self._request("GET", HOME_URL))

    async def async_probe(self) -> ReadingProbe:
        """Lehká kontrola: jen úvodní stránka (jeden požadavek), bez stahování dat."""
        home = await self.async_get(HOME_URL)
        return ReadingProbe(parse_last_reading(home), parse_meter_reading(home))

    async def async_fetch_all(self) -> MeterData:
        """Stáhnout všechny stránky a poskladat z nich ``MeterData``."""
        home = await self.async_get(HOME_URL)
        data = MeterData(
            meter_id=parse_meter_id(home),
            last_reading=parse_last_reading(home),
            reading_m3=parse_meter_reading(home),
        )

        index_url = find_index_url(home) or (
            f"{ENERGY_URL}?Affichage=IndexJour&IndexesSepares=true&PeriodeComplete=false"
        )
        daily = await self.async_get(f"{ENERGY_URL}?Affichage=ConsoJour")
        index = await self.async_get(index_url)
        monthly = await self.async_get(f"{ENERGY_URL}?Affichage=ConsoMois")
        curve = await self.async_get(f"{ENERGY_URL}?Affichage=CourbeMois")

        data.daily_liters = parse_day_series(daily)
        data.daily_liters = await self._with_week_start(data.daily_liters)
        data.daily_index_m3 = await self._with_index_fallback(parse_day_series(index), data)
        data.monthly_m3 = parse_month_series(monthly)
        data.curve_liters = await self._with_curve_context(parse_curve(curve), data)
        data.meter_id = data.meter_id or parse_meter_id(daily)

        if not data.daily_index_m3:
            raise ParseError("Ze stránky se stavem měřidla se nepodařilo přečíst data")
        return data

    async def _with_index_fallback(
        self, index: list[DayValue], data: MeterData
    ) -> list[DayValue]:
        """Když aktuální měsíc ještě nemá žádný stav, vzít poslední stavy z předchozího měsíce.

        Nový měsíc se v řadě stavů objeví až po zveřejnění prvního odečtu (hodiny po půlnoci),
        do té doby by bylo načtení dat chybou a senzory by byly nedostupné.
        """
        if index:
            return index
        # Měsíc posledního odečtu; bez něj měsíc před dneškem.
        if data.last_reading:
            month = data.last_reading.date().replace(day=1)
        else:
            month = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
        try:
            body = await self.async_get(
                f"{ENERGY_URL}?Affichage=IndexJour&IndexesSepares=True&PeriodeComplete=False"
                f"&Annee={month.year}&Mois={month.month}"
            )
        except VhsError as err:
            _LOGGER.debug("Stavy za %s se nestáhly: %s", month, err)
            return index
        return parse_day_series(body)

    async def _with_curve_context(
        self, curve: list[PointValue], data: MeterData
    ) -> list[PointValue]:
        """Doplnit křivku z předchozího měsíce, když je úplných dnů málo.

        Části dne se srovnávají s průměrem posledních dnů; na začátku měsíce by
        z aktuálního měsíce nebyl žádný smysluplný průměr.
        """
        if len(complete_days(curve)) >= MIN_CONTEXT_DAYS:
            return curve
        latest = max(
            [p.at.date() for p in curve] + ([data.index_day] if data.index_day else []),
            default=None,
        )
        if latest is None:
            return curve
        this_month = latest.replace(day=1)
        months = [this_month, (this_month - timedelta(days=1)).replace(day=1)]
        have_month = curve[0].at.date().replace(day=1) if curve else None
        merged = {p.at: p for p in curve}
        for month in months:
            if month == have_month:
                continue
            try:
                body = await self.async_get(
                    f"{ENERGY_URL}?Affichage=CourbeMois&Annee={month.year}&Mois={month.month}"
                )
            except VhsError as err:
                _LOGGER.debug("Křivka za %s se nestáhla: %s", month, err)
                continue
            for point in parse_curve(body):
                merged.setdefault(point.at, point)
        return [merged[at] for at in sorted(merged)]

    async def _with_week_start(self, daily: list[DayValue]) -> list[DayValue]:
        """Doplnit předchozí měsíc, když týden začal dřív než aktuální měsíc.

        Denní stránka ukazuje jen aktuální měsíc; na začátku měsíce by proto
        pondělí týdne (např. 31. 8. při úterý 1. 9.) chybělo. "Dnes" se bere
        z dat portálu (nejnovější den), ne z hodin počítače.
        """
        if not daily:
            return daily
        latest = daily[-1].day
        monday = latest - timedelta(days=latest.weekday())
        month_start = latest.replace(day=1)
        if monday >= month_start:
            return daily
        prev = monday.replace(day=1)
        try:
            body = await self.async_get(
                f"{ENERGY_URL}?Affichage=ConsoJour&Annee={prev.year}&Mois={prev.month}"
            )
        except VhsError as err:
            _LOGGER.debug("Předchozí měsíc pro týdenní spotřebu se nestáhl: %s", err)
            return daily
        merged = {item.day: item for item in parse_day_series(body)}
        merged.update({item.day: item for item in daily})
        return [merged[day] for day in sorted(merged)]

    async def async_fetch_history(
        self,
        *,
        delay: float = HISTORY_DELAY,
        backoff: float = HISTORY_BACKOFF,
        progress: Callable[[int, int], None] | None = None,
        only_months: list[date] | None = None,
    ) -> HistoryData:
        """Stáhnout všechny měsíce, které portál nabízí (řádově desítky měsíců).

        Na měsíc jsou to dvě stránky (stav měřidla po dnech a křivka po
        6 hodinách). Požadavky jdou jeden po druhém s pauzou ``delay`` sekund
        a malou náhodnou odchylkou, aby portál nezatěžovaly ani nevypadaly
        jako robot v pravidelném rytmu. Když server odpoví 429 nebo 503,
        čeká se ``backoff`` sekund (nebo podle ``Retry-After``) a zkusí se to
        znovu. Selhání jednoho měsíce se zopakuje; když selže i potom, celé
        stažení skončí chybou, aby se nezapsala historie s dírou. S ``only_months`` (první
        dny měsíců) se stáhnou jen ty měsíce, například při doplnění dnů před aktuální měsíc.
        """
        if only_months:
            months = sorted({m.replace(day=1) for m in only_months})
        else:
            picker = await self.async_get(f"{ENERGY_URL}?Affichage=ConsoJour")
            months = parse_month_options(picker)
        if not months:
            raise ParseError("Portál nenabízí výběr měsíců, historii nelze stáhnout")

        history = HistoryData(
            months=len(months), first_month=months[0], last_month=months[-1]
        )
        for done, month in enumerate(months, start=1):
            period = f"&Annee={month.year}&Mois={month.month}"
            index_url = (
                f"{ENERGY_URL}?Affichage=IndexJour&IndexesSepares=True"
                f"&PeriodeComplete=False{period}"
            )
            curve_url = f"{ENERGY_URL}?Affichage=CourbeMois{period}"
            await _pause(delay)
            index_body = await self._get_with_retry(index_url, delay, backoff)
            await _pause(delay)
            curve_body = await self._get_with_retry(curve_url, delay, backoff)

            for item in parse_day_series(index_body):
                history.index_end[item.day] = item.value
            for point in parse_curve(curve_body):
                history.curve_liters[point.at] = point.value

            if progress:
                progress(done, len(months))
        return history

    async def _get_with_retry(self, url: str, delay: float, backoff: float) -> str:
        """GET s opakováním: 429/503 čekají dlouho, jiné chyby krátce."""
        last: VhsError | None = None
        for attempt in range(HISTORY_ATTEMPTS):
            try:
                return await self.async_get(url)
            except InvalidAuth:
                raise
            except VhsError as err:
                last = err
                if attempt == HISTORY_ATTEMPTS - 1:
                    break
                if isinstance(err, VhsHttpError) and err.status in (429, 503):
                    wait = err.retry_after if err.retry_after is not None else backoff * (attempt + 1)
                    wait = min(wait, HISTORY_MAX_WAIT)
                else:
                    wait = max(delay, 1.0)
                _LOGGER.debug("Stažení %s selhalo (%s), čekám %.0f s", url, err, wait)
                await asyncio.sleep(wait)
        assert last is not None
        raise last
