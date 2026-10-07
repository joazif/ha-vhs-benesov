"""Externí statistika spotřeby vody: celá historie z portálu a průběžné doplňování.

Statistika senzoru "Aktuální stav vodoměru" počítá Home Assistant ze změn jeho stavu. Stav se
ale mění, až když portál zveřejní nový odečet (se zpožděním hodin až půl dne), takže by se
spotřeba zapsala do hodiny, kdy ji HA uviděl, a ne kdy voda tekla. Proto zapisujeme vlastní
externí statistiku ``vhs_benesov:<měřidlo>_consumption``: hodinové řádky ze stavů měřidla a
šestihodinové křivky, každý u hodiny, do které spotřeba patří.

* Při prvním spuštění (nebo na tlačítko) se stáhne celá historie z portálu.
* Potom se po každé změně dat z portálu přepočítají jen poslední dny a zapíšou znovu; HA
  řádky se stejným začátkem přepíše. Součet navazuje na uložený kontrolní bod (viz
  ``history.Checkpoint``), takže se nemusí číst databáze a nevznikne záporný přírůstek.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import MeterData, VhsError
from .const import CONF_IMPORT_HISTORY, DOMAIN
from .history import Checkpoint, SeriesUpdate, series_update

if TYPE_CHECKING:
    from .coordinator import VhsBenesovCoordinator

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
CHUNK = 5000  # hodinových řádků na jednu dávku do rekordéru
# Ochrana portálu před opakovaným spouštěním (tlačítko, automatizace, restarty):
# po úspěšném importu se další povolí až za den, po neúspěšném za čtvrt hodiny.
COOLDOWN_AFTER_SUCCESS = timedelta(hours=24)
COOLDOWN_AFTER_FAILURE = timedelta(minutes=15)
# Automatické opakování po neúspěšném importu (při každé další kontrole portálu) je řidší,
# ať se nedotazuje pořád dokola, když portál nejede. Po startu platí čtvrthodina.
AUTO_RETRY_AFTER_FAILURE = timedelta(hours=6)
# Nejvýš tolik měsíců před aktuální se při doplňování dotáhne z portálu; delší výpadek se
# řeší novým importem celé historie.
MAX_CATCH_UP_MONTHS = 3
STATISTIC_NAME = "Spotřeba vody (VHS Benešov)"


def statistic_id_for(meter_id: str | None, entry_id: str) -> str:
    """``vhs_benesov:12345_xx_0000001_consumption`` (malá písmena, číslice a podtržítka)."""
    slug = re.sub(r"[^a-z0-9]+", "_", (meter_id or entry_id).lower()).strip("_")
    return f"{DOMAIN}:{slug}_consumption"


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
    # Kdy import naposled doběhl (místní čas, "04.10.2026 09:12"); None dokud neproběhl.
    finished: str | None = None
    # Do kdy je statistika zapsaná (poslední hodina, místní čas).
    written_to: str | None = None

    @property
    def percent(self) -> int | None:
        return round(self.done / self.total * 100) if self.total else None


def _done_state(finished: str | None) -> str:
    """Stav po dokončení; s datem a časem, aby bylo vidět, kdy historie naposled doběhla."""
    return f"hotovo ({finished})" if finished else "hotovo"


def _checkpoint_from(data: dict[str, Any]) -> Checkpoint | None:
    try:
        return Checkpoint(
            date.fromisoformat(data["series_day"]),
            float(data["series_state"]),
            float(data["series_total"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _checkpoint_to(checkpoint: Checkpoint) -> dict[str, Any]:
    return {
        "series_day": checkpoint.day.isoformat(),
        "series_state": checkpoint.state,
        "series_total": checkpoint.total,
    }


class HistoryImporter:
    """Import celé historie (jednorázově a na požádání) a průběžné doplňování statistiky."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, coordinator: VhsBenesovCoordinator
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._coordinator = coordinator
        self._lock = asyncio.Lock()
        self._series_lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.history.{entry_id}"
        )
        self._signature: tuple | None = None
        # Kdy integrace poprvé uviděla poslední odečet; z rozdílu proti času odečtu je vidět, jak
        # velké zpoždění portál zrovna má. None, dokud se změna odečtu nepozorovala.
        self.reading_seen_at: datetime | None = None
        self._seen_reading: datetime | None = None
        self.status = HistoryStatus()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    @property
    def statistic_id(self) -> str:
        data = self._coordinator.data
        return statistic_id_for(data.meter_id if data else None, self._entry_id)

    async def async_restore(self) -> None:
        """Po restartu HA ukázat, že historie už je stažená (stav není v paměti)."""
        data = await self._store.async_load() or {}
        self._restore_seen_reading(data)
        if data.get("done") and _checkpoint_from(data) is not None:
            # Starší záznam čas dokončení nemá; nejblíž je začátek posledního běhu.
            finished = self._format_time(data.get("finished") or data.get("last_run"))
            self.status = HistoryStatus(
                state=_done_state(finished),
                first=data.get("first"),
                last=data.get("last"),
                rows=int(data.get("rows", 0)),
                finished=finished,
                written_to=self._format_time(data.get("written_to")),
            )

    def _restore_seen_reading(self, data: dict[str, Any]) -> None:
        try:
            self._seen_reading = datetime.fromisoformat(data["seen_reading"])
        except (KeyError, TypeError, ValueError):
            self._seen_reading = None
        seen_at = data.get("seen_at")
        self.reading_seen_at = datetime.fromtimestamp(seen_at, UTC) if seen_at else None

    async def _track_reading(self, data: MeterData) -> None:
        """Zapamatovat, kdy integrace poprvé uviděla nový odečet (první odečet nemá čas znám)."""
        reading = data.last_reading
        if reading is None or reading == self._seen_reading:
            return
        previous = self._seen_reading
        self._seen_reading = reading
        self.reading_seen_at = self._now() if previous is not None else None
        await self._merge_save(
            {
                "seen_reading": reading.isoformat(),
                "seen_at": self.reading_seen_at.timestamp() if self.reading_seen_at else None,
            }
        )
        self._coordinator.async_update_listeners()

    async def async_remove_store(self) -> None:
        await self._store.async_remove()

    def _set(self, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(self.status, key, value)
        self._coordinator.async_update_listeners()

    @staticmethod
    def _format_time(timestamp: float | None) -> str | None:
        if timestamp is None:
            return None
        local = dt_util.as_local(datetime.fromtimestamp(timestamp, UTC))
        return local.strftime("%d.%m.%Y %H:%M")

    async def _merge_save(self, fields: dict[str, Any]) -> None:
        """Uložit pole do záznamu, aniž by se smazala ostatní (např. čas posledního běhu)."""
        data = await self._store.async_load() or {}
        data.update(fields)
        await self._store.async_save(data)

    def _now(self) -> datetime:
        return dt_util.utcnow()

    async def _cooldown_left(
        self, failure_wait: timedelta = COOLDOWN_AFTER_FAILURE
    ) -> timedelta | None:
        """Kolik zbývá do dalšího povoleného importu; None když se smí hned."""
        data = await self._store.async_load() or {}
        started = data.get("last_run")
        if started is None:
            return None
        wait = COOLDOWN_AFTER_SUCCESS if data.get("last_ok") else failure_wait
        left = datetime.fromtimestamp(started, UTC) + wait - self._now()
        return left if left > timedelta(0) else None

    async def _mark_run(self, *, ok: bool) -> None:
        """Zapamatovat začátek (ok=False) a výsledek (ok=True) posledního importu."""
        data = await self._store.async_load() or {}
        if not ok:
            data["last_run"] = self._now().timestamp()
        data["last_ok"] = ok
        await self._store.async_save(data)

    # ------------------------------------------------------------------ celý import

    async def async_import(
        self, *, automatic: bool = False, failure_wait: timedelta = COOLDOWN_AFTER_FAILURE
    ) -> None:
        """Stáhnout historii z portálu a zapsat ji do statistiky. Bezpečné opakovat.

        Import je pro portál zátěž (řádově stovka požadavků), proto se nespustí
        znovu, dokud běží předchozí, ani dřív než po odpočinku (den po úspěchu,
        čtvrt hodiny po chybě). Chrání to před opakovaným mačkáním tlačítka
        i automatizací, která by ho spouštěla dokola. Automatické spuštění (první
        naplnění, doplnění po výpadku) při odpočinku jen počká na další kontrolu.
        """
        if self._lock.locked():
            _LOGGER.debug("Import historie už běží")
            return
        async with self._lock:
            left = await self._cooldown_left(failure_wait)
            if left is not None:
                minutes = max(1, round(left.total_seconds() / 60))
                wait = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"
                _LOGGER.info("Import historie odmítnut, další je možný za %s", wait)
                if not automatic:
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
        instance = get_instance(self._hass)
        await instance.async_db_connected

        self._set(state="stahuji z portálu", done=0, total=0, detail=None)
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
        update: SeriesUpdate = await self._hass.async_add_executor_job(
            series_update, history.index_end, history.curve_liters, tz, None
        )
        finished_at = self._now().timestamp()
        finished = self._format_time(finished_at)
        if not update.rows:
            self._set(
                state=_done_state(finished), rows=0, finished=finished,
                detail="portál neměl žádná data",
            )
            await self._merge_save({"done": False, "finished": finished_at})
            return

        await self._write(update.rows)
        self._signature = None            # další změna dat se zapíše hned
        first = dt_util.as_local(update.rows[0]["start"]).strftime("%d.%m.%Y")
        last = dt_util.as_local(update.rows[-1]["start"]).strftime("%d.%m.%Y")
        saved: dict[str, Any] = {
            "done": True, "finished": finished_at, "first": first, "last": last,
            "rows": len(update.rows),
            "written_to": update.rows[-1]["start"].timestamp(),
        }
        if update.checkpoint is not None:
            saved.update(_checkpoint_to(update.checkpoint))
        else:
            # Žádný den ještě není konečný: řada se pak postaví znovu z dat portálu.
            for key in ("series_day", "series_state", "series_total"):
                saved[key] = None
        await self._merge_save(saved)
        self._set(
            state=_done_state(finished), first=first, last=last, rows=len(update.rows),
            finished=finished, detail=None,
            written_to=self._format_time(saved["written_to"]),
        )
        _LOGGER.info(
            "Statistika %s: %s hodinových řádků, %s až %s",
            self.statistic_id, len(update.rows), first, last,
        )
        self._notify(
            f"Historie spotřeby vody je načtená: {len(update.rows)} hodinových záznamů "
            f"od {first} do {last}. Pro grafy a Energy dashboard použij statistiku "
            f"**{self.statistic_id}** ({STATISTIC_NAME})."
        )

    # ------------------------------------------------- průběžné doplňování po změně dat

    async def async_on_update(self, data: MeterData, *, startup: bool = False) -> None:
        """Po každém úspěšném načtení dat: naplnit statistiku, nebo ji doplnit o nové dny.

        Data se předávají výslovně: úkol se spouští dřív, než je koordinátor uloží do ``data``.
        """
        try:
            await self._track_reading(data)
        except Exception:  # noqa: BLE001 - pomocný údaj nesmí zastavit doplňování statistiky
            _LOGGER.exception("Zápis času zveřejnění odečtu selhal")
        if self.running:
            return
        try:
            stored = await self._store.async_load() or {}
            # "written_to" je jen v záznamu nové verze; starší záznam ho nemá a import se opakuje.
            if _checkpoint_from(stored) is None and "written_to" not in stored:
                options = self._coordinator.config_entry.options
                if options.get(CONF_IMPORT_HISTORY) or stored.get("done"):
                    # Celá historie: první naplnění, nebo nahrazení starého zápisu do statistiky
                    # senzoru. Při odpočinku počká na další kontrolu (za hodinu).
                    await self.async_import(
                        automatic=True,
                        failure_wait=COOLDOWN_AFTER_FAILURE if startup else AUTO_RETRY_AFTER_FAILURE,
                    )
                    return
            async with self._series_lock:
                await self._async_update_series(data)
        except Exception:  # noqa: BLE001 - úkol na pozadí nesmí skončit potichu
            _LOGGER.exception("Doplnění statistiky spotřeby selhalo")

    @staticmethod
    def _signature_of(data: MeterData) -> tuple:
        last_step = max((p.at for p in data.curve_liters), default=None)
        return (data.last_reading, data.reading_m3, last_step, data.index_day)

    async def _async_update_series(self, data: MeterData) -> None:
        signature = self._signature_of(data)
        if signature == self._signature:
            return                                 # od posledního zápisu se nic nezměnilo
        stored = await self._store.async_load() or {}
        checkpoint = _checkpoint_from(stored)
        index = {item.day: item.value for item in data.daily_index_m3}
        curve = {point.at: point.value for point in data.curve_liters}

        if checkpoint is not None and index:
            window = min(index)
            months = self._missing_months(checkpoint.day, window) if checkpoint.day < window else []
            if months:
                if len(months) > MAX_CATCH_UP_MONTHS:
                    _LOGGER.info("Statistika zaostává o %s měsíců, stahuji celou historii", len(months))
                    await self.async_import(automatic=True)
                    return
                try:
                    extra = await self._coordinator.client.async_fetch_history(only_months=months)
                except VhsError as err:
                    _LOGGER.debug("Doplnění starších dní se nepovedlo, zkusím příště: %s", err)
                    return
                index = {**extra.index_end, **index}
                curve = {**extra.curve_liters, **curve}

        tz = dt_util.get_default_time_zone()
        update: SeriesUpdate = await self._hass.async_add_executor_job(
            series_update, index, curve, tz, checkpoint
        )
        if update.rows:
            await self._write(update.rows)
            saved: dict[str, Any] = {"written_to": update.rows[-1]["start"].timestamp()}
            if update.checkpoint is not None:
                saved.update(_checkpoint_to(update.checkpoint))
                if checkpoint is None:
                    saved["done"] = True
                    saved["first"] = dt_util.as_local(update.rows[0]["start"]).strftime("%d.%m.%Y")
            await self._merge_save(saved)
            self._set(
                last=dt_util.as_local(update.rows[-1]["start"]).strftime("%d.%m.%Y"),
                written_to=self._format_time(saved["written_to"]),
            )
        self._signature = signature

    @staticmethod
    def _missing_months(from_day: date, window_start: date) -> list[date]:
        months: list[date] = []
        month = from_day.replace(day=1)
        while month < window_start.replace(day=1):
            months.append(month)
            month = (month + timedelta(days=32)).replace(day=1)
        return months

    # ------------------------------------------------------------------ zápis

    def _metadata(self) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=STATISTIC_NAME,
            source=DOMAIN,
            statistic_id=self.statistic_id,
            unit_class="volume",
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )

    async def _write(self, rows: list[dict]) -> None:
        metadata = self._metadata()
        for i in range(0, len(rows), CHUNK):
            chunk = [
                StatisticData(start=r["start"], state=r["state"], sum=r["sum"])
                for r in rows[i : i + CHUNK]
            ]
            async_add_external_statistics(self._hass, metadata, chunk)
            await asyncio.sleep(0)  # dát rekordéru prostor

    def _notify(self, message: str) -> None:
        persistent_notification.async_create(
            self._hass,
            message,
            title="VHS Benešov: historie spotřeby",
            notification_id=f"{DOMAIN}_history_{self._entry_id}",
        )
