"""The controller device's selects: "Scene" (the controller's built-in effects, the running one selected) and
"Mode 1".."Mode 5" (what each palette slot holds: a custom color or one of the controller's whites)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DDPConfigEntry
from .commands import MODE_WHITES, SLOT_MODES, ControllerCommands, slot_mode
from .controller import DEFAULT_CUSTOM_COLOR, PALETTE_SLOTS
from .entity import ControllerEntity, controller_device_parent

# Commands serialize on the coordinator lock.
PARALLEL_UPDATES = 0

SCENE_KEY = "scene"  # unique-ID suffix
MODE_KEY = "mode"  # unique-ID suffix of a slot's mode, with its slot: mode_1 .. mode_5


async def async_setup_entry(
    hass: HomeAssistant, entry: DDPConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Only a vendor controller type has a controller device (Generic DDP's was removed with its entities)."""
    if (commands := entry.runtime_data.commands) is not None:
        parent = controller_device_parent(hass, entry)
        async_add_entities(
            [
                SceneSelect(entry, commands, SCENE_KEY, parent),
                *(ModeSelect(entry, commands, parent, slot) for slot in range(1, PALETTE_SLOTS + 1)),
            ]
        )


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


class ModeSelect(ControllerEntity, SelectEntity):
    """A palette slot's mode, read from the slot (#c69740 or #ffffff Warm White, #feffff Cool White, anything else,
    black included, Color). A white is sent at once; Color, from a white, re-sends the slot's last custom color
    (default red), and otherwise leaves the slot as it is."""

    _attr_translation_key = "mode"
    _attr_options = SLOT_MODES

    def __init__(self, entry: DDPConfigEntry, commands: ControllerCommands, ddp_device_id: str, slot: int) -> None:
        super().__init__(entry, commands, f"{MODE_KEY}_{slot}", ddp_device_id)
        self._slot = slot
        self._attr_translation_placeholders = {"slot": str(slot)}

    @property
    def current_option(self) -> str | None:
        colors = self.scene.colors if self.scene is not None else ()
        return slot_mode(colors[self._slot - 1]) if len(colors) >= self._slot else None

    async def async_select_option(self, option: str) -> None:
        if option in MODE_WHITES:
            await self._commands.set_color(self.entity_id, self._slot, MODE_WHITES[option])
        elif self.current_option in MODE_WHITES:  # an HA scene restoring Mode then Color never sees a cleared slot
            last = self.coordinator.last_custom.get(self._slot, DEFAULT_CUSTOM_COLOR)
            await self._commands.set_color(self.entity_id, self._slot, last)
