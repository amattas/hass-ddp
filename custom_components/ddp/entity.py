"""Base entity: every DDP entity belongs to the one controller device."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONF_FIRMWARE, CONF_MANUFACTURER, CONF_MODEL, DOMAIN

if TYPE_CHECKING:
    from . import DDPConfigEntry


class DDPEntity(Entity):
    """Shares the controller device. Unique IDs are ``<entry_id>_<key>``."""

    _attr_has_entity_name = True

    def __init__(self, entry: DDPConfigEntry, key: str) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_HOST])},
            name=entry.title,
            manufacturer=entry.data.get(CONF_MANUFACTURER),
            model=entry.data.get(CONF_MODEL),
            sw_version=entry.data.get(CONF_FIRMWARE),
        )

    async def async_recall_preset(self, preset: int) -> None:
        """The ``recall_preset`` action: only the Scenes light of a vendor controller type can."""
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_controller",
            translation_placeholders={"entity": self.entity_id},
        )
