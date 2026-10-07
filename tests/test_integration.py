"""Testy integrace proti skutečnému jádru Home Assistantu (klient je nahrazen)."""

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vhs_benesov import api
from custom_components.vhs_benesov.const import (
    CONF_UNITS,
    CONF_COMPARE_MONTHS,
    CONF_IMPORT_HISTORY,
    DOMAIN,
)

CLIENT = "custom_components.vhs_benesov.api.VhsBenesovClient"


@pytest.fixture(autouse=True)
async def _enable(recorder_mock, enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _unreadable_home_page_by_default():
    """Lehká kontrola bez vlastního nastavení nic nezjistí, takže se stahuje všechno.

    Testy, které chtějí sledovat kontrolu, ji přepíšou vlastním ``patch``.
    """
    with patch(f"{CLIENT}.async_probe", return_value=api.ReadingProbe(None, None)):
        yield


def _data() -> api.MeterData:
    today = dt_util.now().date()
    yesterday = date.fromordinal(today.toordinal() - 1)
    return api.MeterData(
        meter_id="12345-XX-0000001",
        last_reading=datetime(2026, 9, 29, 17, 42),
        daily_liters=[api.DayValue(yesterday, 875.0), api.DayValue(today, 234.0)],
        daily_index_m3=[api.DayValue(today, 911.695)],
        monthly_m3=[api.DayValue(today.replace(day=1), 20.278)],
    )


def _entry() -> MockConfigEntry:
    # Většina testů počítá v m³; výchozí jednotka (litry) má vlastní testy níže.
    return MockConfigEntry(
        domain=DOMAIN, unique_id="0000000000",
        data={CONF_USERNAME: "0000000000", CONF_PASSWORD: "heslo"},
        options={CONF_UNITS: "m3"},
    )


async def test_sensors(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    data = _data()
    data.curve_liters = _curve_days(dt_util.now().date() - timedelta(days=1), 3, unfinished=True)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)

    def state(key):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
        assert entity_id, key
        return hass.states.get(entity_id)

    index = state("meter_index")
    assert float(index.state) == pytest.approx(911.695)
    assert "state_class" not in index.attributes      # statistiku vede vlastní statistika spotřeby
    assert index.attributes["device_class"] == "water"
    assert index.attributes["unit_of_measurement"] == "m³"
    # Poslední úplný den (neúplný další den se nepočítá): 52 + 152 + 202 + 252 l.
    assert float(state("last_complete_day").state) == pytest.approx(0.658)   # při volbě m³
    assert state("last_complete_day").attributes["den"] == (
        dt_util.now().date() - timedelta(days=1)
    ).isoformat()
    assert float(state("consumption_week").state) >= 0
    assert float(state("consumption_month").state) == pytest.approx(20.278)
    # Čas z portálu je místní; v testu je zóna HA US/Pacific, takže se ověří posun.
    stamp = datetime.fromisoformat(state("last_reading").state)
    assert stamp == datetime(2026, 9, 29, 17, 42, tzinfo=dt_util.get_default_time_zone())

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_week_sensor_sums_monday_to_today_across_months(hass: HomeAssistant):
    today = dt_util.now().date()
    monday = today - timedelta(days=today.weekday())
    data = _data()
    # 12 dní zpět po 100 l: týden = pondělí až dnes, starší dny se nepočítají.
    data.daily_liters = [
        api.DayValue(today - timedelta(days=i), 100.0) for i in range(11, -1, -1)
    ]
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_consumption_week"
    )
    state = hass.states.get(entity_id)
    assert float(state.state) == pytest.approx((today - monday).days * 0.1 + 0.1)
    assert state.attributes["unit_of_measurement"] == "m³"


async def test_week_sensor_unknown_without_daily_data(hass: HomeAssistant):
    data = _data()
    data.daily_liters = []
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_consumption_week"
    )
    assert hass.states.get(entity_id).state == "unknown"


async def test_auth_failure_starts_reauth(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", side_effect=api.InvalidAuth("x")):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress())


async def test_single_login_rejection_after_setup_does_not_start_reauth(hass: HomeAssistant):
    """Portál občas odmítne přihlášení i se správným heslem; to není důvod ptát se na heslo."""
    from custom_components.vhs_benesov.coordinator import AUTH_FAILURES_BEFORE_REAUTH

    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data

    with patch(f"{CLIENT}.async_fetch_all", side_effect=api.InvalidAuth("x")):
        for _ in range(AUTH_FAILURES_BEFORE_REAUTH - 1):
            await coordinator.async_refresh()
        assert not any(
            f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress()
        )
        assert not coordinator.last_update_success
        await coordinator.async_refresh()                      # třetí po sobě
        await hass.async_block_till_done()
    assert any(
        f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress()
    )


async def test_successful_update_resets_login_rejection_count(hass: HomeAssistant):
    from custom_components.vhs_benesov.coordinator import AUTH_FAILURES_BEFORE_REAUTH

    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    for _ in range(3):                                         # dvakrát odmítnuto, pak v pořádku
        with patch(f"{CLIENT}.async_fetch_all", side_effect=api.InvalidAuth("x")):
            for _ in range(AUTH_FAILURES_BEFORE_REAUTH - 1):
                await coordinator.async_refresh()
        with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):
            await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert not any(
        f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress()
    )


async def test_temporary_failure_retries(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", side_effect=api.VhsError("dolů")):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_config_flow_ok_and_duplicate(hass: HomeAssistant):
    with patch(f"{CLIENT}.async_validate", return_value="12345-XX-0000001"), patch(
        f"{CLIENT}.async_fetch_all", return_value=_data()
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: " 0000000000 ", CONF_PASSWORD: "heslo",
                CONF_IMPORT_HISTORY: True,
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["options"] == {
            CONF_IMPORT_HISTORY: True, CONF_UNITS: "l",   # litry jsou výchozí
        }
        assert result["title"] == "12345-XX-0000001"
        assert result["data"][CONF_USERNAME] == "0000000000"
        await hass.async_block_till_done()

        again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        again = await hass.config_entries.flow.async_configure(
            again["flow_id"],
            {
                CONF_USERNAME: "0000000000", CONF_PASSWORD: "heslo",
                CONF_IMPORT_HISTORY: False,
            },
        )
        assert again["type"] is FlowResultType.ABORT
        assert again["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("error", "key"),
    [(api.InvalidAuth("x"), "invalid_auth"), (api.VhsError("x"), "cannot_connect")],
)
async def test_config_flow_errors(hass: HomeAssistant, error, key):
    with patch(f"{CLIENT}.async_validate", side_effect=error):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: "a", CONF_PASSWORD: "b",
                CONF_IMPORT_HISTORY: True,
            },
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}


async def test_reauth_updates_password(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="12345-XX-0000001")
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with patch(f"{CLIENT}.async_validate", return_value="12345-XX-0000001"), patch(
        f"{CLIENT}.async_fetch_all", return_value=_data()
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "nove"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "nove"


async def test_checks_every_hour_and_forms_have_no_interval_option(hass: HomeAssistant):
    from custom_components.vhs_benesov.coordinator import CHECK_INTERVAL

    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.runtime_data.update_interval == CHECK_INTERVAL == timedelta(hours=1)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert "scan_interval_hours" not in [str(k) for k in result["data_schema"].schema]
    options = await hass.config_entries.options.async_init(entry.entry_id)
    assert "scan_interval_hours" not in [str(k) for k in options["data_schema"].schema]


async def _setup_coordinator(hass: HomeAssistant, data: api.MeterData):
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data) as fetch:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, entry.runtime_data


async def test_unchanged_reading_costs_only_one_light_check(hass: HomeAssistant):
    data = _data()
    data.reading_m3 = 911.7
    entry, coordinator = await _setup_coordinator(hass, data)
    before = coordinator.last_full
    probe = api.ReadingProbe(data.last_reading, data.reading_m3)
    with patch(f"{CLIENT}.async_probe", return_value=probe) as check, patch(
        f"{CLIENT}.async_fetch_all", return_value=data
    ) as full:
        await coordinator.async_refresh()
    assert check.call_count == 1
    assert full.call_count == 0
    assert coordinator.last_full == before          # data se nestahovala
    assert coordinator.last_update_success


async def test_new_reading_triggers_full_download(hass: HomeAssistant):
    data = _data()
    data.reading_m3 = 911.7
    entry, coordinator = await _setup_coordinator(hass, data)
    newer = _data()
    newer.last_reading = datetime(2026, 9, 30, 5, 42)
    newer.reading_m3 = 912.0
    probe = api.ReadingProbe(newer.last_reading, newer.reading_m3)
    with patch(f"{CLIENT}.async_probe", return_value=probe), patch(
        f"{CLIENT}.async_fetch_all", return_value=newer
    ) as full:
        await coordinator.async_refresh()
    assert full.call_count == 1
    assert coordinator.data.reading_m3 == 912.0


async def test_unreadable_home_page_falls_back_to_full_download(hass: HomeAssistant):
    entry, coordinator = await _setup_coordinator(hass, _data())
    with patch(f"{CLIENT}.async_probe", return_value=api.ReadingProbe(None, None)), patch(
        f"{CLIENT}.async_fetch_all", return_value=_data()
    ) as full:
        await coordinator.async_refresh()
    assert full.call_count == 1


async def test_full_download_at_least_once_a_day_even_without_change(hass: HomeAssistant):
    from custom_components.vhs_benesov.coordinator import FULL_REFRESH_AFTER

    data = _data()
    entry, coordinator = await _setup_coordinator(hass, data)
    coordinator.last_full = dt_util.now() - FULL_REFRESH_AFTER - timedelta(minutes=1)
    probe = api.ReadingProbe(data.last_reading, data.reading_m3)
    with patch(f"{CLIENT}.async_probe", return_value=probe) as check, patch(
        f"{CLIENT}.async_fetch_all", return_value=data
    ) as full:
        await coordinator.async_refresh()
    assert full.call_count == 1 and check.call_count == 0


async def test_refresh_button_downloads_everything_even_if_reading_unchanged(hass: HomeAssistant):
    data = _data()
    entry, coordinator = await _setup_coordinator(hass, data)
    probe = api.ReadingProbe(data.last_reading, data.reading_m3)
    button_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_refresh"
    )
    with patch(f"{CLIENT}.async_probe", return_value=probe), patch(
        f"{CLIENT}.async_fetch_all", return_value=data
    ) as full:
        await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
        await hass.async_block_till_done()
    assert full.call_count == 1


async def test_diagnostic_sensor_and_refresh_button(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()) as fetch:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        sensor_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_last_update")
        record = registry.async_get(sensor_id)
        assert record.entity_category is EntityCategory.DIAGNOSTIC
        state = hass.states.get(sensor_id)
        assert datetime.strptime(state.state, "%d.%m.%Y %H:%M")
        assert state.attributes["interval_kontroly_hodin"] == 1
        assert state.attributes["posledni_pokus_uspesny"] is True

        button_id = registry.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_refresh")
        assert registry.async_get(button_id).entity_category is None  # "Ovládací prvky"
        calls = fetch.call_count
        await hass.services.async_call(
            "button", "press", {"entity_id": button_id}, blocking=True
        )
        await hass.async_block_till_done()
        assert fetch.call_count == calls + 1


async def test_diagnostic_sensor_stays_available_after_failed_refresh(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()) as fetch:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        sensor_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_last_update")
        before = hass.states.get(sensor_id).state

        fetch.side_effect = api.VhsError("dolů")
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get(sensor_id)
    assert state.state == before  # poslední úspěšné stažení se nezměnilo
    assert state.attributes["posledni_pokus_uspesny"] is False


# ---------------------------------------------------------------------------
# Import historie do statistik
# ---------------------------------------------------------------------------

from datetime import UTC, timedelta  # noqa: E402

from homeassistant.components.recorder import get_instance  # noqa: E402
from homeassistant.components.recorder.models import StatisticMeanType  # noqa: E402
from homeassistant.components.recorder.statistics import (  # noqa: E402
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.helpers import storage  # noqa: E402
from pytest_homeassistant_custom_component.components.recorder.common import (  # noqa: E402
    async_wait_recording_done,
)

from custom_components.vhs_benesov.const import CONF_IMPORT_HISTORY  # noqa: E402,F811
from custom_components.vhs_benesov.importer import HistoryImporter  # noqa: E402


def _at(delta):
    """Patch času importu o `delta` dál od teď (pro pojistku proti opakování)."""
    return patch.object(HistoryImporter, "_now", return_value=dt_util.utcnow() + delta)


def _history(days=10) -> api.HistoryData:
    """Deset dní zpět (do včerejška včetně), stav roste o 0,5 m³ denně.

    Křivka je úplná u posledních tří dnů, takže poslední dva jsou konečné a vznikne kontrolní bod.
    """
    today = dt_util.now().date()
    first = today - timedelta(days=days)
    end = {
        first + timedelta(days=i): 900.0 + 0.5 * i for i in range(days)
    }
    last = max(end)
    curve = {
        datetime(d.year, d.month, d.day, hour): 100.0
        for d in (last - timedelta(days=2), last - timedelta(days=1), last)
        for hour in (0, 6, 12, 18)
    }
    return api.HistoryData(index_end=end, curve_liters=curve, months=1, first_month=first, last_month=today)


def _entry_with_history(option=True) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, unique_id="0000000000",
        data={CONF_USERNAME: "0000000000", CONF_PASSWORD: "heslo"},
        options={CONF_IMPORT_HISTORY: option, CONF_UNITS: "m3"},
    )


def _meter_data(index=905.0) -> api.MeterData:
    data = _data()
    data.daily_index_m3 = [api.DayValue(dt_util.now().date(), index)]
    return data


async def _stats(hass, entity_id):
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=UTC), None,
        {entity_id}, "hour", {"volume": "m³"}, {"state", "sum"},
    )
    return rows.get(entity_id, [])


def _history_status(hass, entry):
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_history_status"
    )
    assert entity_id
    return hass.states.get(entity_id)


STAT_ID = "vhs_benesov:12345_xx_0000001_consumption"     # podle čísla měřidla z _data()


async def _entity_id(hass, entry):
    """Externí statistika spotřeby (dřív zápis do statistiky senzoru stavu)."""
    return STAT_ID


async def _setup(hass, entry, history):
    with patch(f"{CLIENT}.async_fetch_all", return_value=_meter_data()), patch(
        f"{CLIENT}.async_fetch_history", return_value=history
    ) as fetch:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        return fetch


async def test_history_is_imported_into_external_statistics(hass: HomeAssistant):
    entry = _entry_with_history()
    entry.add_to_hass(hass)
    fetch = await _setup(hass, entry, _history())
    assert fetch.call_count == 1

    rows = await _stats(hass, STAT_ID)
    # První den nemá předchozí stav ani křivku, takže se přeskočí: 9 dní.
    assert len(rows) == 9 * 24
    assert rows[0]["start"] < rows[-1]["start"]
    assert rows[-1]["state"] == pytest.approx(900.0 + 0.5 * 9)
    assert rows[-1]["sum"] == pytest.approx(0.5 * 9)           # součet spotřeby od začátku řady
    assert all(b["sum"] >= a["sum"] for a, b in zip(rows, rows[1:]))  # součet neklesá

    status = _history_status(hass, entry)
    assert status.state.startswith("hotovo (")
    assert status.attributes["zaznamu"] == 216
    assert status.attributes["od"] and status.attributes["do"]
    assert status.attributes["statistika"] == STAT_ID
    assert status.attributes["zapsano_do"]

    saved = await storage.Store(hass, 1, f"{DOMAIN}.history.{entry.entry_id}").async_load()
    assert saved["done"] is True and saved["series_day"]      # kontrolní bod pro doplňování
    assert any(
        n["notification_id"].startswith(f"{DOMAIN}_history_") and STAT_ID in n["message"]
        for n in hass.data["persistent_notification"].values()
    )


async def test_history_not_downloaded_when_declined(hass: HomeAssistant):
    entry = _entry_with_history(option=False)
    entry.add_to_hass(hass)
    fetch = await _setup(hass, entry, _history())
    assert fetch.call_count == 0
    assert await _stats(hass, await _entity_id(hass, entry)) == []


async def test_history_not_repeated_after_restart_but_button_reruns_idempotently(
    hass: HomeAssistant,
):
    entry = _entry_with_history()
    entry.add_to_hass(hass)
    await _setup(hass, entry, _history())
    entity_id = await _entity_id(hass, entry)
    before = await _stats(hass, entity_id)

    # Restart integrace: historie se znovu nestahuje.
    assert await hass.config_entries.async_unload(entry.entry_id)
    fetch = await _setup(hass, entry, _history())
    assert fetch.call_count == 0

    # Tlačítko: stáhne znovu, hodnoty zůstanou stejné a nic se nezdvojí.
    button_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_refresh_history"
    )
    with _at(timedelta(hours=25)), patch(
        f"{CLIENT}.async_fetch_all", return_value=_meter_data()
    ), patch(f"{CLIENT}.async_fetch_history", return_value=_history()) as again:
        await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
    assert again.call_count == 1
    after = await _stats(hass, entity_id)
    assert [(r["start"], round(r["sum"], 9)) for r in after] == [
        (r["start"], round(r["sum"], 9)) for r in before
    ]


async def test_failed_import_is_reported_and_retried_next_start(hass: HomeAssistant):
    entry = _entry_with_history()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_meter_data()), patch(
        f"{CLIENT}.async_fetch_history", side_effect=api.VhsError("portál nejede")
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    status = _history_status(hass, entry)
    assert status.state == "chyba"
    assert "portál nejede" in status.attributes["poznamka"]
    assert await _stats(hass, await _entity_id(hass, entry)) == []

    # Nedokončený import se při dalším startu zkusí znovu, ale až po odpočinku.
    assert await hass.config_entries.async_unload(entry.entry_id)
    with _at(timedelta(minutes=20)):
        fetch = await _setup(hass, entry, _history())
    assert fetch.call_count == 1


# ---------------------------------------------------------------------------
# Víc účtů; identifikátor je číslo měřidla
# ---------------------------------------------------------------------------


def _other_meter_data(meter_id: str) -> api.MeterData:
    data = _data()
    data.meter_id = meter_id
    data.daily_index_m3 = [api.DayValue(dt_util.now().date(), 100.0)]
    return data


async def test_unique_id_is_meter_number_and_same_meter_cannot_be_added_twice(
    hass: HomeAssistant,
):
    with patch(f"{CLIENT}.async_validate", return_value="12345-XX-0000001"), patch(
        f"{CLIENT}.async_fetch_all", return_value=_data()
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "0000000001", CONF_PASSWORD: "a",
             CONF_IMPORT_HISTORY: False},
        )
        assert result["result"].unique_id == "12345-XX-0000001"
        await hass.async_block_till_done()

        # Jiný login, ale stejné měřidlo: odmítnout.
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "0000000002", CONF_PASSWORD: "b",
             CONF_IMPORT_HISTORY: False},
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


async def test_second_account_with_other_meter_is_added_alongside(hass: HomeAssistant):
    meters = {"0000000001": "12345-XX-0000001", "0000000002": "12345-XX-0000002"}

    async def fetch(self):
        return _other_meter_data(meters[self._login])

    async def validate(self):
        return meters[self._login]

    with patch(f"{CLIENT}.async_validate", autospec=True, side_effect=validate), patch(
        f"{CLIENT}.async_fetch_all", autospec=True, side_effect=fetch
    ):
        for login in meters:
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_USERNAME: login, CONF_PASSWORD: "x",
                 CONF_IMPORT_HISTORY: False},
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert sorted(e.unique_id for e in entries) == sorted(meters.values())
    assert all(e.state is ConfigEntryState.LOADED for e in entries)
    assert sorted(e.title for e in entries) == sorted(meters.values())

    registry = er.async_get(hass)
    ids = {e.entry_id: registry.async_get_entity_id("sensor", DOMAIN, f"{e.entry_id}_meter_index")
           for e in entries}
    assert len(set(ids.values())) == 2 and None not in ids.values()  # každý svůj senzor
    device_registry = dr.async_get(hass)
    device_ids = {
        d.id for e in entries for d in dr.async_entries_for_config_entry(device_registry, e.entry_id)
    }
    assert len(device_ids) == 2  # každé měřidlo má své zařízení


async def test_old_login_based_unique_id_is_migrated_to_meter_without_reload(
    hass: HomeAssistant,
):
    entry = _entry()  # unique_id = login, jako u starších instalací
    entry.add_to_hass(hass)
    assert entry.unique_id == "0000000000"
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()) as fetch:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.unique_id == "12345-XX-0000001"
    assert entry.state is ConfigEntryState.LOADED
    assert fetch.call_count == 1  # úprava identifikátoru nevyvolala znovunačtení


async def test_migration_keeps_login_id_when_meter_already_used(hass: HomeAssistant):
    first = MockConfigEntry(
        domain=DOMAIN, unique_id="12345-XX-0000001",
        data={CONF_USERNAME: "0000000001", CONF_PASSWORD: "x"},
    )
    second = MockConfigEntry(
        domain=DOMAIN, unique_id="0000000002",
        data={CONF_USERNAME: "0000000002", CONF_PASSWORD: "x"},
    )
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):  # obě vidí totéž měřidlo
        # První setup načte celou doménu, tedy obě služby.
        await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done()
    assert second.unique_id == "0000000002"  # konflikt se neřeší násilím


async def test_reauth_with_login_of_another_meter_is_refused(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="12345-XX-0000001")
    result = await entry.start_reauth_flow(hass)
    with patch(f"{CLIENT}.async_validate", return_value="12345-XX-0000099"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "jine"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_PASSWORD] == "heslo"  # heslo se nezměnilo


# ---------------------------------------------------------------------------
# Části dne
# ---------------------------------------------------------------------------


def _curve_days(latest_complete: date, days: int, *, unfinished: bool) -> list[api.PointValue]:
    """`days` úplných dnů končících `latest_complete`; volitelně neúplný další den."""
    points = []
    for i in range(days):
        day = latest_complete - timedelta(days=days - 1 - i)
        for hour, litres in zip((0, 6, 12, 18), (50 + i, 150 + i, 200 + i, 250 + i), strict=True):
            points.append(api.PointValue(datetime(day.year, day.month, day.day, hour), float(litres)))
    if unfinished:
        nxt = latest_complete + timedelta(days=1)
        points.append(api.PointValue(datetime(nxt.year, nxt.month, nxt.day, 0), 999.0))
    return points


async def test_day_part_sensors_use_last_complete_day_and_give_context(hass: HomeAssistant):
    today = dt_util.now().date()
    yesterday = today - timedelta(days=1)
    data = _data()
    data.curve_liters = _curve_days(yesterday, 40, unfinished=True)
    entry = MockConfigEntry(      # výchozí jednotky (litry)
        domain=DOMAIN, unique_id="0000000000",
        data={CONF_USERNAME: "0000000000", CONF_PASSWORD: "heslo"},
    )
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)

    def sensor(hour):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_day_part_{hour:02d}")
        assert entity_id, hour
        assert registry.async_get(entity_id).entity_category is EntityCategory.DIAGNOSTIC
        return hass.states.get(entity_id)

    night = sensor(0)
    # poslední úplný den je index 39: 50 + 39 = 89; neúplný den (999) se nepočítá
    assert float(night.state) == 89
    assert night.attributes["den"] == yesterday.isoformat()
    assert night.attributes["dnu_v_prumeru"] == 30            # jen posledních 30 dní
    assert "minimum_l" not in night.attributes and "maximum_l" not in night.attributes
    assert night.attributes["prumer_l"] == round(sum(50 + i for i in range(10, 40)) / 30)
    total = 89 + 189 + 239 + 289
    assert night.attributes["podil_dne_procent"] == round(89 / total * 100)
    assert float(sensor(18).state) == 289
    assert float(sensor(6).state) == 189 and float(sensor(12).state) == 239
    assert night.attributes["unit_of_measurement"] == "L"


async def test_day_part_sensors_unknown_without_a_complete_day(hass: HomeAssistant):
    data = _data()
    data.curve_liters = [api.PointValue(datetime(2026, 9, 29, 0), 81.0)]  # jediný krok
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_day_part_06"
    )
    state = hass.states.get(entity_id)
    assert state.state == "unknown"
    assert "prumer_l" not in state.attributes


async def test_history_status_shows_progress_while_downloading(hass: HomeAssistant):
    """Během stahování je vidět "38/53 měsíců" a procenta; po dokončení výsledek."""
    import asyncio

    entry = _entry_with_history()
    entry.add_to_hass(hass)
    reached = asyncio.Event()
    release = asyncio.Event()

    async def slow_fetch(self, *, progress=None, **_):
        progress(38, 53)
        reached.set()
        await release.wait()
        return _history()

    with patch(f"{CLIENT}.async_fetch_all", return_value=_meter_data()), patch(
        f"{CLIENT}.async_fetch_history", autospec=True, side_effect=slow_fetch
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(reached.wait(), 5)
        await hass.async_block_till_done()

        during = _history_status(hass, entry)
        assert during.state == "stahuji z portálu (38/53 měsíců)"
        assert during.attributes["postup"] == "38/53"
        assert during.attributes["postup_procent"] == 72
        # Při spuštění se objeví upozornění, kde průběh sledovat.
        message = next(
            n["message"] for n in hass.data["persistent_notification"].values()
        )
        assert "Stav historie" in message

        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
        await hass.async_block_till_done()

    after = _history_status(hass, entry)
    assert after.state.startswith("hotovo (")
    assert "postup" not in after.attributes  # po dokončení se ukazuje výsledek, ne postup


async def test_history_status_survives_restart_as_done(hass: HomeAssistant):
    entry = _entry_with_history()
    entry.add_to_hass(hass)
    await _setup(hass, entry, _history())
    done = _history_status(hass, entry)
    assert done.state.startswith("hotovo (")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await _setup(hass, entry, _history())  # restart; historie se znovu nestahuje
    again = _history_status(hass, entry)
    assert again.state == done.state
    assert again.attributes["zaznamu"] == done.attributes["zaznamu"] == 216
    assert again.attributes["od"] == done.attributes["od"]


async def test_history_status_before_any_import(hass: HomeAssistant):
    entry = _entry_with_history(option=False)
    entry.add_to_hass(hass)
    await _setup(hass, entry, _history())
    status = _history_status(hass, entry)
    assert status.state == "nespuštěno"
    assert status.attributes.get("postup") is None


# ---------------------------------------------------------------------------
# Ochrana portálu před opakovaným importem
# ---------------------------------------------------------------------------


async def _press_history_button(hass, entry, history):
    button_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_refresh_history"
    )
    with patch(f"{CLIENT}.async_fetch_all", return_value=_meter_data()), patch(
        f"{CLIENT}.async_fetch_history", return_value=history
    ) as fetch:
        await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
    return fetch


def _notification_text(hass, entry) -> str:
    return hass.data["persistent_notification"][f"{DOMAIN}_history_{entry.entry_id}"]["message"]


async def test_button_pressed_again_right_after_success_is_ignored(hass: HomeAssistant):
    entry = _entry_with_history()
    entry.add_to_hass(hass)
    await _setup(hass, entry, _history())
    for _ in range(3):  # klidně opakovaně, portál se nezatíží
        fetch = await _press_history_button(hass, entry, _history())
        assert fetch.call_count == 0
    assert "za 23 h" in _notification_text(hass, entry) or "Další stažení" in _notification_text(hass, entry)
    assert _history_status(hass, entry).state.startswith("hotovo")   # stav se nepokazil


async def test_button_allowed_again_after_a_day(hass: HomeAssistant):
    entry = _entry_with_history()
    entry.add_to_hass(hass)
    await _setup(hass, entry, _history())
    with _at(timedelta(hours=23)):
        assert (await _press_history_button(hass, entry, _history())).call_count == 0
    with _at(timedelta(hours=24, minutes=1)):
        assert (await _press_history_button(hass, entry, _history())).call_count == 1


async def test_after_failure_retry_is_allowed_only_after_quarter_of_hour(hass: HomeAssistant):
    entry = _entry_with_history()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_meter_data()), patch(
        f"{CLIENT}.async_fetch_history", side_effect=api.VhsError("dolů")
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert _history_status(hass, entry).state == "chyba"

    with _at(timedelta(minutes=5)):
        assert (await _press_history_button(hass, entry, _history())).call_count == 0
    assert "Další stažení" in _notification_text(hass, entry)
    with _at(timedelta(minutes=16)):
        assert (await _press_history_button(hass, entry, _history())).call_count == 1
    assert _history_status(hass, entry).state.startswith("hotovo")


async def test_restart_loop_after_failure_does_not_hammer_portal(hass: HomeAssistant):
    """Chybný import + opakované restarty: portál se nezahltí každým startem."""
    entry = _entry_with_history()
    entry.add_to_hass(hass)
    calls = 0
    for _ in range(3):
        with patch(f"{CLIENT}.async_fetch_all", return_value=_meter_data()), patch(
            f"{CLIENT}.async_fetch_history", side_effect=api.VhsError("dolů")
        ) as fetch:
            if entry.state is ConfigEntryState.LOADED:
                assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done(wait_background_tasks=True)
            calls += fetch.call_count
    assert calls == 1   # jen první start; další do čtvrt hodiny odmítnuty


async def test_cooldown_survives_restart_via_storage(hass: HomeAssistant):
    entry = _entry_with_history()
    entry.add_to_hass(hass)
    await _setup(hass, entry, _history())
    saved = await storage.Store(hass, 1, f"{DOMAIN}.history.{entry.entry_id}").async_load()
    assert saved["last_ok"] is True and saved["last_run"]
    assert saved["done"] is True and saved["series_day"]        # slučování nic nesmazalo


async def test_update_button_is_rate_limited(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()) as fetch:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        button_id = er.async_get(hass).async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}_refresh"
        )
        base = fetch.call_count
        for _ in range(5):  # spam
            await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
            await hass.async_block_till_done()
        assert fetch.call_count - base <= 1


# ---------------------------------------------------------------------------
# Sada senzorů: poslední úplný den, názvy a úklid vyřazených entit
# ---------------------------------------------------------------------------


async def test_last_complete_day_ignores_unfinished_day_and_unknown_without_data(
    hass: HomeAssistant,
):
    entry = _entry()
    entry.add_to_hass(hass)
    data = _data()
    data.curve_liters = [api.PointValue(datetime(2026, 9, 29, 0), 81.0)]  # jen jeden krok
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_last_complete_day"
    )
    state = hass.states.get(entity_id)
    assert state.state == "unknown"
    assert "den" not in state.attributes


async def test_retired_today_and_yesterday_sensors_are_removed(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = [
        registry.async_get_or_create(
            "sensor", DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry
        ).entity_id
        for key in ("consumption_today", "consumption_yesterday", "parts_day_date")
    ]
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert all(registry.async_get(entity_id) is None for entity_id in old)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_meter_index")


def test_sensor_names_sort_alphabetically_into_a_logical_order():
    """HA řadí entity podle názvu; názvy musí dát pořadí stav, odečet, den, týden, měsíc."""
    import json

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    sensors = json.loads(
        (root / "custom_components/vhs_benesov/translations/cs.json").read_text("utf-8")
    )["entity"]["sensor"]
    main = [
        "meter_index", "last_reading", "last_complete_day",
        "consumption_week", "consumption_month", "change_vs_last_year",
        "change_vs_last_year_m3", "compare_period",
    ]
    names = [sensors[k]["name"] for k in main]
    assert names == sorted(names, key=str.casefold), names
    assert not any(name[0].isdigit() for name in names)             # žádné číslování názvů
    # Diagnostika: části dne (řadí je čas na začátku názvu), pak datum, aktualizace, historie.
    diag = [
        "day_part_00", "day_part_06", "day_part_12", "day_part_18",
        "last_complete_day_date", "last_update", "history_status",
    ]
    diag_names = [sensors[k]["name"] for k in diag]
    assert diag_names == sorted(diag_names, key=str.casefold), diag_names
    assert "consumption_today" not in sensors and "consumption_yesterday" not in sensors
    assert "parts_day_date" not in sensors


async def test_date_sensors_show_which_day_the_numbers_belong_to(hass: HomeAssistant):
    yesterday = dt_util.now().date() - timedelta(days=1)
    data = _data()
    data.curve_liters = _curve_days(yesterday, 3, unfinished=True)
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_last_complete_day_date"
    )
    assert entity_id
    assert registry.async_get(entity_id).entity_category is EntityCategory.DIAGNOSTIC
    state = hass.states.get(entity_id)
    assert state.state == yesterday.isoformat()              # neúplný další den se nepočítá
    assert state.attributes["device_class"] == "date"
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_parts_day_date") is None

    # Stejný den jako spotřeba a části dne.
    total = hass.states.get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_last_complete_day")
    )
    assert total.attributes["den"] == yesterday.isoformat()


async def test_date_sensors_unknown_without_a_complete_day(hass: HomeAssistant):
    data = _data()
    data.curve_liters = []
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_last_complete_day_date"
    )
    assert hass.states.get(entity_id).state == "unknown"


# ---------------------------------------------------------------------------
# Změna oproti loňsku
# ---------------------------------------------------------------------------


def _monthly_back_from_today(this_year: float, last_year: float) -> list[api.DayValue]:
    """24 měsíců končících měsícem dneška: posledních 12 po `this_year`, předchozích 12 po `last_year`."""
    today = dt_util.now().date().replace(day=1)
    months = []
    for back in range(23, -1, -1):
        index = today.year * 12 + today.month - 1 - back
        months.append(
            api.DayValue(date(index // 12, index % 12 + 1, 1), this_year if back < 12 else last_year)
        )
    return months


async def test_change_vs_last_year_sensor(hass: HomeAssistant):
    data = _data()
    data.monthly_m3 = _monthly_back_from_today(this_year=15.0, last_year=10.0)
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_change_vs_last_year"
    )
    state = hass.states.get(entity_id)
    assert float(state.state) == 50.0
    assert state.attributes["unit_of_measurement"] == "%"
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["spotreba_m3"] == 45.0
    assert state.attributes["loni_m3"] == 30.0
    assert state.attributes["rozdil_m3"] == 15.0
    assert "/" in state.attributes["obdobi"]


async def test_change_vs_last_year_unknown_with_short_history(hass: HomeAssistant):
    data = _data()     # jen aktuální měsíc, žádný loňský rok
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_change_vs_last_year"
    )
    state = hass.states.get(entity_id)
    assert state.state == "unknown"
    assert "obdobi" not in state.attributes


async def _change_states(hass, entry):
    registry = er.async_get(hass)
    return {
        key: hass.states.get(
            registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
        )
        for key in ("change_vs_last_year", "change_vs_last_year_m3")
    }


async def test_change_in_percent_and_in_cubic_meters_are_two_consistent_sensors(hass: HomeAssistant):
    data = _data()
    data.monthly_m3 = _monthly_back_from_today(this_year=15.0, last_year=10.0)
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    states = await _change_states(hass, entry)
    pct, m3 = states["change_vs_last_year"], states["change_vs_last_year_m3"]

    assert float(pct.state) == 50.0 and pct.attributes["unit_of_measurement"] == "%"
    assert float(m3.state) == 15.0 and m3.attributes["unit_of_measurement"] == "m³"
    assert m3.attributes["device_class"] == "water"
    # Oba senzory se navzájem doplňují a mluví o stejném období.
    assert pct.attributes["rozdil_m3"] == 15.0
    assert m3.attributes["zmena_procent"] == 50.0
    assert pct.attributes["obdobi"] == m3.attributes["obdobi"]
    assert m3.attributes["spotreba_m3"] == 45.0 and m3.attributes["loni_m3"] == 30.0


async def test_change_is_negative_when_consumption_dropped(hass: HomeAssistant):
    data = _data()
    data.monthly_m3 = _monthly_back_from_today(this_year=8.0, last_year=10.0)
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    states = await _change_states(hass, entry)
    assert float(states["change_vs_last_year"].state) == -20.0
    assert float(states["change_vs_last_year_m3"].state) == -6.0


async def test_both_change_sensors_unknown_with_short_history(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    states = await _change_states(hass, entry)
    assert {s.state for s in states.values()} == {"unknown"}


# ---------------------------------------------------------------------------
# Nastavení: za kolik měsíců srovnávat s loňskem
# ---------------------------------------------------------------------------


async def _setup_with_monthly(hass, monthly, options=None):
    data = _data()
    data.monthly_m3 = monthly
    entry = _entry()
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(entry, options={CONF_UNITS: "m3", **options})
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, data


async def test_options_offer_1_3_6_months_default_3_and_keep_other_options(hass: HomeAssistant):
    entry, data = await _setup_with_monthly(
        hass, _monthly_back_from_today(15.0, 10.0),
        options={CONF_IMPORT_HISTORY: True},
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    field = next(k for k in result["data_schema"].schema if k == CONF_COMPARE_MONTHS)
    assert field.default() == "3"
    choices = list(result["data_schema"].schema[field].config["options"])
    assert choices == ["1", "3", "6"]          # 12 nejde: portál má jen 24 měsíců

    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_COMPARE_MONTHS: "6"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_COMPARE_MONTHS] == 6
    assert entry.options[CONF_IMPORT_HISTORY] is True     # nic nesmazáno


async def test_sensors_follow_the_configured_number_of_months(hass: HomeAssistant):
    monthly = _monthly_back_from_today(this_year=15.0, last_year=10.0)
    entry, _ = await _setup_with_monthly(hass, monthly, options={CONF_COMPARE_MONTHS: 6})
    states = await _change_states(hass, entry)
    pct, m3 = states["change_vs_last_year"], states["change_vs_last_year_m3"]
    assert float(pct.state) == 50.0
    assert float(m3.state) == 30.0                        # 6 měsíců × 5 m³ rozdíl
    assert m3.attributes["spotreba_m3"] == 90.0 and m3.attributes["loni_m3"] == 60.0

    # Rozsah období v atributu odpovídá šesti měsícům.
    first, last = (part.strip() for part in pct.attributes["obdobi"].split("–"))
    (m1, y1), (m2, y2) = (map(int, first.split("/"))), (map(int, last.split("/")))
    assert (y2 * 12 + m2) - (y1 * 12 + m1) == 5


async def test_one_month_comparison(hass: HomeAssistant):
    entry, _ = await _setup_with_monthly(
        hass, _monthly_back_from_today(this_year=15.0, last_year=10.0), options={CONF_COMPARE_MONTHS: 1}
    )
    states = await _change_states(hass, entry)
    assert float(states["change_vs_last_year_m3"].state) == 5.0
    assert float(states["change_vs_last_year"].state) == 50.0
    assert states["change_vs_last_year"].attributes["obdobi"].count("/") == 2   # jediný měsíc


async def test_six_months_unknown_when_history_too_short(hass: HomeAssistant):
    short = _monthly_back_from_today(this_year=15.0, last_year=10.0)[-15:]   # jen 15 měsíců
    entry, _ = await _setup_with_monthly(hass, short, options={CONF_COMPARE_MONTHS: 6})
    states = await _change_states(hass, entry)
    assert {s.state for s in states.values()} == {"unknown"}


# ---------------------------------------------------------------------------
# Aktuální stav vodoměru z číselníku
# ---------------------------------------------------------------------------


async def test_meter_sensor_shows_odometer_reading_not_end_of_previous_day(hass: HomeAssistant):
    """Skutečný případ: číselník 912,027 (30. 9. 5:42), denní řada končí 911,988 (29. 9.)."""
    data = _data()
    data.daily_index_m3 = [api.DayValue(date(2026, 9, 28), 911.28), api.DayValue(date(2026, 9, 29), 911.988)]
    data.reading_m3 = 912.027
    data.last_reading = datetime(2026, 9, 30, 5, 42)
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_meter_index"
    )
    state = hass.states.get(entity_id)
    assert float(state.state) == pytest.approx(912.027)
    assert state.attributes["stav_k_datu"] == "2026-09-30"
    assert state.attributes["stav_k_casu"] == "2026-09-30T05:42:00"
    assert "state_class" not in state.attributes


# ---------------------------------------------------------------------------
# Jednotky: litry jsou výchozí, jdou přepnout na m³ a převod dělá Home Assistant
# ---------------------------------------------------------------------------


def _entry_default_units() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, unique_id="0000000000",
        data={CONF_USERNAME: "0000000000", CONF_PASSWORD: "heslo"},
    )


async def _unit_states(hass, entry):
    registry = er.async_get(hass)
    keys = ("meter_index", "last_complete_day", "consumption_week", "consumption_month",
            "change_vs_last_year_m3", "day_part_00")
    return {
        key: hass.states.get(registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}"))
        for key in keys
    }


def _unit_data() -> api.MeterData:
    data = _data()
    data.curve_liters = _curve_days(dt_util.now().date() - timedelta(days=1), 3, unfinished=False)
    data.monthly_m3 = _monthly_back_from_today(this_year=15.0, last_year=10.0)
    return data


async def test_litres_are_the_default_for_all_water_sensors(hass: HomeAssistant):
    entry = _entry_default_units()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_unit_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    states = await _unit_states(hass, entry)
    assert {s.attributes["unit_of_measurement"] for s in states.values()} == {"L"}
    assert float(states["meter_index"].state) == pytest.approx(911695.0)   # 911,695 m³
    assert float(states["change_vs_last_year_m3"].state) == pytest.approx(15000.0)
    assert float(states["last_complete_day"].state) == 658.0               # nativně litry
    assert entry.data["units_applied"].startswith("l:")      # značka s verzí nastavení


async def test_changing_unit_option_converts_every_water_sensor_back_and_forth(hass: HomeAssistant):
    entry = _entry_default_units()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_unit_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        field = next(k for k in result["data_schema"].schema if k == CONF_UNITS)
        assert field.default() == "l"
        assert list(result["data_schema"].schema[field].config["options"]) == ["l", "m3"]

        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_COMPARE_MONTHS: "3", CONF_UNITS: "m3"}
        )
        await hass.async_block_till_done()
        states = await _unit_states(hass, entry)
        assert {s.attributes["unit_of_measurement"] for s in states.values()} == {"m³"}
        assert float(states["meter_index"].state) == pytest.approx(911.695)
        assert float(states["last_complete_day"].state) == pytest.approx(0.658)   # litry -> m³

        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_COMPARE_MONTHS: "3", CONF_UNITS: "l"}
        )
        await hass.async_block_till_done()
    states = await _unit_states(hass, entry)
    assert {s.attributes["unit_of_measurement"] for s in states.values()} == {"L"}
    assert float(states["meter_index"].state) == pytest.approx(911695.0)


async def test_manual_unit_change_of_one_entity_survives_restart(hass: HomeAssistant):
    entry = _entry_default_units()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_unit_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        week_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_consumption_week")
        options = dict(registry.async_get(week_id).options.get("sensor", {}))
        options["unit_of_measurement"] = "m³"                      # uživatel si přepne jen jednu entitu
        registry.async_update_entity_options(week_id, "sensor", options)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    states = await _unit_states(hass, entry)
    assert states["consumption_week"].attributes["unit_of_measurement"] == "m³"   # nepřepsáno
    assert states["meter_index"].attributes["unit_of_measurement"] == "L"         # ostatní beze změny


async def test_external_statistic_stays_in_cubic_metres_with_litres_displayed(hass: HomeAssistant):
    """Statistika je v m³ bez ohledu na to, v čem se zobrazují senzory (litry jsou výchozí)."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="0000000000",
        data={CONF_USERNAME: "0000000000", CONF_PASSWORD: "heslo"},
        options={CONF_IMPORT_HISTORY: True},          # jednotky výchozí = litry
    )
    entry.add_to_hass(hass)
    await _setup(hass, entry, _history())
    states = await _unit_states(hass, entry)
    assert states["meter_index"].attributes["unit_of_measurement"] == "L"
    rows = await _stats(hass, STAT_ID)
    assert len(rows) == 9 * 24
    assert rows[-1]["state"] == pytest.approx(904.5)                       # m³, ne 904 500
    assert rows[-1]["sum"] == pytest.approx(0.5 * 9)


async def test_units_are_chosen_in_the_first_step_of_the_setup_form(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    field = next(k for k in result["data_schema"].schema if k == CONF_UNITS)
    assert field.default() == "l"
    assert list(result["data_schema"].schema[field].config["options"]) == ["l", "m3"]

    with patch(f"{CLIENT}.async_validate", return_value="12345-XX-0000001"), patch(
        f"{CLIENT}.async_fetch_all", return_value=_unit_data()
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "0000000000", CONF_PASSWORD: "heslo",
             CONF_UNITS: "m3", CONF_IMPORT_HISTORY: False},
        )
        await hass.async_block_till_done()
    assert result["options"][CONF_UNITS] == "m3"
    entry = result["result"]
    states = await _unit_states(hass, entry)
    assert {s.attributes["unit_of_measurement"] for s in states.values()} == {"m³"}   # hned od začátku
    assert float(states["meter_index"].state) == pytest.approx(911.695)


# ---------------------------------------------------------------------------
# Desetinná místa podle jednotky
# ---------------------------------------------------------------------------


def _sensor_options(hass, entry, key):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    return dict(registry.async_get(entity_id).options.get("sensor", {}))


async def test_litres_have_no_decimals_and_cubic_metres_keep_theirs(hass: HomeAssistant):
    entry = _entry_default_units()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_unit_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for key in ("meter_index", "consumption_week", "consumption_month",
                    "change_vs_last_year_m3", "last_complete_day", "day_part_00"):
            options = _sensor_options(hass, entry, key)
            assert options["unit_of_measurement"] == "L" and options["display_precision"] == 0, key

        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_COMPARE_MONTHS: "3", CONF_UNITS: "m3"}
        )
        await hass.async_block_till_done()
    assert _sensor_options(hass, entry, "meter_index")["display_precision"] == 3
    assert _sensor_options(hass, entry, "consumption_week")["display_precision"] == 3
    assert _sensor_options(hass, entry, "change_vs_last_year_m3")["display_precision"] == 2
    assert _sensor_options(hass, entry, "meter_index")["unit_of_measurement"] == "m³"


async def test_installations_set_up_by_older_version_get_precision_fixed_once(hass: HomeAssistant):
    """Stará verze nastavila litry se třemi desetinnými místy; nová je jednou opraví."""
    entry = _entry_default_units()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_unit_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        for key in ("meter_index", "consumption_week"):       # simulace starého stavu
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
            options = dict(registry.async_get(entity_id).options.get("sensor", {}))
            options["display_precision"] = 3
            registry.async_update_entity_options(entity_id, "sensor", options)
        hass.config_entries.async_update_entry(entry, data={**entry.data, "units_applied": "L"})
        await hass.async_block_till_done()                    # změna dat znovu načte integraci
    assert _sensor_options(hass, entry, "meter_index")["display_precision"] == 0
    assert _sensor_options(hass, entry, "consumption_week")["display_precision"] == 0
    assert entry.data["units_applied"].startswith("l:")


async def test_manual_precision_is_kept_across_restarts(hass: HomeAssistant):
    entry = _entry_default_units()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_unit_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_consumption_month")
        options = dict(registry.async_get(entity_id).options.get("sensor", {}))
        options["display_precision"] = 2                       # uživatel si přesnost změní
        registry.async_update_entity_options(entity_id, "sensor", options)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert _sensor_options(hass, entry, "consumption_month")["display_precision"] == 2   # nepřepsáno


# ---------------------------------------------------------------------------
# Období srovnání
# ---------------------------------------------------------------------------


def _expected_period(months: int) -> str:
    """Totéž, co senzor, spočítané v testu z dneška (měsíc dneška je neúplný a vynechá se)."""
    today = dt_util.now().date().replace(day=1)

    def back(n: int) -> tuple[int, int]:
        index = today.year * 12 + today.month - 1 - n
        return index // 12, index % 12 + 1

    (y1, m1), (y2, m2) = back(months), back(1)
    def span(ya, ma, yb, mb):
        if (ya, ma) == (yb, mb):
            return f"{ma}/{ya}"
        if ya == yb:
            return f"{ma}–{mb}/{ya}"
        return f"{ma}/{ya}–{mb}/{yb}"
    return f"{span(y1, m1, y2, m2)} vs {span(y1 - 1, m1, y2 - 1, m2)}"


async def _period_state(hass, entry):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_compare_period")
    return registry.async_get(entity_id), hass.states.get(entity_id)


async def test_comparison_period_sensor_shows_which_months_are_compared(hass: HomeAssistant):
    entry, _ = await _setup_with_monthly(hass, _monthly_back_from_today(15.0, 10.0))
    record, state = await _period_state(hass, entry)
    assert record.entity_category is None            # je ve Senzorech, vedle obou změn
    assert state.state == _expected_period(3)
    assert " vs " in state.state


async def test_comparison_period_follows_the_configured_number_of_months(hass: HomeAssistant):
    for months in (1, 6):
        entry, _ = await _setup_with_monthly(
            hass, _monthly_back_from_today(15.0, 10.0), options={CONF_COMPARE_MONTHS: months}
        )
        _, state = await _period_state(hass, entry)
        assert state.state == _expected_period(months)
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()


async def test_comparison_period_unknown_without_last_years_data(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    _, state = await _period_state(hass, entry)
    assert state.state == "unknown"


async def test_comparison_period_moves_from_diagnostics_to_sensors_for_existing_entities(hass: HomeAssistant):
    """Instalace, která měla senzor v Diagnostice, ho po aktualizaci má ve Senzorech."""
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_compare_period", config_entry=entry,
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    data = _data()
    data.monthly_m3 = _monthly_back_from_today(15.0, 10.0)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    record, _ = await _period_state(hass, entry)
    assert record.entity_category is None


def test_the_three_comparison_sensors_sort_next_to_each_other():
    import json

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    sensors = json.loads(
        (root / "custom_components/vhs_benesov/translations/cs.json").read_text("utf-8")
    )["entity"]["sensor"]
    names = [sensors[k]["name"] for k in ("change_vs_last_year", "change_vs_last_year_m3", "compare_period")]
    assert all(n.startswith("Změna oproti loňsku") for n in names)
    assert names == sorted(names, key=str.casefold)


def test_month_sensor_is_zero_when_new_month_not_yet_in_portal_overview():
    """První den měsíce portál ještě nemá sloupec; nula, ne "neznámý"."""
    from custom_components.vhs_benesov import sensor

    data = api.MeterData(monthly_m3=[api.DayValue(date(2026, 8, 1), 24.0), api.DayValue(date(2026, 9, 1), 21.2)])
    with patch.object(sensor, "_today", return_value=date(2026, 10, 1)):
        assert sensor._this_month_m3(data) == 0.0
    with patch.object(sensor, "_today", return_value=date(2026, 9, 15)):
        assert sensor._this_month_m3(data) == 21.2


def test_month_sensor_is_unknown_without_overview():
    """Prázdný přehled (výpadek portálu) není nula."""
    from custom_components.vhs_benesov import sensor

    with patch.object(sensor, "_today", return_value=date(2026, 10, 1)):
        assert sensor._this_month_m3(api.MeterData()) is None
        # Přehled jen z budoucnosti neodpovídá aktuálnímu měsíci, tak se nula nevrací.
        assert sensor._this_month_m3(api.MeterData(monthly_m3=[api.DayValue(date(2026, 11, 1), 5.0)])) is None


def test_week_sensor_is_zero_when_the_new_days_are_not_yet_published():
    from custom_components.vhs_benesov import sensor

    data = api.MeterData(daily_liters=[api.DayValue(date(2026, 9, 27), 500.0)])   # neděle
    with patch.object(sensor, "_today", return_value=date(2026, 9, 28)):              # pondělí
        assert sensor._this_week_m3(data) == 0.0
        assert sensor._this_week_m3(api.MeterData()) is None                          # bez dat


def test_manifest_has_what_hacs_and_hassfest_require():
    import json
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "custom_components/vhs_benesov/manifest.json").read_text("utf-8"))
    keys = list(manifest)
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])                  # hassfest: za doménou a názvem abecedně
    for key in ("codeowners", "documentation", "issue_tracker", "iot_class", "version", "config_flow"):
        assert manifest[key], key
    assert manifest["codeowners"] == ["@joazif"]
    assert (root / "LICENSE").read_text("utf-8").startswith("MIT License")
    hacs = json.loads((root / "hacs.json").read_text("utf-8"))
    assert hacs["zip_release"] is True and hacs["filename"] == "vhs_benesov.zip"
    assert (root / "custom_components/vhs_benesov/brand/icon.png").exists()


async def test_option_stored_with_capital_L_by_older_setup_still_means_litres(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="0000000000",
        data={CONF_USERNAME: "0000000000", CONF_PASSWORD: "heslo"},
        options={CONF_UNITS: "L"},
    )
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert _sensor_options(hass, entry, "meter_index")["unit_of_measurement"] == "L"
    assert entry.data["units_applied"].startswith("l:")


def test_selector_option_keys_in_translations_satisfy_hassfest():
    """Hassfest vyžaduje u klíčů výběrů [a-z0-9_-]+, nezačínat ani nekončit pomlčkou či podtržítkem."""
    import json
    import pathlib
    import re

    base = pathlib.Path(__file__).resolve().parents[1] / "custom_components/vhs_benesov"
    for name in ("strings.json", "translations/cs.json", "translations/en.json"):
        selectors = json.loads((base / name).read_text("utf-8"))["selector"]
        for selector in selectors.values():
            for key in selector["options"]:
                assert re.fullmatch(r"[a-z0-9]([a-z0-9_-]*[a-z0-9])?", key), (name, key)


async def test_history_status_shows_when_the_import_finished(hass: HomeAssistant):
    entry = _entry_with_history()
    entry.add_to_hass(hass)
    await _setup(hass, entry, _history())
    status = _history_status(hass, entry)
    stamp = datetime.strptime(status.attributes["dokonceno"], "%d.%m.%Y %H:%M")
    assert status.state == f"hotovo ({status.attributes['dokonceno']})"
    assert abs(stamp - dt_util.now().replace(tzinfo=None)) < timedelta(minutes=5)

    saved = await storage.Store(hass, 1, f"{DOMAIN}.history.{entry.entry_id}").async_load()
    assert saved["finished"] > 0
    # Záznam z dřívější verze bez času dokončení: ukáže se čas začátku posledního běhu.
    store = storage.Store(hass, 1, f"{DOMAIN}.history.{entry.entry_id}")
    legacy = {k: v for k, v in saved.items() if k != "finished"}
    legacy["last_run"] = saved["finished"] - 3600
    await store.async_save(legacy)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await _setup(hass, entry, _history())
    older = _history_status(hass, entry)
    assert older.state.startswith("hotovo (") and older.state != status.state


# ---------------------------------------------------------------------------
# Průběžné doplňování externí statistiky
# ---------------------------------------------------------------------------


def _series_data(days: int, complete: int, *, start_offset: int = 0) -> api.MeterData:
    """Měsíční data portálu: stav po dnech a křivka úplná u prvních ``complete`` dnů.

    Poslední den řady je odhad ze stavu posledního odečtu. Dny končí dneškem minus ``start_offset``.
    """
    today = dt_util.now().date() - timedelta(days=start_offset)
    first = today - timedelta(days=days - 1)
    data = _data()
    data.daily_index_m3 = [
        api.DayValue(first + timedelta(days=i), 100.0 + 0.5 * (i + 1)) for i in range(days)
    ]
    data.curve_liters = [
        api.PointValue(datetime(d.year, d.month, d.day, h), 125.0)
        for d in (first + timedelta(days=i) for i in range(complete))
        for h in (0, 6, 12, 18)
    ]
    data.reading_m3 = data.daily_index_m3[-1].value
    data.last_reading = datetime(today.year, today.month, today.day, 5, 42)
    return data


async def _refresh(hass, entry, data):
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_series_starts_from_current_data_even_when_history_is_declined(hass: HomeAssistant):
    entry = _entry_with_history(option=False)
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_series_data(6, 5)), patch(
        f"{CLIENT}.async_fetch_history", return_value=_history()
    ) as fetch:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert fetch.call_count == 0                            # celá historie se nestahovala
    rows = await _stats(hass, STAT_ID)
    assert len(rows) == 5 * 24
    assert rows[-1]["sum"] == pytest.approx(0.5 * 5)
    assert all(b["sum"] >= a["sum"] for a, b in zip(rows, rows[1:]))


async def test_new_portal_data_extends_the_series_without_a_jump(hass: HomeAssistant):
    entry = _entry_with_history(option=False)
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_series_data(6, 5)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
    first = await _stats(hass, STAT_ID)

    # Portál zveřejnil další den: o jeden den víc ve stavech i v křivce.
    await _refresh(hass, entry, _series_data(7, 6, start_offset=-1))
    second = await _stats(hass, STAT_ID)
    assert len(second) > len(first)
    assert [r["start"] for r in second[: len(first)]] == [r["start"] for r in first]
    for old, new in zip(first, second, strict=False):
        assert new["sum"] == pytest.approx(old["sum"])        # zapsané řádky se nezměnily
    assert all(b["sum"] >= a["sum"] for a, b in zip(second, second[1:]))
    assert second[-1]["sum"] == pytest.approx(0.5 * 6)


async def test_unchanged_portal_data_does_not_rewrite_the_statistic(hass: HomeAssistant):
    from custom_components.vhs_benesov import importer as importer_module

    entry = _entry_with_history(option=False)
    entry.add_to_hass(hass)
    data = _series_data(6, 5)
    with patch(f"{CLIENT}.async_fetch_all", return_value=data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
    with patch.object(
        importer_module, "async_add_external_statistics", wraps=importer_module.async_add_external_statistics
    ) as write:
        await _refresh(hass, entry, data)
    assert write.call_count == 0


async def test_old_style_history_triggers_one_full_import_into_the_new_statistic(
    hass: HomeAssistant,
):
    """Instalace po starší verzi (historie ve statistice senzoru, bez kontrolního bodu)."""
    entry = _entry_with_history(option=False)
    entry.add_to_hass(hass)
    await storage.Store(hass, 1, f"{DOMAIN}.history.{entry.entry_id}").async_save(
        {"done": True, "live_from": 1.0, "anchor": 905.0, "last_ok": True,
         "last_run": (dt_util.utcnow() - timedelta(days=3)).timestamp()}
    )
    fetch = await _setup(hass, entry, _history())
    assert fetch.call_count == 1
    assert len(await _stats(hass, STAT_ID)) == 9 * 24
    saved = await storage.Store(hass, 1, f"{DOMAIN}.history.{entry.entry_id}").async_load()
    assert saved["series_day"]


async def test_old_style_history_waits_for_the_cooldown_without_nagging(hass: HomeAssistant):
    entry = _entry_with_history(option=False)
    entry.add_to_hass(hass)
    await storage.Store(hass, 1, f"{DOMAIN}.history.{entry.entry_id}").async_save(
        {"done": True, "anchor": 905.0, "last_ok": True,
         "last_run": (dt_util.utcnow() - timedelta(hours=2)).timestamp()}
    )
    fetch = await _setup(hass, entry, _history())
    assert fetch.call_count == 0                            # ještě je odpočinek
    assert not any(
        "nedávno" in n["message"]
        for n in hass.data.get("persistent_notification", {}).values()
    )
    # Po odpočinku se import při další kontrole spustí sám.
    with _at(timedelta(hours=23)), patch(f"{CLIENT}.async_fetch_all", return_value=_meter_data()), patch(
        f"{CLIENT}.async_fetch_history", return_value=_history()
    ) as later:
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
    assert later.call_count == 1
    assert len(await _stats(hass, STAT_ID)) == 9 * 24


async def test_month_turn_fetches_the_previous_month_once_to_finish_its_last_steps(
    hass: HomeAssistant,
):
    entry = _entry_with_history(option=False)
    entry.add_to_hass(hass)
    today = dt_util.now().date()
    first_of_month = today.replace(day=1)
    prev_month_day = first_of_month - timedelta(days=1)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    # Kontrolní bod zůstal na posledním dni minulého měsíce.
    # Přes úložiště importéru (jiná instance by měla vlastní mezipaměť).
    await entry.runtime_data.history._store.async_save(
        {"done": True, "written_to": 1.0, "series_day": prev_month_day.isoformat(),
         "series_state": 100.0, "series_total": 5.0}
    )
    extra = api.HistoryData(
        index_end={prev_month_day: 100.5},
        curve_liters={
            datetime(prev_month_day.year, prev_month_day.month, prev_month_day.day, h): 125.0
            for h in (0, 6, 12, 18)
        },
        months=1, first_month=prev_month_day.replace(day=1), last_month=prev_month_day.replace(day=1),
    )
    # Nový měsíc zatím jen se stavem posledního odečtu, bez křivky.
    data = _data()
    data.daily_index_m3 = [api.DayValue(first_of_month, 100.7)]
    data.curve_liters = []
    data.last_reading = datetime(first_of_month.year, first_of_month.month, first_of_month.day, 5, 42)
    with patch(f"{CLIENT}.async_fetch_history", return_value=extra) as fetch:
        await _refresh(hass, entry, data)
    assert fetch.call_count == 1
    assert fetch.call_args.kwargs["only_months"] == [prev_month_day.replace(day=1)]
    rows = await _stats(hass, STAT_ID)
    assert rows and rows[-1]["sum"] == pytest.approx(5.5)    # navázáno na uložený součet
    saved = await storage.Store(hass, 1, f"{DOMAIN}.history.{entry.entry_id}").async_load()
    assert saved["series_day"] == first_of_month.isoformat()  # minulý měsíc je hotový


async def test_long_outage_triggers_a_full_import_instead_of_catching_up(hass: HomeAssistant):
    from custom_components.vhs_benesov.importer import MAX_CATCH_UP_MONTHS

    entry = _entry_with_history(option=False)
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    old = dt_util.now().date() - timedelta(days=31 * (MAX_CATCH_UP_MONTHS + 2))
    # Přes úložiště importéru (jiná instance by měla vlastní mezipaměť).
    await entry.runtime_data.history._store.async_save(
        {"done": True, "written_to": 1.0, "series_day": old.isoformat(),
         "series_state": 100.0, "series_total": 5.0}
    )
    with patch(f"{CLIENT}.async_fetch_history", return_value=_history()) as fetch:
        await _refresh(hass, entry, _series_data(3, 2))
    assert fetch.call_count == 1 and not fetch.call_args.kwargs.get("only_months")
    assert len(await _stats(hass, STAT_ID)) == 9 * 24


async def test_statistic_id_comes_from_the_meter_number():
    from custom_components.vhs_benesov.importer import statistic_id_for

    assert statistic_id_for("12345-XX-0000001", "abc") == "vhs_benesov:12345_xx_0000001_consumption"
    assert statistic_id_for(None, "A1b2") == "vhs_benesov:a1b2_consumption"


async def test_automatic_retry_after_a_failed_import_is_slow_not_hourly(hass: HomeAssistant):
    entry = _entry_with_history()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_meter_data()), patch(
        f"{CLIENT}.async_fetch_history", side_effect=api.VhsError("portál nejede")
    ) as failing:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert failing.call_count == 1

    async def refresh(delta, history):
        with _at(delta), patch(f"{CLIENT}.async_fetch_all", return_value=_meter_data()), patch(
            f"{CLIENT}.async_fetch_history", return_value=history, side_effect=None
        ) as fetch:
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done(wait_background_tasks=True)
            await async_wait_recording_done(hass)
        return fetch.call_count

    assert await refresh(timedelta(hours=1), _history()) == 0       # příliš brzy po chybě
    assert await refresh(timedelta(hours=7), _history()) == 1       # po odpočinku se zkusí znovu
    assert len(await _stats(hass, STAT_ID)) == 9 * 24


async def test_reading_shows_when_the_integration_first_saw_it(hass: HomeAssistant):
    entry = _entry_with_history(option=False)
    entry.add_to_hass(hass)
    first = _series_data(6, 5)
    with patch(f"{CLIENT}.async_fetch_all", return_value=first):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_last_reading"
    )
    # První odečet po spuštění: kdy se objevil na portálu, se neví.
    assert "zverejneno" not in hass.states.get(entity_id).attributes

    newer = _series_data(7, 6, start_offset=-1)               # další odečet, o den později
    newer.last_reading = first.last_reading + timedelta(hours=12)
    await _refresh(hass, entry, newer)
    attrs = hass.states.get(entity_id).attributes
    seen = datetime.fromisoformat(attrs["zverejneno"])
    assert abs(seen - dt_util.now()) < timedelta(minutes=5)
    reading = newer.last_reading.replace(tzinfo=dt_util.get_default_time_zone())
    assert attrs["zpozdeni_hodin"] == pytest.approx((seen - reading).total_seconds() / 3600, abs=0.1)

    # Po restartu integrace údaj zůstane.
    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch(f"{CLIENT}.async_fetch_all", return_value=newer):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(entity_id).attributes["zverejneno"] == attrs["zverejneno"]


async def test_redirect_loop_error_is_retried_not_reauth(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.async_fetch_all", return_value=_data()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    with patch(f"{CLIENT}.async_fetch_all", side_effect=api.VhsError("Příliš mnoho přesměrování u Accueil.aspx")):
        for _ in range(5):
            await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert not any(
        f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress()
    )
