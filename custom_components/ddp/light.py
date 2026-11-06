"""One light per segment (DDP), an "All outputs" light, and a "Scenes" light for a vendor controller's built-in effects.

Segment lights: on/off, brightness, RGB color (RGBW on an rgbw entry, white on the wire), aioddp effects, and
multi-color patterns (``ddp.set_pattern``), restored and re-sent on startup. Effects that render RGB only (the
animated ones) take the first three channels of an RGBW color and ignore W. An entry with more than one segment
also gets "All outputs", the same light over the whole strip, so a pattern or effect runs on across the outputs in
one DDP frame.

Source: each DDP light is lit by nothing (``off``), by HA over DDP (``ddp``), by the controller's running scene
(``scene``: nothing is sent), or is held black over a running scene (``blanked``: turned off while the scene runs).
It shows on for ``ddp`` and ``scene``. A bare turn_on of a blanked light releases its pixels back to the scene; any
turn_on with a brightness, color or effect takes it to ``ddp``.

Ownership: lights whose pixels overlap (All outputs and each output) take the pixels from each other. Turning one on
over DDP stops the overlapping lights' runners and shows the ``ddp`` and ``scene`` ones off without sending anything;
the pixels then show only the light that was turned on, where it covers them. Which pixels the sender keeps resending
follows the lights' state (``DDPRuntime.sync_holds``, after every change).

The controller light (the controller device's main entity, named like it) selects the controller's built-in effects
and stored presets; DDP data overrides those while it flows, so once the controller took the command it puts every
DDP light in ``scene`` (sending nothing) and releases the sender (``ControllerCommands``, shared with the Power switch
and the Scene select). A poll that finds a scene started or stopped outside HA does the same. It also sets the palette
those effects use (``ddp.set_colors``), which leaves DDP alone; "Color 1".."Color 5" set one palette slot each.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass, field
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

from . import DDPConfigEntry, DDPRuntime, SegmentConfig
from .commands import ControllerCommands, to_brightness
from .const import (
    ATTR_BACKGROUND,
    ATTR_BLOCK_SIZE,
    ATTR_COLORS,
    ATTR_PARAMS,
    COLOR_COOL_WHITE,
    COLOR_WARM_WHITE,
    CONF_DATA_TYPE,
    CONTROLLER_OFF,
    DATA_TYPE_RGBW,
    DOMAIN,
    PATTERN_EFFECT,
    RGB,
    RGBW,
    SOLID_EFFECT,
    STRIP_KEY,
    Color,
    LightSource,
)
from .controller import EMPTY_COLOR, NAMED_COLORS, PALETTE_SLOTS, WARM_WHITE_SHOWN
from .coordinator import SceneState
from .entity import ControllerEntity, DDPEntity, controller_device_parent, device_identifiers

_LOGGER = logging.getLogger(__name__)

# No cap: segment sends are independent local UDP writes, and controller commands serialize on the coordinator lock.
PARALLEL_UPDATES = 0

DEFAULT_BRIGHTNESS = 255
DEFAULT_RGB: RGB = (255, 255, 255)
DEFAULT_RGBW: RGBW = (0, 0, 0, 255)  # white on an RGBW strip is its white LEDs alone

EFFECTS: dict[str, EffectInfo] = {info.name: info for info in list_effects() if info.name != SOLID_EFFECT}

ATTR_LAST_EFFECT = "last_effect"
ATTR_PATTERN = "pattern"
ATTR_SOURCE = "source"  # restore data: a light that was on showing a scene is not re-sent
ALL_SOURCES = tuple(LightSource)
SCENES_KEY = "controller"  # unique-ID suffix; unchanged so entities from before controller types survive
COLOR_KEY = "color"  # unique-ID suffix of a palette slot light, with its slot: color_1 .. color_5
ATTR_LAST_COLOR = "last_color"  # restore data: a palette slot's last color, for a bare turn_on of an empty slot
# The palette lights' effects: the controller's named whites, sent as their exact values.
PALETTE_EFFECTS: dict[str, RGB] = {
    "Cool White": NAMED_COLORS[COLOR_COOL_WHITE],
    "Warm White": NAMED_COLORS[COLOR_WARM_WHITE],
}
# What a slot reads back as for each named white (Warm White shows as #c69740 once the controller set it).
PALETTE_READBACK: dict[RGB, str] = {
    **{color: name for name, color in PALETTE_EFFECTS.items()},
    WARM_WHITE_SHOWN: "Warm White",
}
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
    """One light per configured segment, All outputs when there are several, and for a vendor controller its light
    and the five palette slot lights."""
    runtime = entry.runtime_data
    lights = [DDPSegmentLight(entry, segment) for segment in runtime.segments]
    if len(runtime.segments) > 1:
        whole = Segment(STRIP_KEY, 0, runtime.sender.strip.pixel_count)
        lights.append(AllOutputsLight(entry, SegmentConfig(STRIP_KEY, None, whole)))
    runtime.segment_lights = lights
    # Drop lights this entry no longer has: an output the re-read layout lost, All outputs once one output is left,
    # and the controller's lights after a switch to Generic DDP.
    current = {light.unique_id for light in lights}
    if runtime.commands is not None:
        keys = [SCENES_KEY, *(f"{COLOR_KEY}_{slot}" for slot in range(1, PALETTE_SLOTS + 1))]
        current |= {f"{entry.entry_id}_{key}" for key in keys}
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.domain == LIGHT_DOMAIN and registered.unique_id not in current:
            registry.async_remove(registered.entity_id)
    async_add_entities(lights)
    if runtime.commands is None:
        # Switched to Generic DDP: drop the controller device the previous type registered, with its entities.
        devices = dr.async_get(hass)
        _, controller = device_identifiers(entry.data[CONF_HOST])
        if stale_device := devices.async_get_device_by_identifier(controller, entry.entry_id):
            devices.async_remove_device(stale_device.id)
        return
    parent = controller_device_parent(hass, entry)
    commands = runtime.commands
    async_add_entities(
        [
            ScenesLight(entry, commands, parent),
            *(PaletteLight(entry, commands, parent, slot) for slot in range(1, PALETTE_SLOTS + 1)),
        ]
    )


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
    """A segment of the strip. DDP is send-only, so HA's state (and the controller's polled scene) is the truth.

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
        self._source = LightSource.OFF
        self._attr_brightness = DEFAULT_BRIGHTNESS
        self._attr_effect = EFFECT_OFF
        self._pattern: Pattern | None = None  # set by set_pattern; replaced by a color or an effect change
        self._runner_effect: str | None = None  # effect of the runner this entity last started

    @property
    def source(self) -> LightSource:
        return self._source

    @property
    def is_on(self) -> bool:
        return self.source in (LightSource.DDP, LightSource.SCENE)

    @property
    def key(self) -> str:
        return self._config.key

    @property
    def segment(self) -> Segment:
        return self._config.segment

    @property
    def holding(self) -> bool:
        """Whether these pixels must stay held (``sync_holds`` holds them): DDP drives them, black is held over a
        running scene, or, with no scene running, another light drives DDP (an off light then stays black)."""
        if self._source in (LightSource.DDP, LightSource.BLANKED):
            return True
        runtime = self._runtime
        return (
            self._source is LightSource.OFF
            and not runtime.scene_running
            and any(light.source is LightSource.DDP for light in runtime.segment_lights)
        )

    @property
    def idle(self) -> bool:
        """Off, with no overlapping light driving these pixels over DDP: a running scene shows on them."""
        return self._source is LightSource.OFF and all(
            light.source is not LightSource.DDP for light in self._overlapping()
        )

    def _overlapping(self) -> list[DDPSegmentLight]:
        return [
            light
            for light in self._runtime.segment_lights
            if light is not self and _overlaps(light.segment, self.segment)
        ]

    def _covered(self) -> list[DDPSegmentLight]:
        """The other lights whose pixels all lie in this light's (the outputs, for All outputs)."""
        segment = self.segment
        return [
            light
            for light in self._overlapping()
            if segment.start <= light.segment.start and light.segment.end <= segment.end
        ]

    def _normalize(self) -> None:
        """Store the derived source (All outputs derives its own from its outputs)."""

    def _write_states(self) -> None:
        """Write this light's state and that of the lights sharing its pixels (All outputs derives its own)."""
        self.async_write_ha_state()
        for light in self._overlapping():
            light._normalize()
            light.async_write_ha_state()

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
        """Restore the last state and, if HA drove it, send it again (DDP controllers forget on power loss).

        A light that was blanked is not restored, and one that showed a scene is not re-sent: if the controller runs
        a scene now, the light shows it (when no overlapping light took the pixels over DDP). Sends nothing else.
        """
        await super().async_added_to_hass()
        await self._restore()
        scenes = self._runtime.scenes
        running = scenes is not None and scenes.data is not None and scenes.data.effect not in (None, CONTROLLER_OFF)
        if running and self.idle:
            self._source = LightSource.SCENE

    async def _restore(self) -> None:
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
        if last.state != STATE_ON or look.get(ATTR_SOURCE, LightSource.DDP) != LightSource.DDP:
            return
        try:
            async with self._lock:
                await self._send_on()
        except HomeAssistantError as err:
            _LOGGER.warning("Could not restore %s: %s", self.entity_id, err)
            return
        self._source = LightSource.DDP

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
                ATTR_SOURCE: self.source.value,
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
        if not kwargs.keys() & {ATTR_BRIGHTNESS, ATTR_EFFECT, self._color_key} and self.source in (
            LightSource.SCENE,
            LightSource.BLANKED,
        ):
            await self._show_scene()
            return
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
                self._source = LightSource.DDP
            await self._sync_holds()
        self._write_states()

    async def _show_scene(self) -> None:
        """Un-blank: hand this light's pixels (and those of the blanked lights it covers) back to the running scene.

        Sends nothing; the controller shows the scene there again after its DDP timeout (about 20 s on the NDB-Pro).
        """
        async with self._runtime.ownership:
            async with self._lock:
                if self._source is LightSource.BLANKED:
                    self._source = LightSource.SCENE
            for light in self._covered():
                await light.async_follow_scene(LightSource.SCENE, (LightSource.BLANKED,))
            await self._sync_holds()
        self._write_states()

    async def _sync_holds(self) -> None:
        """``DDPRuntime.sync_holds`` for a change this light made; a failed black frame is this light's error."""
        try:
            await self._runtime.sync_holds()
        except DDPError as err:
            raise self._send_error(err) from err

    async def _take_pixels(self) -> None:
        """Hand back every other ``ddp`` or ``scene`` light that shares pixels with this one (the ownership rule)."""
        for light in self._runtime.segment_lights:
            if light is not self and _overlaps(light._config.segment, self._config.segment):
                await light.async_hand_back()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Blank this light's pixels if it is on; an off light sends nothing (an overlapping light may own them).

        While a scene runs the black is held: the light (and every light it covers) is ``blanked``. Otherwise the
        light is off, and its black stays held only while another light drives DDP.

        Takes the locks in turn_on's order (ownership, then this light's own), so a concurrent turn_on of an
        overlapping light either finishes first (this light was handed back, so it is off) or runs after.
        """
        runtime = self._runtime
        async with runtime.ownership:
            async with self._lock:
                await runtime.stop_runner(self._config.key)
                if not self.is_on:
                    return
                blank = runtime.scene_running
                try:
                    await runtime.sender.off(self._config.segment)
                except DDPError as err:
                    raise self._send_error(err) from err
                runtime.held[self.key] = self.segment
                self._source = LightSource.BLANKED if blank else LightSource.OFF
            if blank:
                for light in self._covered():
                    await light.async_follow_scene(LightSource.BLANKED, ALL_SOURCES)
            await self._sync_holds()
        self._write_states()

    async def async_controller_off(self) -> None:
        """The controller is going "Off": blank this light if DDP drives it, else just show it off.

        Releases nothing; the controller command syncs the held pixels once every DDP light is off.
        """
        runtime = self._runtime
        async with runtime.ownership, self._lock:
            await runtime.stop_runner(self._config.key)
            if self._source is LightSource.DDP:
                try:
                    await runtime.sender.off(self._config.segment)
                except DDPError as err:
                    raise self._send_error(err) from err
                runtime.held[self.key] = self.segment
            self._source = LightSource.OFF
        self.async_write_ha_state()

    async def async_hand_back(self) -> None:
        """Stop this light's runner and, if DDP drove it or it showed the scene, show it off, sending nothing: another
        light takes over. A blanked light keeps its black."""
        async with self._lock:
            await self._runtime.stop_runner(self._config.key)
            if self.source not in (LightSource.DDP, LightSource.SCENE):
                return
            self._source = LightSource.OFF
        self.async_write_ha_state()

    async def async_follow_scene(self, source: LightSource, sources: Container[LightSource]) -> bool:
        """Show ``source`` if this light's is one of ``sources``, stopping any runner and sending nothing.

        For the controller's scene starting or stopping; returns whether the light changed.
        """
        async with self._lock:
            if self._source not in sources:
                return False
            await self._runtime.stop_runner(self._config.key)
            self._source = source
        self._write_states()
        return True

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
            else:
                effect_color = color if info.rgbw else (color[0], color[1], color[2])
                await self._run_effect(info, _effect_params(info, [effect_color], self._pattern), brightness)
        except DDPError as err:
            raise self._send_error(err) from err
        runtime.held[key] = segment  # the frames hold it

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

    @property
    def source(self) -> LightSource:
        """Blanked only while an output it covers still is, so un-blanking the outputs one by one brings it back."""
        if self._source is LightSource.BLANKED and all(
            light.source is not LightSource.BLANKED for light in self._covered()
        ):
            return LightSource.SCENE
        return self._source

    @property
    def holding(self) -> bool:
        # Blanked, its outputs hold their own black: holding the whole strip would keep an un-blanked output black.
        return self._source is not LightSource.BLANKED and super().holding

    def _normalize(self) -> None:
        self._source = self.source


class ScenesLight(ControllerEntity, LightEntity, RestoreEntity):
    """A vendor controller's built-in effects (stored shows included) and their palette: the controller device's main
    entity, named like the device.

    The controller is the truth; it is polled. The palette shows as the ``colors`` and ``background`` attributes.
    """

    _attr_name = None  # the device's main entity; the translation key gives the icon and attribute names
    _attr_translation_key = "controller"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, entry: DDPConfigEntry, commands: ControllerCommands, ddp_device_id: str) -> None:
        super().__init__(entry, commands, SCENES_KEY, ddp_device_id)
        self._runtime: DDPRuntime = entry.runtime_data
        self._attr_is_on = False
        if self.scene is not None:
            self._apply(self.scene)

    async def async_added_to_hass(self) -> None:
        """Remember the last scene for a bare turn_on. Never commands the controller: its own state is the truth."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        last = extra.as_dict().get(ATTR_LAST_EFFECT) if extra is not None else None
        if self.coordinator.last_effect is None and isinstance(last, str) and last != CONTROLLER_OFF:
            self.coordinator.last_effect = last
        self.coordinator.on_poll = self._follow_poll
        self.async_on_remove(self._stop_following)

    @callback
    def _stop_following(self) -> None:
        self.coordinator.on_poll = None

    async def _follow_poll(self, previous: SceneState | None, state: SceneState) -> None:
        """Show on the DDP lights a scene started or stopped outside HA (the controller's own UI or schedule).

        A new scene takes every ``off`` and ``ddp`` light (blanked ones keep their black). Any poll of a running scene
        also shows it on every ``off`` light that holds no pixels (released, so the controller lights them again);
        ``ddp`` lights are then left alone, since DDP may be overriding that scene. "Off" turns the lights showing or
        blanking a scene off and leaves ``ddp`` lights running.
        """
        if state.effect is None:
            return
        before = previous.effect if previous is not None else None
        runtime = self._runtime
        lights = runtime.segment_lights
        sources: tuple[LightSource, ...] = (LightSource.OFF, LightSource.DDP)
        target = LightSource.SCENE
        if state.effect == CONTROLLER_OFF:
            sources, target = (LightSource.SCENE, LightSource.BLANKED), LightSource.OFF
        elif before is None or before == state.effect:
            # Decided before any light changes: one light showing the scene would make the others stop holding.
            lights = [light for light in lights if light.source is LightSource.OFF and not light.holding]
        async with runtime.ownership:
            for light in lights:
                await light.async_follow_scene(target, sources)
            try:
                await runtime.sync_holds()
            except DDPError as err:  # the poll itself succeeded; the next change syncs again
                _LOGGER.warning("Could not send black to the off DDP lights of %s: %s", self.entity_id, err)

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        return RestoredExtraData({ATTR_LAST_EFFECT: self.coordinator.last_effect})

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.scene is not None:
            self._apply(self.scene)
        super()._handle_coordinator_update()

    def _apply(self, state: SceneState) -> None:
        self._attr_effect_list = self._commands.effects
        if state.intensity is not None:
            self._attr_brightness = to_brightness(state.intensity)
        self._attr_is_on = self.scene_on
        self._attr_effect = state.effect if self._attr_is_on else None
        if state.background is not None:
            self._attr_extra_state_attributes = {
                ATTR_COLORS: [list(color) for color in state.colors],
                ATTR_BACKGROUND: list(state.background),
            }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._commands.turn_on(self.entity_id, kwargs.get(ATTR_EFFECT), kwargs.get(ATTR_BRIGHTNESS))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._commands.turn_off(self.entity_id)

    async def async_recall_preset(self, preset: int) -> None:
        await self._commands.recall_preset(self.entity_id, preset)

    async def async_set_colors(self, colors: Sequence[RGB], background: RGB | None = None) -> None:
        """The palette is the controller's own setting, so DDP keeps the pixels and the light need not be on."""
        await self._commands.set_palette(self.entity_id, colors, background)


def _scaled(color: RGB, brightness: int | None) -> RGB:
    """``color`` at ``brightness`` (255 or ``None``: unchanged)."""
    if brightness is None:
        return color
    return (round(color[0] * brightness / 255), round(color[1] * brightness / 255), round(color[2] * brightness / 255))


class PaletteLight(ControllerEntity, LightEntity, RestoreEntity):
    """One slot of the controller's palette ("Color 1".."Color 5"): off is an empty slot, the effects are the
    controller's named whites (shown as the effect, not as a color, when the slot reads back as one)."""

    _attr_translation_key = "color"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = list(PALETTE_EFFECTS)
    _attr_brightness = DEFAULT_BRIGHTNESS  # the slot is a color; a turn_on brightness scales the color sent

    def __init__(self, entry: DDPConfigEntry, commands: ControllerCommands, ddp_device_id: str, slot: int) -> None:
        super().__init__(entry, commands, f"{COLOR_KEY}_{slot}", ddp_device_id)
        self._slot = slot
        self._attr_translation_placeholders = {"slot": str(slot)}
        self._attr_is_on = False
        self._last_color: RGB = DEFAULT_RGB  # last color other than empty, for a bare turn_on of an empty slot
        if self.scene is not None:
            self._apply(self.scene)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        stored = _color(extra.as_dict().get(ATTR_LAST_COLOR)) if extra is not None else None
        if not self._attr_is_on and stored is not None and len(stored) == 3 and stored != EMPTY_COLOR:
            self._last_color = (stored[0], stored[1], stored[2])

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        return RestoredExtraData({ATTR_LAST_COLOR: list(self._last_color)})

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.scene is not None:
            self._apply(self.scene)
        super()._handle_coordinator_update()

    def _apply(self, state: SceneState) -> None:
        if len(state.colors) < self._slot:  # the palette is unknown until it is read
            return
        color = state.colors[self._slot - 1]
        self._attr_is_on = color != EMPTY_COLOR
        self._attr_rgb_color = color if self._attr_is_on else None
        self._attr_effect = PALETTE_READBACK.get(color) if self._attr_is_on else None
        if self._attr_is_on:
            self._last_color = PALETTE_EFFECTS[self._attr_effect] if self._attr_effect is not None else color

    async def async_turn_on(self, **kwargs: Any) -> None:
        """A color (scaled by a brightness), a named white, or (bare) the slot's last color, default white."""
        effect: str | None = kwargs.get(ATTR_EFFECT)
        if effect is not None:
            if effect not in PALETTE_EFFECTS:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_effect",
                    translation_placeholders={"effect": effect, "effects": ", ".join(PALETTE_EFFECTS)},
                )
            color = PALETTE_EFFECTS[effect]  # the exact value the controller maps to its white LEDs
        else:
            rgb = kwargs.get(ATTR_RGB_COLOR)
            base: RGB = (rgb[0], rgb[1], rgb[2]) if rgb is not None else self._last_color
            color = _scaled(base, kwargs.get(ATTR_BRIGHTNESS))
        await self._commands.set_color(self.entity_id, self._slot, color)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._commands.set_color(self.entity_id, self._slot, EMPTY_COLOR)
