"""Senzory vodoměru."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import VhsBenesovConfigEntry
from .api import (
    DAY_PART_HOURS,
    MeterData,
    complete_days,
    format_period,
    latest_complete_day,
    year_over_year,
)
from .const import CONF_COMPARE_MONTHS, DEFAULT_COMPARE_MONTHS
from .coordinator import CHECK_INTERVAL, VhsBenesovCoordinator
from .entity import VhsBenesovEntity

PARALLEL_UPDATES = 0


def _today() -> date:
    return dt_util.now().date()


def _last_complete_day_l(data: MeterData) -> float | None:
    """Spotřeba posledního dne, který portál zná celý (všechny čtyři kroky)."""
    found = latest_complete_day(data.curve_liters)
    return found[1] if found else None


def _last_complete_day_date(data: MeterData) -> date | None:
    """Den, ke kterému patří spotřeba za poslední úplný den a části dne."""
    found = latest_complete_day(data.curve_liters)
    return found[0] if found else None


def _year_over_year(data: MeterData, months: int) -> dict | None:
    return year_over_year(data.monthly_m3, data.index_day, months)


def _change_vs_last_year(data: MeterData, months: int) -> float | None:
    result = _year_over_year(data, months)
    return round(result["change_percent"], 1) if result else None


def _compare_period(data: MeterData, months: int) -> str | None:
    result = _year_over_year(data, months)
    return format_period(result) if result else None


def _change_m3(data: MeterData, months: int) -> float | None:
    result = _year_over_year(data, months)
    return round(result["now_m3"] - result["then_m3"], 3) if result else None


def _this_week_m3(data: MeterData) -> float | None:
    """Spotřeba od pondělí do dneška v m³ (z denních hodnot v litrech)."""
    today = _today()
    monday = today - timedelta(days=today.weekday())
    days = [d.value for d in data.daily_liters if monday <= d.day <= today]
    if days:
        return round(sum(days) / 1000, 3)
    # Portál nový den zveřejní se zpožděním (v pondělí ráno ještě chybí). Týden právě začal,
    # takže nula, ale jen když portál jinak data má.
    return 0.0 if data.daily_liters else None


def _this_month_m3(data: MeterData) -> float | None:
    first = _today().replace(day=1)
    for item in data.monthly_m3:
        if item.day == first:
            return item.value
    # Portál přidá nový měsíc do přehledu až po prvním dopočtu (první den měsíce chybí).
    # Měsíc právě začal, takže nula je pravda, ale jen když přehled jinak platí.
    if any(item.day < first for item in data.monthly_m3):
        return 0.0
    return None


def _last_reading(data: MeterData) -> datetime | None:
    # Portál udává čas v místním čase bez zóny.
    if data.last_reading is None:
        return None
    return data.last_reading.replace(tzinfo=dt_util.get_default_time_zone())


@dataclass(frozen=True, kw_only=True)
class VhsSensorDescription(SensorEntityDescription):
    """Popis senzoru s funkcí, která z dat vytáhne hodnotu."""

    value_fn: Callable[..., Any]
    # Senzory srovnání s loňskem berou navíc počet měsíců z nastavení.
    uses_compare_months: bool = False


SENSORS: tuple[VhsSensorDescription, ...] = (
    VhsSensorDescription(
        key="meter_index",
        translation_key="meter_index",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda d: d.index_m3,
    ),
    VhsSensorDescription(
        key="last_complete_day",
        translation_key="last_complete_day",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_last_complete_day_l,
    ),
    VhsSensorDescription(
        key="last_complete_day_date",
        translation_key="last_complete_day_date",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:calendar-check",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_complete_day_date,
    ),
    VhsSensorDescription(
        key="compare_period",
        translation_key="compare_period",
        icon="mdi:calendar-range",
        value_fn=_compare_period,
        uses_compare_months=True,
    ),
    VhsSensorDescription(
        key="consumption_week",
        translation_key="consumption_week",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        value_fn=_this_week_m3,
    ),
    VhsSensorDescription(
        key="consumption_month",
        translation_key="consumption_month",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        value_fn=_this_month_m3,
    ),
    VhsSensorDescription(
        key="last_reading",
        translation_key="last_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_reading,
    ),
    VhsSensorDescription(
        key="change_vs_last_year",
        translation_key="change_vs_last_year",
        icon="mdi:trending-up",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_change_vs_last_year,
        uses_compare_months=True,
    ),
    VhsSensorDescription(
        key="change_vs_last_year_m3",
        translation_key="change_vs_last_year_m3",
        icon="mdi:water-plus",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=1,
        value_fn=_change_m3,
        uses_compare_months=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VhsBenesovConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            *(VhsSensor(coordinator, entry.entry_id, d) for d in SENSORS),
            LastUpdateSensor(coordinator, entry.entry_id),
            HistoryStatusSensor(coordinator, entry.entry_id),
            *(DayPartSensor(coordinator, entry.entry_id, hour) for hour in DAY_PART_HOURS),
        ]
    )


class VhsSensor(VhsBenesovEntity, SensorEntity):
    """Jeden údaj z portálu."""

    entity_description: VhsSensorDescription

    def __init__(
        self,
        coordinator: VhsBenesovCoordinator,
        entry_id: str,
        description: VhsSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def _compare_months(self) -> int:
        options = self.coordinator.config_entry.options
        return int(options.get(CONF_COMPARE_MONTHS, DEFAULT_COMPARE_MONTHS))

    @property
    def native_value(self) -> Any:
        if self.entity_description.uses_compare_months:
            return self.entity_description.value_fn(self.coordinator.data, self._compare_months)
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        key = self.entity_description.key
        if key == "meter_index":
            return {
                "stav_k_datu": data.index_day.isoformat() if data.index_day else None,
                "stav_k_casu": data.last_reading.isoformat() if data.last_reading else None,
            }
        if key == "last_complete_day":
            found = latest_complete_day(data.curve_liters)
            return {"den": found[0].isoformat()} if found else None
        if key in ("change_vs_last_year", "change_vs_last_year_m3"):
            result = _year_over_year(data, self._compare_months)
            if not result:
                return None
            attrs = {
                "obdobi": f"{result['from']:%m/%Y} – {result['to']:%m/%Y}",
                "spotreba_m3": round(result["now_m3"], 3),
                "loni_m3": round(result["then_m3"], 3),
            }
            # Druhý senzor doplňuje k rozdílu procenta a naopak.
            if key == "change_vs_last_year":
                attrs["rozdil_m3"] = round(result["now_m3"] - result["then_m3"], 3)
            else:
                attrs["zmena_procent"] = round(result["change_percent"], 1)
            return attrs
        return None


class LastUpdateSensor(VhsBenesovEntity, SensorEntity):
    """Kdy naposledy vyšlo stažení dat z portálu."""

    _attr_translation_key = "last_update"
    _attr_icon = "mdi:cloud-check-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: VhsBenesovCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_last_update"

    @property
    def available(self) -> bool:
        """Zůstat dostupný i když poslední pokus selhal - právě pak je vidět."""
        return True

    @property
    def native_value(self) -> str | None:
        """Čas posledního úspěšného kontaktu s portálem: 20.09.2026 23:18.

        Naschvál jako text - časové razítko by Home Assistant vypsal půlkou
        slovy a půlkou číslicemi ("20. září 2026 v 23:18").
        """
        last = self.coordinator.last_success
        return last.strftime("%d.%m.%Y %H:%M") if last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Strojový čas, jestli poslední pokus prošel a kdy se naposled stáhla celá data."""
        last = self.coordinator.last_success
        full = self.coordinator.last_full
        return {
            "cas": last.isoformat() if last else None,
            "posledni_pokus_uspesny": self.coordinator.last_update_success,
            "posledni_stazeni_dat": full.strftime("%d.%m.%Y %H:%M") if full else None,
            "interval_kontroly_hodin": round(CHECK_INTERVAL.total_seconds() / 3600),
        }


class HistoryStatusSensor(VhsBenesovEntity, SensorEntity):
    """Stav importu historie: co se právě děje a jak je to daleko.

    Stav je čitelný text (například "stahuji z portálu (38/53 měsíců)"),
    aby bylo průběh vidět přímo na stránce zařízení.
    """

    _attr_translation_key = "history_status"
    _attr_icon = "mdi:history"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: VhsBenesovCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_history_status"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        importer = self.coordinator.history
        return importer.status.state if importer else "nespuštěno"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        importer = self.coordinator.history
        if importer is None:
            return {}
        status = importer.status
        attrs: dict[str, Any] = {}
        if status.total:
            attrs["postup"] = f"{status.done}/{status.total}"
            attrs["postup_procent"] = status.percent
        if status.first:
            attrs["od"] = status.first
            attrs["do"] = status.last
            attrs["zaznamu"] = status.rows
        if status.detail:
            attrs["poznamka"] = status.detail
        return attrs


# Kolik posledních úplných dnů se bere do průměru, minima a maxima.
CONTEXT_DAYS = 30


class DayPartSensor(VhsBenesovEntity, SensorEntity):
    """Spotřeba v jedné části dne (šestihodinový krok) za poslední úplný den.

    Hodnota je vždy za den, ke kterému portál zná všechny čtyři kroky,
    protože dnešek je většinu dne neúplný. Atribut ``prumer_l`` je průměr téže
    části dne z posledních dnů, aby šlo posoudit, jestli je číslo obvyklé.
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: VhsBenesovCoordinator, entry_id: str, hour: int
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._hour = hour
        self._attr_unique_id = f"{entry_id}_day_part_{hour:02d}"
        self._attr_translation_key = f"day_part_{hour:02d}"
        self._attr_icon = {
            0: "mdi:weather-night", 6: "mdi:weather-sunset-up",
            12: "mdi:weather-sunny", 18: "mdi:weather-sunset-down",
        }[hour]

    def _days(self) -> dict:
        return complete_days(self.coordinator.data.curve_liters)

    @property
    def native_value(self) -> float | None:
        days = self._days()
        if not days:
            return None
        return days[max(days)][self._hour]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        days = self._days()
        if not days:
            return None
        recent = sorted(days)[-CONTEXT_DAYS:]
        values = [days[d][self._hour] for d in recent]
        latest = recent[-1]
        total = sum(days[latest].values())
        return {
            "den": latest.isoformat(),
            "podil_dne_procent": round(days[latest][self._hour] / total * 100) if total else None,
            "prumer_l": round(sum(values) / len(values)),
            "dnu_v_prumeru": len(values),
        }
