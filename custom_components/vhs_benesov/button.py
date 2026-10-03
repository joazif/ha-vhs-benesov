"""Tlačítko pro ruční aktualizaci dat."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import VhsBenesovConfigEntry
from .coordinator import VhsBenesovCoordinator
from .entity import VhsBenesovEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VhsBenesovConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Vytvořit tlačítko aktualizace."""
    async_add_entities(
        [
            RefreshButton(entry.runtime_data, entry.entry_id),
            RefreshHistoryButton(entry.runtime_data, entry.entry_id),
        ]
    )


class RefreshButton(VhsBenesovEntity, ButtonEntity):
    """Stáhne data hned, bez čekání na další kontrolu."""

    _attr_translation_key = "refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: VhsBenesovCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_refresh"

    async def async_press(self) -> None:
        """Stáhnout data znovu."""
        await self.coordinator.async_refresh_full()


class RefreshHistoryButton(VhsBenesovEntity, ButtonEntity):
    """Znovu stáhne historii z portálu a zapíše ji do statistik.

    Bezpečné opakovat: hodnoty jsou vždy stejné a živá data se nepřepisují.
    """

    _attr_translation_key = "refresh_history"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator: VhsBenesovCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_refresh_history"

    async def async_press(self) -> None:
        """Spustit import na pozadí; tlačítko nečeká na dokončení."""
        importer = self.coordinator.history
        if importer is None or importer.running:
            return
        self.coordinator.config_entry.async_create_background_task(
            self.hass, importer.async_import(), "vhs_benesov_history_manual"
        )
