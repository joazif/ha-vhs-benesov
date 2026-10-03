"""Společný základ entit."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import BASE
from .const import DOMAIN
from .coordinator import VhsBenesovCoordinator


class VhsBenesovEntity(CoordinatorEntity[VhsBenesovCoordinator]):
    """Entita navázaná na jedno přihlášení (jeden vodoměr)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: VhsBenesovCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=coordinator.data.meter_id or "Vodoměr",
            manufacturer="Vodohospodářská společnost Benešov",
            model="Dálkový odečet vodoměru",
            serial_number=coordinator.data.meter_id,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=BASE,
        )
