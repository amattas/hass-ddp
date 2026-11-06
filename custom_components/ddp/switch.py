"""The controller device's "Power" switch: on while the controller runs a scene, off for its "Off"."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DDPConfigEntry
from .entity import ControllerEntity, controller_device_parent

# Commands serialize on the coordinator lock.
PARALLEL_UPDATES = 0

POWER_KEY = "power"  # unique-ID suffix


async def async_setup_entry(
    hass: HomeAssistant, entry: DDPConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Only a vendor controller type has a controller device (Generic DDP's was removed with its entities)."""
    if (commands := entry.runtime_data.commands) is not None:
        async_add_entities([PowerSwitch(entry, commands, POWER_KEY, controller_device_parent(hass, entry))])


class PowerSwitch(ControllerEntity, SwitchEntity):
    """The controller light's on/off: turn_on runs the last scene (else the first), turn_off the controller's Off."""

    _attr_translation_key = "power"

    @property
    def is_on(self) -> bool:
        return self.scene_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._commands.turn_on(self.entity_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._commands.turn_off(self.entity_id)
