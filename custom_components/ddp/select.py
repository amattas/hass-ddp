"""The controller device's "Scene" select: the controller's built-in effects, the running one selected."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DDPConfigEntry
from .entity import ControllerEntity, controller_device_parent

# Commands serialize on the coordinator lock.
PARALLEL_UPDATES = 0

SCENE_KEY = "scene"  # unique-ID suffix


async def async_setup_entry(
    hass: HomeAssistant, entry: DDPConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Only a vendor controller type has a controller device (Generic DDP's was removed with its entities)."""
    if (commands := entry.runtime_data.commands) is not None:
        async_add_entities([SceneSelect(entry, commands, SCENE_KEY, controller_device_parent(hass, entry))])


class SceneSelect(ControllerEntity, SelectEntity):
    """Options are the controller's effects without "Off"; none is selected while the controller is off. Selecting
    one runs it (and hands the pixels back from DDP)."""

    _attr_translation_key = "scene"

    @property
    def options(self) -> list[str]:
        return self._commands.effects

    @property
    def current_option(self) -> str | None:
        return self.scene.effect if self.scene is not None and self.scene_on else None

    async def async_select_option(self, option: str) -> None:
        await self._commands.turn_on(self.entity_id, option)
