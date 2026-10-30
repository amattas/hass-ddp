"""One light per segment: on/off, brightness, RGB color, and pyddp effects, restored and re-sent on startup."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.light import LightEntity
from homeassistant.components.light.const import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    EFFECT_OFF,
    ColorMode,
    LightEntityFeature,
)
from homeassistant.const import CONF_HOST, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from pyddp import EffectInfo, EffectRunner, PyDDPError, list_effects

from . import DDPConfigEntry, DDPRuntime, SegmentConfig
from .const import CONF_FIRMWARE, CONF_MANUFACTURER, CONF_MODEL, DOMAIN, SOLID_EFFECT

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0  # sends are local UDP writes; nothing to throttle

DEFAULT_BRIGHTNESS = 255
DEFAULT_RGB: tuple[int, int, int] = (255, 255, 255)

EFFECTS: dict[str, EffectInfo] = {info.name: info for info in list_effects() if info.name != SOLID_EFFECT}


async def async_setup_entry(
    hass: HomeAssistant, entry: DDPConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """One light per configured segment."""
    device = DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_HOST])},
        name=entry.title,
        manufacturer=entry.data.get(CONF_MANUFACTURER),
        model=entry.data.get(CONF_MODEL),
        sw_version=entry.data.get(CONF_FIRMWARE),
    )
    async_add_entities(DDPSegmentLight(entry, segment, device) for segment in entry.runtime_data.segments)


def _rgb(value: object) -> tuple[int, int, int] | None:
    if isinstance(value, list | tuple) and len(value) == 3 and all(isinstance(c, int) for c in value):
        return (value[0], value[1], value[2])
    return None


class DDPSegmentLight(LightEntity, RestoreEntity):
    """A segment of the strip. DDP is send-only, so HA's state is the truth."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_assumed_state = True
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = [EFFECT_OFF, *EFFECTS]

    def __init__(self, entry: DDPConfigEntry, config: SegmentConfig, device: DeviceInfo) -> None:
        self._runtime: DDPRuntime = entry.runtime_data
        self._config = config
        self._lock = asyncio.Lock()  # one state change at a time, so runners never double-start
        self._attr_name = config.name
        self._attr_unique_id = f"{entry.entry_id}_{config.key}"
        self._attr_device_info = device
        self._attr_is_on = False
        self._attr_brightness = DEFAULT_BRIGHTNESS
        self._attr_rgb_color = DEFAULT_RGB
        self._attr_effect = EFFECT_OFF
        self._runner_effect: str | None = None  # effect of the runner this entity last started

    async def async_added_to_hass(self) -> None:
        """Restore the last state and, if it was on, send it again (DDP controllers forget on power loss)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        brightness = last.attributes.get(ATTR_BRIGHTNESS)
        if isinstance(brightness, int) and 1 <= brightness <= 255:
            self._attr_brightness = brightness
        self._attr_rgb_color = _rgb(last.attributes.get(ATTR_RGB_COLOR)) or self._attr_rgb_color
        effect = last.attributes.get(ATTR_EFFECT)
        self._attr_effect = effect if effect in EFFECTS else EFFECT_OFF
        if last.state != STATE_ON:
            return
        try:
            async with self._lock:
                await self._send_on()
        except HomeAssistantError as err:
            _LOGGER.warning("Could not restore %s: %s", self.entity_id, err)
            return
        self._attr_is_on = True

    async def async_will_remove_from_hass(self) -> None:
        """Stop this segment's runner; leave the pixels as they are."""
        await self._runtime.stop_runner(self._config.key)
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs: Any) -> None:
        effect = kwargs.get(ATTR_EFFECT, self._attr_effect)
        if effect != EFFECT_OFF and effect not in EFFECTS:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_effect",
                translation_placeholders={"effect": str(effect), "effects": ", ".join(self.effect_list or [])},
            )
        async with self._lock:
            previous = (self._attr_brightness, self._attr_rgb_color, self._attr_effect)
            self._attr_brightness = kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness)
            self._attr_rgb_color = kwargs.get(ATTR_RGB_COLOR, self._attr_rgb_color)
            self._attr_effect = effect
            try:
                await self._send_on()
            except HomeAssistantError:
                self._attr_brightness, self._attr_rgb_color, self._attr_effect = previous
                raise
            self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        async with self._lock:
            await self._runtime.stop_runner(self._config.key)
            try:
                await self._runtime.sender.off(self._config.segment)
            except PyDDPError as err:
                raise self._send_error(err) from err
            self._attr_is_on = False
        self.async_write_ha_state()

    async def _send_on(self) -> None:
        """Send the current brightness, color, and effect. Caller holds the lock."""
        runtime, key, segment = self._runtime, self._config.key, self._config.segment
        brightness = (self._attr_brightness or DEFAULT_BRIGHTNESS) / 255
        rgb = self._attr_rgb_color or DEFAULT_RGB
        info = EFFECTS.get(self._attr_effect or EFFECT_OFF)
        try:
            if info is None:
                await runtime.stop_runner(key)
                await runtime.sender.fill(segment, rgb, brightness=brightness)
                return
            params = info.color_params(rgb)
            runner = runtime.runners.get(key)
            if runner is not None and runner.running and self._runner_effect == info.name:
                runner.update(params=params, brightness=brightness)
                return
            await runtime.stop_runner(key)
            runner = EffectRunner(
                runtime.sender, segment, info.name, fps=runtime.fps, params=params, brightness=brightness
            )
            await runner.start()
            runtime.runners[key] = runner
            self._runner_effect = info.name
        except PyDDPError as err:
            raise self._send_error(err) from err

    def _send_error(self, err: PyDDPError) -> HomeAssistantError:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="send_failed",
            translation_placeholders={"entity": self.entity_id, "error": str(err)},
        )
