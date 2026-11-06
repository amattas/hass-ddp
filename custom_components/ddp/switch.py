"""The controller device's main entity, "Power" (named like the device): on while the controller runs a scene.

turn_on runs the last scene (else the first), turn_off the controller's "Off". It also takes ``ddp.recall_preset``
and ``ddp.set_colors``, and shows the controller's palette as the ``colors`` and ``background`` attributes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoredExtraData,
    RestoreEntity,
)

from . import DDPConfigEntry
from .const import ATTR_BACKGROUND, ATTR_COLORS, CONTROLLER_OFF, RGB
from .entity import ControllerEntity, controller_device_parent

# Commands serialize on the coordinator lock.
PARALLEL_UPDATES = 0

POWER_KEY = "power"  # unique-ID suffix
ATTR_LAST_EFFECT = "last_effect"  # restore data: the last scene, for a bare turn_on after a restart


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DDPConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Only a vendor controller type has a controller device (Generic DDP's was removed with its entities)."""
    if (commands := entry.runtime_data.commands) is not None:
        async_add_entities([PowerSwitch(entry, commands, POWER_KEY, controller_device_parent(hass, entry))])


class PowerSwitch(ControllerEntity, SwitchEntity, RestoreEntity):
    """The controller's on/off. The controller is the truth (polled); restoring never commands it."""

    _attr_name = None  # the device's main entity; the translation key gives the icon and attribute names
    _attr_translation_key = "power"

    async def async_added_to_hass(self) -> None:
        """Remember the last scene for a bare turn_on (the coordinator's ``last_effect``, which Power and the Scene
        select share)."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        last = extra.as_dict().get(ATTR_LAST_EFFECT) if extra is not None else None
        if self.coordinator.last_effect is None and isinstance(last, str) and last != CONTROLLER_OFF:
            self.coordinator.last_effect = last

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        return RestoredExtraData({ATTR_LAST_EFFECT: self.coordinator.last_effect})

    @property
    def is_on(self) -> bool:
        return self.scene_on

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The palette, once it has been read."""
        state = self.scene
        if state is None or state.background is None:
            return None
        return {
            ATTR_COLORS: [list(color) for color in state.colors],
            ATTR_BACKGROUND: list(state.background),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._commands.turn_on(self.entity_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._commands.turn_off(self.entity_id)

    async def async_recall_preset(self, preset: int) -> None:
        """The ``recall_preset`` action."""
        await self._commands.recall_preset(self.entity_id, preset)

    async def async_set_colors(self, colors: Sequence[RGB], background: RGB | None = None) -> None:
        """The ``set_colors`` action. The palette is the controller's own setting, so DDP keeps the pixels and Power
        need not be on."""
        await self._commands.set_palette(self.entity_id, colors, background)
