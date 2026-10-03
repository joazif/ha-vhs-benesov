"""Integrace VHS Benešov (dálkové odečty vodoměru) pro Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .api import VhsBenesovClient
from .const import (
    CONF_IMPORT_HISTORY,
    CONF_UNITS,
    DATA_UNITS_APPLIED,
    DEFAULT_UNITS,
    DOMAIN,
)
from .coordinator import VhsBenesovCoordinator
from .importer import HistoryImporter

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

type VhsBenesovConfigEntry = ConfigEntry[VhsBenesovCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: VhsBenesovConfigEntry) -> bool:
    """Nastavit integraci z config entry."""
    client = VhsBenesovClient(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    coordinator = VhsBenesovCoordinator(hass, entry, client)

    try:
        # ConfigEntryAuthFailed / ConfigEntryNotReady si HA zpracuje samo.
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        await client.async_close()
        raise

    entry.runtime_data = coordinator
    try:
        _migrate_unique_id(hass, entry, coordinator.data.meter_id)
        _remove_retired_entities(hass, entry)
        coordinator.history = HistoryImporter(hass, entry.entry_id, coordinator)
        await coordinator.history.async_restore()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await client.async_close()
        raise
    _apply_units(hass, entry)
    # Listener až po úpravách identifikátoru a jednotek, ať nevyvolají znovunačtení.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    # Historii stahujeme jen když si ji uživatel při přidání vyžádal a ještě
    # se nepovedla. Běží na pozadí, ať nezdržuje start integrace.
    if entry.options.get(CONF_IMPORT_HISTORY) and not await coordinator.history.async_is_done():
        entry.async_create_background_task(
            hass, coordinator.history.async_import(), f"{DOMAIN}_history"
        )
    return True


# Senzory s objemem vody, kterým se hromadně nastavuje jednotka zobrazení.
VOLUME_SENSOR_KEYS = (
    "meter_index",
    "last_complete_day",
    "consumption_week",
    "consumption_month",
    "change_vs_last_year_m3",
    "day_part_00",
    "day_part_06",
    "day_part_12",
    "day_part_18",
)
UNIT_OF_CHOICE = {"l": UnitOfVolume.LITERS, "m3": UnitOfVolume.CUBIC_METERS}
# Počet desetinných míst podle zvolené jednotky. Bez toho by po převodu na litry zůstaly tři
# desetinná místa z m³ ("912 027,000 l"). Stav, týden a měsíc mají v m³ tři místa, rozdíl dvě.
DISPLAY_PRECISION = {
    "l": {key: 0 for key in VOLUME_SENSOR_KEYS},
    "m3": {key: 2 if key == "change_vs_last_year_m3" else 3 for key in VOLUME_SENSOR_KEYS},
}
# Zvýšit, když se změní, co integrace entitám nastavuje (jednotka, přesnost); u už
# nastavených instalací se pak nastavení jednou obnoví.
UNITS_SETTINGS_VERSION = 2


def _units_marker(wanted: str) -> str:
    return f"{wanted}:{UNITS_SETTINGS_VERSION}"


def _apply_units(hass: HomeAssistant, entry: VhsBenesovConfigEntry) -> None:
    """Nastavit zvolenou jednotku a přesnost senzorům s objemem, ale jen při změně volby.

    Při každém spuštění by se přepsaly ruční změny jednotky u jednotlivých entit,
    proto si integrace pamatuje, co už nastavila. Převod hodnot i statistik
    provede Home Assistant (stejná třída jednotek, tedy bez skoku a bez chyb).
    """
    # Velikost písmen se nerozlišuje, ať fungují i hodnoty uložené dřív ("L").
    wanted = str(entry.options.get(CONF_UNITS, DEFAULT_UNITS)).lower()
    applied = str(entry.data.get(DATA_UNITS_APPLIED, "")).lower()
    if wanted not in UNIT_OF_CHOICE or applied == _units_marker(wanted):
        return
    registry = er.async_get(hass)
    for key in VOLUME_SENSOR_KEYS:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
        if entity_id is None:
            continue
        record = registry.async_get(entity_id)
        # Ostatní volby entity zůstanou zachované.
        options = dict(record.options.get("sensor", {})) if record else {}
        options["unit_of_measurement"] = UNIT_OF_CHOICE[wanted]
        options["display_precision"] = DISPLAY_PRECISION[wanted][key]
        registry.async_update_entity_options(entity_id, "sensor", options)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, DATA_UNITS_APPLIED: _units_marker(wanted)}
    )


# Senzory, které už integrace nemá. "Dnes" bylo většinu dne prázdné a "včera" ukazovalo
# neúplný den, obojí nahradila "Spotřeba poslední úplný den". "Data za den" ukazovalo totéž
# co "Den spotřeby" (dřív "Poslední úplný den").
RETIRED_SENSORS = ("consumption_today", "consumption_yesterday", "parts_day_date")


def _remove_retired_entities(hass: HomeAssistant, entry: VhsBenesovConfigEntry) -> None:
    """Smazat z registru entity vyřazených senzorů, ať nezůstanou jako nedostupné."""
    registry = er.async_get(hass)
    for key in RETIRED_SENSORS:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
        if entity_id:
            registry.async_remove(entity_id)


def _migrate_unique_id(
    hass: HomeAssistant, entry: VhsBenesovConfigEntry, meter_id: str | None
) -> None:
    """Starší instalace mají identifikátor podle loginu; převést na číslo měřidla.

    Když už jiná služba stejné měřidlo má (dva loginy na jedno měřidlo),
    nechá se stávající hodnota - dva záznamy se stejným identifikátorem
    Home Assistant nepřipouští.
    """
    if not meter_id or entry.unique_id == meter_id:
        return
    if any(
        other.unique_id == meter_id and other.entry_id != entry.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        return
    hass.config_entries.async_update_entry(entry, unique_id=meter_id)


async def async_unload_entry(hass: HomeAssistant, entry: VhsBenesovConfigEntry) -> bool:
    """Odpojit integraci."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.async_close()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: VhsBenesovConfigEntry) -> None:
    """Po změně nastavení načíst znovu."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: VhsBenesovConfigEntry) -> None:
    """Při smazání integrace uklidit uložený stav importu historie."""
    from homeassistant.helpers.storage import Store

    from .importer import STORE_VERSION

    await Store(hass, STORE_VERSION, f"{DOMAIN}.history.{entry.entry_id}").async_remove()
