"""Aktualizace dat z portálu dálkových odečtů."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import InvalidAuth, MeterData, VhsBenesovClient, VhsError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Nejkratší odstup mezi ručními aktualizacemi (tlačítko), v sekundách.
REFRESH_COOLDOWN = 60.0
# Jak často se zjišťuje, jestli portál zveřejnil nový odečet. Je to jeden požadavek (úvodní
# stránka); celé stahování (pět stránek) běží, jen když se odečet změní.
CHECK_INTERVAL = timedelta(hours=1)
# Celé stažení aspoň jednou za tuto dobu i bez změny odečtu, kdyby kontrola změnu přehlédla
# (portál občas hodnotu přeskočí).
FULL_REFRESH_AFTER = timedelta(hours=24)
# Kolikrát po sobě musí portál odmítnout přihlášení, než Home Assistant vyžádá nové heslo.
# Portál občas přihlášení odmítne i se správným heslem, jednorázové odmítnutí se proto bere
# jako výpadek. Při prvním načtení (bez dat) se neodkládá, nové heslo se pozná hned.
AUTH_FAILURES_BEFORE_REAUTH = 3


class VhsBenesovCoordinator(DataUpdateCoordinator[MeterData]):
    """Stahuje spotřebu a stav vodoměru."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: VhsBenesovClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=CHECK_INTERVAL,
            # Tlačítko Aktualizovat je jeden požadavek za minutu nejvýš; výchozích
            # 10 s by dovolilo portál zbytečně zatěžovat (pět stránek na stisk).
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=REFRESH_COOLDOWN, immediate=True
            ),
        )
        self.client = client
        # Čas posledního úspěšného kontaktu s portálem (místní): kontroly i celá stažení.
        self.last_success: datetime | None = None
        # Čas posledního celého stažení dat; None do prvního úspěchu.
        self.last_full: datetime | None = None
        self._force_full = False
        # Volá se s novými daty po každém úspěšném načtení (doplňování statistiky spotřeby).
        self.on_new_data: Callable[[MeterData], None] | None = None
        self._auth_failures = 0
        # Doplní se v async_setup_entry; None dokud import historie neexistuje.
        self.history = None

    async def async_refresh_full(self) -> None:
        """Stáhnout vše hned, bez ohledu na to, jestli se odečet změnil (tlačítko)."""
        self._force_full = True
        await self.async_request_refresh()

    async def _async_fetch(self) -> MeterData:
        """Celé stažení jen při změně odečtu, jinak stačí jedna lehká kontrola."""
        now = dt_util.now()
        if (
            self.data is not None
            and not self._force_full
            and self.last_full is not None
            and now - self.last_full < FULL_REFRESH_AFTER
        ):
            probe = await self.client.async_probe()
            if probe.known and (probe.last_reading, probe.reading_m3) == (
                self.data.last_reading,
                self.data.reading_m3,
            ):
                return self.data
            _LOGGER.debug("Portál ukazuje nový odečet, stahuji data")
        data = await self.client.async_fetch_all()
        self._force_full = False
        self.last_full = now
        return data

    async def _async_update_data(self) -> MeterData:
        try:
            data = await self._async_fetch()
        except InvalidAuth as err:
            self._auth_failures += 1
            if self.data is None or self._auth_failures >= AUTH_FAILURES_BEFORE_REAUTH:
                raise ConfigEntryAuthFailed(str(err)) from err
            _LOGGER.warning(
                "Portál odmítl přihlášení (%s z %s), zkusím znovu při další kontrole",
                self._auth_failures,
                AUTH_FAILURES_BEFORE_REAUTH,
            )
            raise UpdateFailed(str(err)) from err
        except VhsError as err:
            raise UpdateFailed(str(err)) from err
        self._auth_failures = 0
        self.last_success = dt_util.now()
        if self.on_new_data is not None:
            self.on_new_data(data)
        return data
