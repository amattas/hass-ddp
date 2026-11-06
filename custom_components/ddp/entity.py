"""Base entity: the DDP lights belong to the DDP device; a vendor controller's Scenes light to the controller device."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_HOST
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import (
    CONF_CONTROLLER_TYPE,
    CONF_FIRMWARE,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONTROLLER_DEVICE_SUFFIX,
    DOMAIN,
    PATTERN_EFFECT,
    RGB,
)
from .controller import CONTROLLER_TYPES

if TYPE_CHECKING:
    from . import DDPConfigEntry


def device_identifiers(host: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """The identifiers of an entry's DDP device and of its controller device (vendor controller types only)."""
    return (DOMAIN, host), (DOMAIN, f"{host}{CONTROLLER_DEVICE_SUFFIX}")


def ddp_device_info(entry: DDPConfigEntry) -> DeviceInfo:
    """The DDP device: the DDP lights' device, named after the entry."""
    return DeviceInfo(
        identifiers={device_identifiers(entry.data[CONF_HOST])[0]},
        name=entry.title,
        manufacturer=entry.data.get(CONF_MANUFACTURER),
        model=entry.data.get(CONF_MODEL),
        sw_version=entry.data.get(CONF_FIRMWARE),
    )


class DDPEntity(Entity):
    """Unique IDs are ``<entry_id>_<key>``. The entity is on the DDP device, or with ``ddp_device_id`` (that device's
    registry ID) on the controller device, named after the entry and the controller type and connected through it."""

    _attr_has_entity_name = True

    def __init__(self, entry: DDPConfigEntry, key: str, *, ddp_device_id: str | None = None) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = ddp_device_info(entry)
        if ddp_device_id is not None:
            label = CONTROLLER_TYPES[entry.data[CONF_CONTROLLER_TYPE]].label
            self._attr_device_info |= DeviceInfo(
                identifiers={device_identifiers(entry.data[CONF_HOST])[1]},
                name=f"{entry.title} {label}",
                via_device_id=ddp_device_id,
            )

    async def async_recall_preset(self, preset: int) -> None:
        """The ``recall_preset`` action: only the Scenes light of a vendor controller type can."""
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_controller",
            translation_placeholders={"entity": self.entity_id},
        )

    async def async_set_colors(self, colors: Sequence[RGB], background: RGB | None = None) -> None:
        """The ``set_colors`` action: only the Scenes light of a vendor controller type can."""
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="palette_not_a_controller",
            translation_placeholders={"entity": self.entity_id},
        )

    async def async_set_pattern(
        self,
        colors: Sequence[Sequence[int]],
        effect: str = PATTERN_EFFECT,
        block_size: int | None = None,
        params: Mapping[str, Any] | None = None,
        brightness: int | None = None,
    ) -> None:
        """The ``set_pattern`` action: only a DDP light (an output or All outputs) can."""
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_ddp_light",
            translation_placeholders={"entity": self.entity_id},
        )
