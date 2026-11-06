"""One light per segment (DDP), an "All outputs" light, and a "Scenes" light for a vendor controller's built-in effects.

Segment lights: on/off, brightness, RGB color (RGBW on an rgbw entry, white on the wire), aioddp effects, and
multi-color patterns (``ddp.set_pattern``), restored and re-sent on startup. Effects that render RGB only (the
animated ones) take the first three channels of an RGBW color and ignore W. An entry with more than one segment
also gets "All outputs", the same light over the whole strip, so a pattern or effect runs on across the outputs in
one DDP frame.

Ownership: lights whose pixels overlap (All outputs and each output) take the pixels from each other. Turning one on
stops the overlapping lights' runners and shows them off without sending anything, exactly like a hand-back to the
controller; the pixels then show only the light that was turned on, where it covers them.

The Scenes light (on the controller device) selects the controller's built-in effects and stored presets; DDP data
overrides those while it flows, so it turns every DDP light off (without blanking) and releases the sender before
commanding the controller. It also sets the palette those effects use (``ddp.set_colors``), which leaves DDP alone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from aioddp import DDPError, EffectInfo, EffectRunner, Segment, get_effect, list_effects, validate_params
from homeassistant.components.light import LightEntity
from homeassistant.components.light.const import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    EFFECT_OFF,
    ColorMode,
    LightEntityFeature,
)
from homeassistant.components.light.const import DOMAIN as LIGHT_DOMAIN
from homeassistant.const import CONF_HOST, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoredExtraData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DDPConfigEntry, DDPRuntime, SegmentConfig
from .const import (
    ATTR_BACKGROUND,
    ATTR_BLOCK_SIZE,
    ATTR_COLORS,
    ATTR_PARAMS,
    CONF_DATA_TYPE,
    DATA_TYPE_RGBW,
    DOMAIN,
    PATTERN_EFFECT,
    RGB,
    RGBW,
    SOLID_EFFECT,
    STRIP_KEY,
    Color,
)
from .controller import CONTROLLER_ERRORS, EMPTY_COLOR, PALETTE_SLOTS
from .coordinator import DDPScenesCoordinator, SceneState
from .entity import DDPEntity, ddp_device_info, device_identifiers

_LOGGER = logging.getLogger(__name__)

# No cap: segment sends are independent local UDP writes, and the Scenes light serializes its own commands and polls.
PARALLEL_UPDATES = 0

DEFAULT_BRIGHTNESS = 255
DEFAULT_RGB: RGB = (255, 255, 255)
DEFAULT_RGBW: RGBW = (0, 0, 0, 255)  # white on an RGBW strip is its white LEDs alone

EFFECTS: dict[str, EffectInfo] = {info.name: info for info in list_effects() if info.name != SOLID_EFFECT}

CONTROLLER_OFF = "Off"  # the controller's built-in effect that blanks the lights; the Scenes light's off, not an effect
ATTR_LAST_EFFECT = "last_effect"
ATTR_PATTERN = "pattern"
SCENES_KEY = "controller"  # unique-ID suffix; unchanged so entities from before controller types survive
COLORS_KIND = "colors"  # aioddp ParamSpec kind of a color-list param


def _color_target(info: EffectInfo) -> str | None:
    """The param the light's colors drive: the effect's color param, else its color-list param (palette)."""
    if info.color_param is not None:
        return info.color_param
    return next((name for name, spec in info.params.items() if spec.kind == COLORS_KIND), None)


PATTERN_EFFECTS = [name for name, info in EFFECTS.items() if _color_target(info) is not None]
RGBW_PATTERN_EFFECTS = [name for name in PATTERN_EFFECTS if EFFECTS[name].rgbw]


def _has_white(colors: Sequence[Color]) -> bool:
    return any(len(color) == 4 for color in colors)


@dataclass(frozen=True, slots=True)
class Pattern:
    """A ``set_pattern`` look: colors (RGB, or RGBW where the entry and effect allow), pixels per color block
    (``None``: the effect's default), and extra params."""

    colors: tuple[Color, ...]
    block_size: int | None = None
    params: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            ATTR_COLORS: [list(color) for color in self.colors],
            ATTR_BLOCK_SIZE: self.block_size,
            ATTR_PARAMS: dict(self.params),
        }

    @classmethod
    def from_dict(cls, value: object) -> Pattern | None:
        """The pattern stored by ``as_dict``, or ``None`` if ``value`` is not one."""
        stored: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
        colors, block_size, params = stored.get(ATTR_COLORS), stored.get(ATTR_BLOCK_SIZE), stored.get(ATTR_PARAMS) or {}
        parsed = [_color(color) for color in colors] if isinstance(colors, list) else []
        rgbs = [color for color in parsed if color is not None]
        block_ok = block_size is None or (isinstance(block_size, int) and not isinstance(block_size, bool))
        if not rgbs or len(rgbs) != len(parsed) or not isinstance(params, Mapping) or not block_ok:
            return None
        return cls(tuple(rgbs), block_size, dict(params))


def _effect_params(info: EffectInfo, colors: Sequence[Color], pattern: Pattern | None = None) -> dict[str, Any]:
    """Params for ``info``: the pattern's extra params and block size, with its colors (or ``colors``) on the
    effect's color target; an effect that takes one color gets the first."""
    params: dict[str, Any] = dict(pattern.params) if pattern is not None else {}
    if pattern is not None:
        colors = pattern.colors
        if pattern.block_size is not None:
            params[ATTR_BLOCK_SIZE] = pattern.block_size
    if (target := _color_target(info)) is not None:
        params[target] = list(colors) if info.params[target].kind == COLORS_KIND else colors[0]
    return params


async def async_setup_entry(
    hass: HomeAssistant, entry: DDPConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """One light per configured segment, All outputs when there are several, and the Scenes light for a vendor."""
    runtime = entry.runtime_data
    lights = [DDPSegmentLight(entry, segment) for segment in runtime.segments]
    if len(runtime.segments) > 1:
        whole = Segment(STRIP_KEY, 0, runtime.sender.strip.pixel_count)
        lights.append(AllOutputsLight(entry, SegmentConfig(STRIP_KEY, None, whole)))
    runtime.segment_lights = lights
    # Drop lights this entry no longer has: an output the re-read layout lost, All outputs once one output is left,
    # and the Scenes light after a switch to Generic DDP.
    current = {light.unique_id for light in lights}
    if runtime.scenes is not None:
        current.add(f"{entry.entry_id}_{SCENES_KEY}")
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.domain == LIGHT_DOMAIN and registered.unique_id not in current:
            registry.async_remove(registered.entity_id)
    async_add_entities(lights)
    if runtime.scenes is None:
        # Switched to Generic DDP: drop the controller device the previous type registered.
        devices = dr.async_get(hass)
        _, controller = device_identifiers(entry.data[CONF_HOST])
        if stale_device := devices.async_get_device_by_identifier(controller, entry.entry_id):
            devices.async_remove_device(stale_device.id)
        return
    # The controller device is connected through the DDP device, so that one has to exist first.
    ddp_device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, **ddp_device_info(entry))
    async_add_entities([ScenesLight(entry, runtime.scenes, ddp_device.id)])


def _color(value: object) -> Color | None:
    """``value`` as an RGB or RGBW color, or ``None`` if it is neither."""
    if (
        isinstance(value, list | tuple)
        and len(value) in (3, 4)
        and all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in value)
    ):
        return _as_color(value)
    return None


def _as_color(value: Sequence[int]) -> Color:
    """A validated 3- or 4-component sequence as a color tuple."""
    if len(value) == 4:
        return (value[0], value[1], value[2], value[3])
    return (value[0], value[1], value[2])


def _overlaps(a: Segment, b: Segment) -> bool:
    return a.start < b.end and b.start < a.end


class DDPSegmentLight(DDPEntity, LightEntity, RestoreEntity):
    """A segment of the strip. DDP is send-only, so HA's state is the truth.

    The color mode follows the entry's data type: RGB, or RGBW (``rgbw_color``) for 4-channel pixels.
    """

    _attr_should_poll = False
    _attr_assumed_state = True
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = [EFFECT_OFF, *EFFECTS]

    def __init__(self, entry: DDPConfigEntry, config: SegmentConfig) -> None:
        super().__init__(entry, config.key)
        self._runtime: DDPRuntime = entry.runtime_data
        self._config = config
        self._lock = asyncio.Lock()  # one state change at a time, so runners never double-start
        if config.name is not None:
            self._attr_name = config.name  # the layout's label for this output, stored with the entry
        self._rgbw = entry.data[CONF_DATA_TYPE] == DATA_TYPE_RGBW
        mode = ColorMode.RGBW if self._rgbw else ColorMode.RGB
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}
        self._color_key = ATTR_RGBW_COLOR if self._rgbw else ATTR_RGB_COLOR  # turn_on kwarg and restore key
        self._color: Color = DEFAULT_RGBW if self._rgbw else DEFAULT_RGB
        self._attr_is_on = False
        self._attr_brightness = DEFAULT_BRIGHTNESS
        self._attr_effect = EFFECT_OFF
        self._pattern: Pattern | None = None  # set by set_pattern; replaced by a color or an effect change
        self._runner_effect: str | None = None  # effect of the runner this entity last started

    @property
    def rgb_color(self) -> RGB | None:
        color = self._color
        return (color[0], color[1], color[2]) if len(color) == 3 else None

    @property
    def rgbw_color(self) -> RGBW | None:
        color = self._color
        return (color[0], color[1], color[2], color[3]) if len(color) == 4 else None

    def _fit(self, color: Color) -> Color:
        """``color`` as this light's own color: an RGB color gets W=0 on an RGBW light."""
        if self._rgbw and len(color) == 3:
            return (color[0], color[1], color[2], 0)
        return color

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
        color = _color(look.get(self._color_key))
        if color is not None and len(color) == len(self._color):
            self._color = color
        effect = look.get(ATTR_EFFECT)
        self._attr_effect = effect if effect in EFFECTS else EFFECT_OFF
        self._pattern = self._restored_pattern(look.get(ATTR_PATTERN))
        if last.state != STATE_ON:
            return
        try:
            async with self._lock:
                await self._send_on()
        except HomeAssistantError as err:
            _LOGGER.warning("Could not restore %s: %s", self.entity_id, err)
            return
        self._attr_is_on = True

    def _restored_pattern(self, value: object) -> Pattern | None:
        """The stored pattern, if it still validates for the restored effect (aioddp may have changed)."""
        info = EFFECTS.get(self._attr_effect or EFFECT_OFF)
        pattern = Pattern.from_dict(value)
        if info is None or pattern is None or _color_target(info) is None:
            return None
        if not self._rgbw and _has_white(pattern.colors):  # stored while the entry was RGBW
            _LOGGER.warning("Dropping the stored pattern of %s: RGBW colors on RGB pixels", self.entity_id)
            return None
        try:
            validate_params(get_effect(info.name), _effect_params(info, pattern.colors, pattern))
        except DDPError as err:
            _LOGGER.warning("Dropping the stored pattern of %s: %s", self.entity_id, err)
            return None
        return pattern

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """The last look, kept while off so a bare turn_on after a restart brings it back."""
        return RestoredExtraData(
            {
                ATTR_BRIGHTNESS: self._attr_brightness,
                self._color_key: list(self._color),
                ATTR_EFFECT: self._attr_effect,
                ATTR_PATTERN: self._pattern.as_dict() if self._pattern is not None else None,
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
        # A color or another effect replaces the pattern; a bare turn_on (or brightness) keeps it.
        keep = self._color_key not in kwargs and effect == self._attr_effect
        await self._apply(
            kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness),
            _as_color(kwargs[self._color_key]) if self._color_key in kwargs else self._color,
            effect,
            self._pattern if keep else None,
        )

    async def async_set_pattern(
        self,
        colors: Sequence[Sequence[int]],
        effect: str = PATTERN_EFFECT,
        block_size: int | None = None,
        params: Mapping[str, Any] | None = None,
        brightness: int | None = None,
    ) -> None:
        """The ``set_pattern`` action: ``colors`` in blocks of ``block_size`` pixels, rendered by ``effect``.

        RGBW colors need an RGBW entry and an effect that takes white; refused here, before aioddp would.
        """
        info = EFFECTS.get(effect)
        if info is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_effect",
                translation_placeholders={"effect": effect, "effects": ", ".join(PATTERN_EFFECTS)},
            )
        if _color_target(info) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="effect_without_colors",
                translation_placeholders={"effect": effect, "effects": ", ".join(PATTERN_EFFECTS)},
            )
        pattern = Pattern(tuple(_as_color(c) for c in colors), block_size, dict(params or {}))
        if _has_white(pattern.colors) and not self._rgbw:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="rgbw_needs_rgbw_pixels",
                translation_placeholders={"entity": self.entity_id},
            )
        if _has_white(pattern.colors) and not info.rgbw:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="rgbw_effect_unsupported",
                translation_placeholders={"effect": effect, "effects": ", ".join(RGBW_PATTERN_EFFECTS)},
            )
        try:
            validate_params(get_effect(effect), _effect_params(info, pattern.colors, pattern))
        except DDPError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_effect_params",
                translation_placeholders={"effect": effect, "error": str(err)},
            ) from err
        await self._apply(brightness or self._attr_brightness, pattern.colors[0], effect, pattern)

    async def _apply(self, brightness: int | None, color: Color, effect: str, pattern: Pattern | None) -> None:
        """Take the pixels from overlapping lights, then show this look; a failed send keeps the previous one."""
        async with self._runtime.ownership:  # two lights sharing pixels never take them at the same time
            await self._take_pixels()
            async with self._lock:
                previous = (self._attr_brightness, self._color, self._attr_effect, self._pattern)
                self._attr_brightness, self._color, self._attr_effect = brightness, self._fit(color), effect
                self._pattern = pattern
                try:
                    await self._send_on()
                except HomeAssistantError:
                    self._attr_brightness, self._color, self._attr_effect, self._pattern = previous
                    raise
                self._attr_is_on = True
        self.async_write_ha_state()

    async def _take_pixels(self) -> None:
        """Hand back every other DDP light that shares pixels with this one (the ownership rule)."""
        for light in self._runtime.segment_lights:
            if light is not self and _overlaps(light._config.segment, self._config.segment):
                await light.async_hand_back()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Blank this light's pixels if it is on; an off light sends nothing (an overlapping light may own them).

        Takes the locks in turn_on's order (ownership, then this light's own), so a concurrent turn_on of an
        overlapping light either finishes first (this light was handed back, so it is off) or runs after.
        """
        async with self._runtime.ownership, self._lock:
            await self._runtime.stop_runner(self._config.key)
            if self._attr_is_on:
                try:
                    await self._runtime.sender.off(self._config.segment)
                except DDPError as err:
                    raise self._send_error(err) from err
                self._attr_is_on = False
            self._runtime.release_if_idle()
        self.async_write_ha_state()

    async def async_hand_back(self) -> None:
        """Stop this light's runner and show it off, sending nothing: another light or the controller takes over."""
        async with self._lock:
            await self._runtime.stop_runner(self._config.key)
            if not self._attr_is_on:
                return
            self._attr_is_on = False
        self.async_write_ha_state()

    async def _send_on(self) -> None:
        """Send the current brightness, color or pattern, and effect. Caller holds the lock.

        An effect that renders RGB only gets the first three channels of an RGBW color (it ignores W).
        """
        runtime, key, segment = self._runtime, self._config.key, self._config.segment
        brightness = (self._attr_brightness or DEFAULT_BRIGHTNESS) / 255
        color = self._color
        info = EFFECTS.get(self._attr_effect or EFFECT_OFF)
        try:
            if info is None:
                await runtime.stop_runner(key)
                await runtime.sender.fill(segment, color, brightness=brightness)
                return
            effect_color = color if info.rgbw else (color[0], color[1], color[2])
            await self._run_effect(info, _effect_params(info, [effect_color], self._pattern), brightness)
        except DDPError as err:
            raise self._send_error(err) from err

    async def _run_effect(self, info: EffectInfo, params: Mapping[str, Any], brightness: float) -> None:
        """Update this light's runner in place if it runs ``info``, else start one with ``params``."""
        runtime, key = self._runtime, self._config.key
        runner = runtime.runners.get(key)
        if runner is not None and runner.running and self._runner_effect == info.name:
            runner.update(params=params, brightness=brightness)
            return
        await runtime.stop_runner(key)
        runner = EffectRunner(
            runtime.sender, self._config.segment, info.name, fps=runtime.fps, params=params, brightness=brightness
        )
        await runner.start()
        runtime.runners[key] = runner
        self._runner_effect = info.name

    def _send_error(self, err: DDPError) -> HomeAssistantError:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="send_failed",
            translation_placeholders={"entity": self.entity_id, "error": str(err)},
        )


class AllOutputsLight(DDPSegmentLight):
    """The whole strip as one light (entries with several outputs), sharing its pixels with every output light."""

    _attr_translation_key = "all_outputs"


def _to_brightness(intensity: int) -> int:
    return round(intensity * 255 / 100)


def _to_intensity(brightness: int) -> int:
    return max(1, round(brightness * 100 / 255))


class ScenesLight(CoordinatorEntity[DDPScenesCoordinator], DDPEntity, LightEntity, RestoreEntity):
    """A vendor controller's built-in effects (stored shows included) and their palette, on the controller device.

    The controller is the truth; it is polled. The palette shows as the ``colors`` and ``background`` attributes.
    """

    _attr_translation_key = "scenes"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, entry: DDPConfigEntry, coordinator: DDPScenesCoordinator, ddp_device_id: str) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        DDPEntity.__init__(self, entry, SCENES_KEY, ddp_device_id=ddp_device_id)
        self._runtime: DDPRuntime = entry.runtime_data
        self._attr_is_on = False
        self._last_effect: str | None = None  # last effect other than "Off", seen or commanded
        if coordinator.data is not None:
            self._apply(coordinator.data)

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

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data is not None:
            self._apply(self.coordinator.data)
        super()._handle_coordinator_update()

    def _apply(self, state: SceneState) -> None:
        self._attr_effect_list = [effect for effect in state.effects if effect != CONTROLLER_OFF]
        if state.intensity is not None:
            self._attr_brightness = _to_brightness(state.intensity)
        self._attr_is_on = state.effect is not None and state.effect != CONTROLLER_OFF
        self._attr_effect = state.effect if self._attr_is_on else None
        if self._attr_is_on:
            self._last_effect = state.effect
        if state.background is not None:
            self._attr_extra_state_attributes = {
                ATTR_COLORS: [list(color) for color in state.colors],
                ATTR_BACKGROUND: list(state.background),
            }

    def _publish(self, **changes: Any) -> None:
        """Publish a commanded change as the coordinator's data, keeping everything the command did not change."""
        self.coordinator.publish(replace(self.coordinator.data or SceneState(None, None, ()), **changes))

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
        async with self.coordinator.lock:
            try:
                if brightness is not None:
                    await self.coordinator.controller.set_intensity(_to_intensity(brightness))
                await self.coordinator.controller.set_effect(effect)
            except CONTROLLER_ERRORS as err:
                raise self._controller_error(err) from err
            await self._hand_back()  # only once the controller took the command, so a failure keeps the DDP look
            if brightness is None:
                self._publish(effect=effect)
            else:
                self._publish(effect=effect, intensity=_to_intensity(brightness))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Fully dark: blank any DDP segment that is on, then select the controller's "Off"."""
        segment_error: HomeAssistantError | None = None
        async with self.coordinator.lock:
            for light in self._runtime.segment_lights:
                if light.is_on:
                    try:
                        await light.async_turn_off()
                    except HomeAssistantError as err:  # keep going: the controller's "Off" still darkens the rest
                        segment_error = segment_error or err
            try:
                await self.coordinator.controller.set_effect(CONTROLLER_OFF)
            except CONTROLLER_ERRORS as err:
                raise self._controller_error(err) from err
            self._publish(effect=CONTROLLER_OFF)
        if segment_error is not None:
            raise segment_error

    async def async_recall_preset(self, preset: int) -> None:
        """Recall stored show ``preset`` (1-20), then read back what the controller now runs."""
        async with self.coordinator.lock:
            try:
                await self.coordinator.controller.recall_preset(preset - 1)
            except CONTROLLER_ERRORS as err:
                raise self._controller_error(err) from err
            await self._hand_back()
            await self.coordinator.async_read_after_command()

    async def async_set_colors(self, colors: Sequence[RGB], background: RGB | None = None) -> None:
        """Make the palette exactly ``colors`` (the other slots empty; none: the controller picks random colors).

        The palette is the controller's own setting, so DDP keeps the pixels and the light need not be on.
        """
        async with self.coordinator.lock:
            try:
                await self.coordinator.controller.set_palette(colors, background=background)
            except CONTROLLER_ERRORS as err:
                raise self._controller_error(err) from err
            slots = (*colors, *[EMPTY_COLOR] * (PALETTE_SLOTS - len(colors)))
            if background is None:
                self._publish(colors=slots)
            else:
                self._publish(colors=slots, background=background)

    async def _hand_back(self) -> None:
        """Turn every DDP light off without sending, and stop keepalive, so DDP stops overriding the controller."""
        for light in self._runtime.segment_lights:
            await light.async_hand_back()
        # A segment turned on mid-way (an HA scene applies lights concurrently) keeps its keepalive.
        self._runtime.release_if_idle()

    def _controller_error(self, err: Exception) -> HomeAssistantError:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="controller_failed",
            translation_placeholders={"entity": self.entity_id, "error": str(err)},
        )
