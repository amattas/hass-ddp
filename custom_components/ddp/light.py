"""One light per segment (DDP), plus a "Scenes" light for an NDB-Pro's built-in effects (HTTP).

Segment lights: on/off, brightness, RGB color, and pyddp effects, restored and re-sent on startup. The Scenes light
selects the controller's built-in effects and stored presets; DDP data overrides those while it flows, so it turns the
segment lights off (without blanking) and releases the sender before commanding the controller.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import probatio
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
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback, async_get_current_platform
from homeassistant.helpers.restore_state import ExtraStoredData, RestoredExtraData, RestoreEntity
from pyddp import (
    ControllerError,
    DDPQueryError,
    EffectInfo,
    EffectRunner,
    NDBController,
    NDBState,
    PyDDPError,
    list_effects,
)

from . import DDPConfigEntry, DDPRuntime, SegmentConfig
from .const import CONF_FIRMWARE, CONF_MANUFACTURER, CONF_MODEL, DOMAIN, SOLID_EFFECT

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0  # sends are local UDP writes; the one polled entity per entry is the Scenes light
SCAN_INTERVAL = timedelta(seconds=30)  # Scenes light polls the controller's UI; segment lights never poll

DEFAULT_BRIGHTNESS = 255
DEFAULT_RGB: tuple[int, int, int] = (255, 255, 255)

EFFECTS: dict[str, EffectInfo] = {info.name: info for info in list_effects() if info.name != SOLID_EFFECT}

CONTROLLER_OFF = "Off"  # the NDB-Pro's built-in effect that blanks the lights; the Scenes light's off, not an effect
SERVICE_RECALL_PRESET = "recall_preset"
ATTR_PRESET = "preset"
PRESET_COUNT = 20
ATTR_LAST_EFFECT = "last_effect"


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
    runtime = entry.runtime_data
    runtime.segment_lights = [DDPSegmentLight(entry, segment, device) for segment in runtime.segments]
    async_add_entities(runtime.segment_lights)
    if runtime.controller is None:
        return
    # First read at setup; if the controller doesn't answer, the light starts unavailable and the poll retries.
    async_add_entities([NDBScenesLight(entry, runtime.controller, device)], update_before_add=True)
    async_get_current_platform().async_register_entity_service(
        SERVICE_RECALL_PRESET,
        {probatio.Required(ATTR_PRESET): probatio.All(probatio.Coerce(int), probatio.Range(min=1, max=PRESET_COUNT))},
        _async_recall_preset,
    )


async def _async_recall_preset(entity: Entity, call: ServiceCall) -> None:
    if not isinstance(entity, NDBScenesLight):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_controller",
            translation_placeholders={"entity": entity.entity_id},
        )
    await entity.async_recall_preset(call.data[ATTR_PRESET])


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
        # HA blanks an off light's attributes, so the last look comes from extra data when the light was off.
        extra = await self.async_get_last_extra_data()
        look: dict[str, Any] = dict(last.attributes) if last.state == STATE_ON else {}
        if extra is not None:
            look = extra.as_dict() | {k: v for k, v in look.items() if v is not None}
        brightness = look.get(ATTR_BRIGHTNESS)
        if isinstance(brightness, int) and 1 <= brightness <= 255:
            self._attr_brightness = brightness
        self._attr_rgb_color = _rgb(look.get(ATTR_RGB_COLOR)) or self._attr_rgb_color
        effect = look.get(ATTR_EFFECT)
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

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """The last look, kept while off so a bare turn_on after a restart brings it back."""
        return RestoredExtraData(
            {
                ATTR_BRIGHTNESS: self._attr_brightness,
                ATTR_RGB_COLOR: list(self._attr_rgb_color or DEFAULT_RGB),
                ATTR_EFFECT: self._attr_effect,
            }
        )

    async def async_will_remove_from_hass(self) -> None:
        """Stop this segment's runner; leave the pixels as they are."""
        async with self._lock:  # a turn_on in flight must not store a runner after this
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
            self._runtime.release_if_idle()
        self.async_write_ha_state()

    async def async_hand_back(self) -> None:
        """Stop this segment's runner and show it off, sending nothing: the controller's own effect takes over."""
        async with self._lock:
            await self._runtime.stop_runner(self._config.key)
            if not self._attr_is_on:
                return
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


def _to_brightness(intensity: int) -> int:
    return round(intensity * 255 / 100)


def _to_intensity(brightness: int) -> int:
    return max(1, round(brightness * 100 / 255))


class NDBScenesLight(LightEntity, RestoreEntity):
    """The NDB-Pro's built-in effects (its stored shows among them). The controller's UI is the truth; it is polled."""

    _attr_has_entity_name = True
    _attr_name = "Scenes"
    _attr_should_poll = True
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, entry: DDPConfigEntry, controller: NDBController, device: DeviceInfo) -> None:
        self._runtime: DDPRuntime = entry.runtime_data
        self._controller = controller
        self._host: str = entry.data[CONF_HOST]
        self._attr_unique_id = f"{entry.entry_id}_controller"
        self._attr_device_info = device
        self._attr_is_on = False
        self._last_effect: str | None = None  # last effect other than "Off", seen or commanded

    async def async_added_to_hass(self) -> None:
        """Remember the last scene for a bare turn_on. Never commands the controller: its own state is the truth."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        last = extra.as_dict().get(ATTR_LAST_EFFECT) if extra is not None else None
        if self._last_effect is None and isinstance(last, str) and last != CONTROLLER_OFF:
            self._last_effect = last

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        return RestoredExtraData({ATTR_LAST_EFFECT: self._last_effect})

    async def async_update(self) -> None:
        try:
            state = await self._controller.state()
        except (ControllerError, DDPQueryError) as err:
            if self._attr_available:
                _LOGGER.warning("NDB-Pro at %s is unavailable: %s", self._host, err)
            self._attr_available = False
            return
        if not self._attr_available:
            _LOGGER.info("NDB-Pro at %s is available again", self._host)
        self._attr_available = True
        self._apply(state)

    def _apply(self, state: NDBState) -> None:
        self._attr_effect_list = [effect for effect in state.effects if effect != CONTROLLER_OFF]
        if state.intensity is not None:
            self._attr_brightness = _to_brightness(state.intensity)
        self._attr_is_on = state.effect is not None and state.effect != CONTROLLER_OFF
        self._attr_effect = state.effect if self._attr_is_on else None
        if self._attr_is_on:
            self._last_effect = state.effect

    async def async_turn_on(self, **kwargs: Any) -> None:
        effects = self.effect_list or []
        effect = kwargs.get(ATTR_EFFECT) or self._last_effect or (effects[0] if effects else None)
        if effect not in effects:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_effect",
                translation_placeholders={"effect": str(effect), "effects": ", ".join(effects)},
            )
        brightness: int | None = kwargs.get(ATTR_BRIGHTNESS)
        await self._hand_back()
        try:
            if brightness is not None:
                await self._controller.set_intensity(_to_intensity(brightness))
            await self._controller.set_effect(effect)
        except PyDDPError as err:
            raise self._controller_error(err) from err
        if brightness is not None:
            self._attr_brightness = _to_brightness(_to_intensity(brightness))
        self._attr_is_on, self._attr_effect, self._last_effect = True, effect, effect
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Fully dark: blank any DDP segment that is on, then select the controller's "Off"."""
        for light in self._runtime.segment_lights:
            if light.is_on:
                await light.async_turn_off()
        try:
            await self._controller.set_effect(CONTROLLER_OFF)
        except PyDDPError as err:
            raise self._controller_error(err) from err
        self._attr_is_on, self._attr_effect = False, None
        self.async_write_ha_state()

    async def async_recall_preset(self, preset: int) -> None:
        """Recall stored show ``preset`` (1-20), then read back what the controller now runs."""
        await self._hand_back()
        try:
            await self._controller.recall_preset(preset - 1)
        except PyDDPError as err:
            raise self._controller_error(err) from err
        await self.async_update()
        self.async_write_ha_state()

    async def _hand_back(self) -> None:
        """Turn every segment light off without sending, and stop keepalive, so DDP stops overriding the controller."""
        for light in self._runtime.segment_lights:
            await light.async_hand_back()
        self._runtime.sender.release()

    def _controller_error(self, err: PyDDPError) -> HomeAssistantError:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="controller_failed",
            translation_placeholders={"entity": self.entity_id, "error": str(err)},
        )
