"""Import historie spotřeby do dlouhodobých statistik senzoru "Aktuální stav vodoměru".

Statistiky senzoru začínají dnem instalace. Historii z portálu zapisujeme
před ně jako hodinové statistiky se stejným ``statistic_id`` (= entity_id
senzoru), takže Energy dashboard i grafy ukážou celý vývoj v čase.

Součet (``sum``) je nutné ukotvit. Živá data ho počítají od nuly v okamžiku,
kdy senzor poprvé zapíše stav; historie proto končí součtem
``stav - kotva`` (záporným nebo nulovým) a živá data na ni navážou bez skoku.
Kotva a hranice mezi historií a živými daty se při prvním importu uloží,
opakovaný import (tlačítko) tak dá vždy stejné hodnoty a živá data nepřepíše.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import VhsError
from .const import DOMAIN
from .history import build_hourly, flat_rows, to_statistics

if TYPE_CHECKING:
    from .coordinator import VhsBenesovCoordinator

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
CHUNK = 5000  # hodinových řádků na jednu dávku do rekordéru
# Ochrana portálu před opakovaným spouštěním (tlačítko, automatizace, restarty):
# po úspěšném importu se další povolí až za den, po neúspěšném za čtvrt hodiny.
COOLDOWN_AFTER_SUCCESS = timedelta(hours=24)
COOLDOWN_AFTER_FAILURE = timedelta(minutes=15)
SENSOR_KEY = "meter_index"


@dataclass(slots=True)
class HistoryStatus:
    """Stav importu pro atribut diagnostického senzoru."""

    state: str = "nespuštěno"
    first: str | None = None
    last: str | None = None
    rows: int = 0
    detail: str | None = None
    # Postup stahování po měsících (0 mimo stahování).
    done: int = 0
    total: int = 0

    @property
    def percent(self) -> int | None:
        return round(self.done / self.total * 100) if self.total else None


class HistoryImporter:
    """Jednorázový (a na požádání opakovaný) import historie."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, coordinator: VhsBenesovCoordinator
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._coordinator = coordinator
        self._lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.history.{entry_id}"
        )
        self.status = HistoryStatus()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def async_is_done(self) -> bool:
        data = await self._store.async_load() or {}
        return bool(data.get("done"))

    async def async_restore(self) -> None:
        """Po restartu HA ukázat, že historie už je stažená (stav není v paměti)."""
        data = await self._store.async_load() or {}
        if data.get("done"):
            self.status = HistoryStatus(
                state="hotovo",
                first=data.get("first"),
                last=data.get("last"),
                rows=int(data.get("rows", 0)),
            )

    async def async_remove_store(self) -> None:
        await self._store.async_remove()

    def _set(self, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(self.status, key, value)
        self._coordinator.async_update_listeners()

    def _entity_id(self) -> str | None:
        return er.async_get(self._hass).async_get_entity_id(
            "sensor", DOMAIN, f"{self._entry_id}_{SENSOR_KEY}"
        )

    async def _merge_save(self, fields: dict[str, Any]) -> None:
        """Uložit pole do záznamu, aniž by se smazala ostatní (např. čas posledního běhu)."""
        data = await self._store.async_load() or {}
        data.update(fields)
        await self._store.async_save(data)

    def _now(self) -> datetime:
        return dt_util.utcnow()

    async def _cooldown_left(self) -> timedelta | None:
        """Kolik zbývá do dalšího povoleného importu; None když se smí hned."""
        data = await self._store.async_load() or {}
        started = data.get("last_run")
        if started is None:
            return None
        wait = COOLDOWN_AFTER_SUCCESS if data.get("last_ok") else COOLDOWN_AFTER_FAILURE
        left = datetime.fromtimestamp(started, UTC) + wait - self._now()
        return left if left > timedelta(0) else None

    async def _mark_run(self, *, ok: bool) -> None:
        """Zapamatovat začátek (ok=False) a výsledek (ok=True) posledního importu."""
        data = await self._store.async_load() or {}
        if not ok:
            data["last_run"] = self._now().timestamp()
        data["last_ok"] = ok
        await self._store.async_save(data)

    async def async_import(self) -> None:
        """Stáhnout historii z portálu a zapsat ji do statistik. Bezpečné opakovat.

        Import je pro portál zátěž (řádově stovka požadavků), proto se nespustí
        znovu, dokud běží předchozí, ani dřív než po odpočinku (den po úspěchu,
        čtvrt hodiny po chybě). Chrání to před opakovaným mačkáním tlačítka
        i automatizací, která by ho spouštěla dokola.
        """
        if self._lock.locked():
            _LOGGER.debug("Import historie už běží")
            return
        async with self._lock:
            left = await self._cooldown_left()
            if left is not None:
                minutes = max(1, round(left.total_seconds() / 60))
                wait = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"
                _LOGGER.info("Import historie odmítnut, další je možný za %s", wait)
                self._notify(
                    "Historii jsem stahoval nedávno, portál nechci zatěžovat "
                    f"opakovaně. Další stažení bude možné za {wait}."
                )
                return
            await self._mark_run(ok=False)
            try:
                await self._async_run()
                await self._mark_run(ok=True)
            except VhsError as err:
                _LOGGER.warning("Import historie selhal: %s", err)
                self._set(state="chyba", detail=str(err), done=0, total=0)
                self._notify(
                    f"Stažení historie se nepodařilo: {err}\n\n"
                    "Zkuste to znovu tlačítkem „Stáhnout historii“ (za čtvrt hodiny)."
                )
            except Exception as err:  # noqa: BLE001 - nesmí shodit background task potichu
                _LOGGER.exception("Import historie selhal neočekávaně")
                self._set(state="chyba", detail=str(err), done=0, total=0)
                self._notify(f"Import historie selhal: {err}")

    async def _async_run(self) -> None:
        entity_id = self._entity_id()
        if entity_id is None:
            raise VhsError("Senzor Aktuální stav vodoměru ještě nemá entitu")

        instance = get_instance(self._hass)
        await instance.async_db_connected

        self._set(state="zjišťuji hranici", detail=None)
        live_from, anchor = await self._async_anchor(instance, entity_id)

        self._set(state="stahuji z portálu", done=0, total=0)
        self._notify(
            "Stahuji historii spotřeby z portálu, trvá to několik minut. "
            "Průběh ukazuje senzor **Stav historie** (Diagnostika)."
        )

        def progress(done: int, total: int) -> None:
            self._set(
                state=f"stahuji z portálu ({done}/{total} měsíců)", done=done, total=total
            )

        history = await self._coordinator.client.async_fetch_history(progress=progress)

        self._set(state="zapisuji do statistik", done=0, total=0)
        tz = dt_util.get_default_time_zone()
        rows = await self._hass.async_add_executor_job(
            lambda: build_hourly(history.index_end, history.curve_liters, tz, before=live_from)
        )
        stats = to_statistics(rows, anchor)
        if stats and len(stats) < len(rows):
            # Konec historie byl nad kotvou a odřízl se. Hodiny do živých dat se přepíšou
            # plochými řádky, aby po starších verzích nezůstaly řádky s chybným součtem.
            stats += flat_rows(stats[-1]["start"], live_from, anchor)
        if not stats:
            self._set(state="hotovo", rows=0, detail="portál neměl žádná data")
            await self._merge_save(self._saved(live_from, anchor, done=True))
            return

        metadata = await self._async_metadata(instance, entity_id)
        for i in range(0, len(stats), CHUNK):
            async_import_statistics(self._hass, metadata, stats[i : i + CHUNK])
            await asyncio.sleep(0)  # dát rekordéru prostor

        first = dt_util.as_local(stats[0]["start"]).strftime("%d.%m.%Y")
        last = dt_util.as_local(stats[-1]["start"]).strftime("%d.%m.%Y")
        await self._merge_save(
            self._saved(live_from, anchor, done=True, first=first, last=last, rows=len(stats))
        )
        self._set(state="hotovo", first=first, last=last, rows=len(stats), detail=None)
        _LOGGER.info("Historie vodoměru: %s hodinových řádků, %s až %s", len(stats), first, last)
        self._notify(
            f"Historie spotřeby vody je načtená: {len(stats)} hodinových záznamů "
            f"od {first} do {last}. Graf v Energy dashboardu ukáže celý vývoj."
        )

    def _saved(
        self, live_from: datetime, anchor: float, *, done: bool, **extra: Any
    ) -> dict[str, Any]:
        return {"done": done, "live_from": live_from.timestamp(), "anchor": anchor, **extra}

    async def _async_anchor(self, instance, entity_id: str) -> tuple[datetime, float]:
        """Hranice historie a kotva součtu; při prvním importu se uloží."""
        saved = await self._store.async_load() or {}
        if "live_from" in saved and "anchor" in saved:
            return datetime.fromtimestamp(saved["live_from"], UTC), float(saved["anchor"])

        rows = await instance.async_add_executor_job(
            statistics_during_period,
            self._hass,
            datetime(2000, 1, 1, tzinfo=UTC),
            None,
            {entity_id},
            "hour",
            # Bez tohoto by rekordér hodnoty převedl na zobrazovací jednotku entity (např.
            # litry) a kotva součtu by vyšla tisíckrát větší než stav, který zapisujeme.
            {"volume": UnitOfVolume.CUBIC_METERS},
            {"state", "sum"},
        )
        existing = rows.get(entity_id) or []
        if existing:
            # Senzor už statistiky zapisuje (integrace běží dřív): historie
            # končí těsně před nimi a kotva vyplyne z prvního živého řádku.
            first = existing[0]
            live_from = datetime.fromtimestamp(first["start"], UTC)
            anchor = float(first["state"]) - float(first["sum"])
        else:
            # Kotva je poslední stav, který portál zveřejnil (čas posledního odečtu), ne
            # skutečný stav "teď": portál data zveřejňuje se zpožděním hodin až půl dne.
            # Historie proto musí končit u kotvy (viz history.to_statistics), spotřeba
            # od kotvy po dnešek se připíše, jakmile ji portál zveřejní.
            live_from = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
            index = self._coordinator.data.index_m3
            if index is None:
                raise VhsError("Stav vodoměru není znám")
            anchor = float(index)
        await self._merge_save(self._saved(live_from, anchor, done=False))
        return live_from, anchor

    async def _async_metadata(self, instance, entity_id: str) -> StatisticMetaData:
        # Čtení z databáze rekordéru je blokující, proto mimo smyčku událostí.
        existing = await instance.async_add_executor_job(
            lambda: get_metadata(self._hass, statistic_ids={entity_id})
        )
        if entity_id in existing:
            return existing[entity_id][1]
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class="volume",
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )

    def _notify(self, message: str) -> None:
        persistent_notification.async_create(
            self._hass,
            message,
            title="VHS Benešov: historie spotřeby",
            notification_id=f"{DOMAIN}_history_{self._entry_id}",
        )
