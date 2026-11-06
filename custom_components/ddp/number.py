"""The controller device's "Intensity" number: the controller's own brightness for its scenes, in percent."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DDPConfigEntry
from .entity import ControllerEntity, controller_device_parent

# Commands serialize on the coordinator lock.
PARALLEL_UPDATES = 0

INTENSITY_KEY = "intensity"  # unique-ID suffix


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DDPConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Only a vendor controller type has a controller device (Generic DDP's was removed with its entities)."""
    if (commands := entry.runtime_data.commands) is not None:
        async_add_entities(
            [
                IntensityNumber(
                    entry,
                    commands,
                    INTENSITY_KEY,
                    controller_device_parent(hass, entry),
                )
            ]
        )


class IntensityNumber(ControllerEntity, NumberEntity):
    """A setting (entity category config): it neither starts nor stops a scene, and DDP keeps the pixels."""

    _attr_translation_key = "intensity"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> int | None:
        return self.scene.intensity if self.scene is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self._commands.set_intensity(self.entity_id, round(value))
