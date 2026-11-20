"""DDP Pixel Controller: one DDP controller as a device with one light per segment. Protocol code lives in aioddp."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import probatio
from aioddp import DDP_PORT, DataType, DDPSender, DDPSendError, EffectRunner, Segment, Strip
from homeassistant.components.light.const import ATTR_BRIGHTNESS, ATTR_EFFECT
from homeassistant.components.light.const import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service
from homeassistant.helpers.typing import ConfigType

from .commands import ControllerCommands, scene_mode
from .const import (
    ATTR_BACKGROUND,
    ATTR_BLOCK_SIZE,
    ATTR_COLORS,
    ATTR_PARAMS,
    CONF_BRIGHTNESS_CAP,
    CONF_COLOR_ORDER,
    CONF_CONTROLLER_TYPE,
    CONF_DATA_TYPE,
    CONF_FPS,
    CONF_HTTP_PORT,
    CONF_KEEPALIVE,
    CONF_MODEL,
    CONF_PIXEL_COUNT,
    CONF_SEGMENTS,
    DATA_TYPE_RGBW,
    DEFAULT_BRIGHTNESS_CAP,
    DEFAULT_COLOR_ORDER,
    DEFAULT_FPS,
    DEFAULT_HTTP_PORT,
    DEFAULT_KEEPALIVE,
    DOMAIN,
    FRAME_REPEAT_INTERVAL,
    FRAME_REPEATS,
    MAX_PATTERN_COLORS,
    PRESET_COUNT,
    SEGMENT_KEY,
    SEGMENT_LENGTH,
    SEGMENT_NAME,
    SEGMENT_START,
    SERVICE_RECALL_PRESET,
    SERVICE_SET_COLORS,
    SERVICE_SET_PATTERN,
    LightSource,
)
from .controller import NAMED_COLORS, PALETTE_SLOTS, Controller, create_controller, type_for_model
from .coordinator import DDPScenesCoordinator

if TYPE_CHECKING:
    from .light import DDPSegmentLight

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SELECT]
CONFIG_VERSION = 2  # the config flow's VERSION
_HELD = "held"  # name of the pixel ranges ``sync_holds`` builds; aioddp segments need one

_BYTE = probatio.All(probatio.Coerce(int), probatio.Range(min=0, max=255))
_RGB = probatio.All(probatio.ExactSequence([_BYTE, _BYTE, _BYTE]), probatio.Coerce(tuple))
# A pattern color: [r, g, b] or, for an RGBW entry and an effect that takes white, [r, g, b, w].
_PATTERN_COLOR = probatio.Any(
    probatio.ExactSequence([_BYTE, _BYTE, _BYTE]), probatio.ExactSequence([_BYTE, _BYTE, _BYTE, _BYTE])
)
# A palette color: [r, g, b] or the name of one of the controller's whites.
_PALETTE_COLOR = probatio.Any(_RGB, probatio.All(probatio.In(NAMED_COLORS), NAMED_COLORS.__getitem__))


@dataclass(frozen=True, slots=True)
class SegmentConfig:
    """One light's slice of the strip: ``key`` is stable (unique IDs), ``name`` is for display (``None``: the
    entity's translated name)."""

    key: str
    name: str | None
    segment: Segment


@dataclass(slots=True)
class DDPRuntime:
    """Per-entry runtime: the one open sender, the segments, and at most one effect runner per segment key.

    ``controller`` drives a vendor controller's built-in effects (``None`` for Generic DDP), ``scenes`` polls it and
    ``commands`` (the controller device's entities share them) command it.
    ``segment_lights`` is every DDP light (the per-output lights, with several outputs All outputs, and with the
    pixel lights option one light per pixel), so a
    controller command can hand the pixels back from DDP. ``ownership`` serializes a DDP light taking pixels that
    another DDP light shares. The sender keeps resending (keepalive) only the pixels it holds (``DDPSender.held``), and
    ``sync_holds`` makes them follow the lights' state.
    """

    sender: DDPSender
    segments: list[SegmentConfig]
    fps: int
    controller: Controller | None = None
    scenes: DDPScenesCoordinator | None = None
    commands: ControllerCommands | None = None
    runners: dict[str, EffectRunner] = field(default_factory=dict)
    segment_lights: list[DDPSegmentLight] = field(default_factory=list)
    ownership: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Scene mode (True: the controller runs a scene, every DDP light is off), DDP mode (False), or unknown (None: the
    # controller has not been read yet). The DDP lights' model of the controller, set by setup, polls and controller
    # commands; every switch into scene mode happens under ``ownership`` (a scene command counts as running once the
    # controller took it, even if the read-back fails), so a DDP change that checks it under ``ownership`` never misses
    # a scene start.
    scene_mode: bool | None = None

    @property
    def scene_running(self) -> bool:
        """Scene mode: the controller runs a scene, as last read or commanded; every DDP light is off."""
        return self.scene_mode is True

    @property
    def ddp_driving(self) -> bool:
        """Some DDP light drives its pixels (DDP mode): the off DDP lights then hold black."""
        return any(light.source is LightSource.DDP for light in self.segment_lights)

    async def sync_holds(self) -> None:
        """Make the held pixels follow the lights' state (``holding``). Caller holds ``ownership``.

        With no DDP light on (Generic DDP all off, or a vendor controller in scene mode) nothing is held, so keepalive
        resends nothing and the controller's own effect or scene takes the pixels back after its DDP timeout. The
        release drains: a pending repeat burst of the last frame still goes out (a scene hand-back releases at once).
        While one is on, every off DDP light's pixels that no on light covers get black, so they are held black. What
        the sender holds does not count as covered: a light handed back to an overlapping one leaves its last frame
        in the held range, and that is blacked too. The off lights' pixels are merged into runs first, one black
        frame per run (pixel lights would otherwise send one each); a run the sender already holds black (its shadow
        bytes, ``stats.last_frame``) is skipped. A ``ddp`` light's pixels count as covered even before its runner's
        first frame, so they never get black. Raises ``DDPError``.
        """
        if not self.ddp_driving:
            self.sender.release(drain=True)  # the last frame (black) still goes out as a burst; keepalive stops now
            return
        lights = self.segment_lights
        on = sorted((light.segment for light in lights if light.source is LightSource.DDP), key=_start)
        strip, shadow = self.sender.strip, self.sender.stats.last_frame
        for run_start, run_end in _runs([light.segment for light in lights if light.source is not LightSource.DDP]):
            for start, end in _uncovered(Segment(_HELD, run_start, run_end - run_start), on):
                run = Segment(_HELD, start, end - start)
                offset = strip.byte_offset(run)
                if self.sent(run) and not any(shadow[offset : offset + strip.byte_length(run)]):
                    continue  # already held black
                await self.sender.off(run)

    def sent(self, segment: Segment) -> bool:
        """The sender holds every pixel of ``segment``: a frame reached them (and keepalive resends it)."""
        strip = self.sender.strip
        start, end = strip.byte_offset(segment), strip.byte_offset(segment) + strip.byte_length(segment)
        return any(offset <= start and end <= offset + length for offset, length in self.sender.held)

    async def stop_runner(self, key: str) -> None:
        """Stop and forget the runner for ``key``, if any. Never blanks the segment."""
        runner = self.runners.pop(key, None)
        if runner is not None:
            await runner.stop()


type DDPConfigEntry = ConfigEntry[DDPRuntime]


def _start(segment: Segment) -> int:
    return segment.start


def _runs(segments: list[Segment]) -> list[tuple[int, int]]:
    """The ``(start, end)`` pixel runs ``segments`` cover together: overlapping or adjacent ones merged, sorted."""
    runs: list[tuple[int, int]] = []
    for segment in sorted(segments, key=_start):
        if runs and segment.start <= runs[-1][1]:
            runs[-1] = (runs[-1][0], max(runs[-1][1], segment.end))
        else:
            runs.append((segment.start, segment.end))
    return runs


def _uncovered(segment: Segment, cover: list[Segment]) -> list[tuple[int, int]]:
    """The ``(start, end)`` pixel runs of ``segment`` that no segment in ``cover`` (sorted by start) covers."""
    runs, position = [], segment.start
    for other in cover:
        if other.start > position:
            runs.append((position, min(other.start, segment.end)))
        position = max(position, other.end)
        if position >= segment.end:
            break
    if position < segment.end:
        runs.append((position, segment.end))
    return [(start, end) for start, end in runs if start < end]


def make_strip(
    host: str,
    pixel_count: int,
    data_type: str,
    *,
    port: int = DDP_PORT,
    color_order: str | None = None,
    brightness_cap: int = DEFAULT_BRIGHTNESS_CAP,
) -> Strip:
    """An aioddp strip from stored values: ``data_type`` is "rgb"/"rgbw", ``brightness_cap`` a percent."""
    return Strip(
        host=host,
        pixel_count=pixel_count,
        port=port,
        data_type=DataType.RGBW if data_type == DATA_TYPE_RGBW else DataType.RGB,
        color_order=color_order or DEFAULT_COLOR_ORDER[data_type],
        brightness_cap=brightness_cap / 100,
    )


def build_strip(entry: DDPConfigEntry) -> Strip:
    """The strip for an entry's data and options. ``port`` in data is optional (tests point it at loopback)."""
    return make_strip(
        entry.data[CONF_HOST],
        entry.data[CONF_PIXEL_COUNT],
        entry.data[CONF_DATA_TYPE],
        port=entry.data.get(CONF_PORT, DDP_PORT),
        color_order=entry.options.get(CONF_COLOR_ORDER),
        brightness_cap=entry.options.get(CONF_BRIGHTNESS_CAP, DEFAULT_BRIGHTNESS_CAP),
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions once, so they exist whether or not an entry is loaded. The controller's
    actions take the controller light, ``set_pattern`` the DDP lights."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RECALL_PRESET,
        entity_domain=LIGHT_DOMAIN,
        schema={
            probatio.Required("preset"): probatio.All(probatio.Coerce(int), probatio.Range(min=1, max=PRESET_COUNT))
        },
        func="async_recall_preset",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_PATTERN,
        entity_domain=LIGHT_DOMAIN,
        schema={
            probatio.Required(ATTR_COLORS): probatio.All(
                [_PATTERN_COLOR], probatio.Length(min=1, max=MAX_PATTERN_COLORS)
            ),
            probatio.Optional(ATTR_BLOCK_SIZE): probatio.All(probatio.Coerce(int), probatio.Range(min=1)),
            probatio.Optional(ATTR_EFFECT): str,
            probatio.Optional(ATTR_PARAMS): dict,
            probatio.Optional(ATTR_BRIGHTNESS): probatio.All(probatio.Coerce(int), probatio.Range(min=1, max=255)),
        },
        func="async_set_pattern",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_COLORS,
        entity_domain=LIGHT_DOMAIN,
        schema={
            probatio.Required(ATTR_COLORS): probatio.All([_PALETTE_COLOR], probatio.Length(max=PALETTE_SLOTS)),
            probatio.Optional(ATTR_BACKGROUND): _RGB,
        },
        func="async_set_colors",
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DDPConfigEntry) -> bool:
    """Open the sender and forward to the light platform. Sends nothing; lights restore their own state."""
    # Built before the socket opens, so a host it rejects leaks nothing. No probing: the stored type decides.
    controller = create_controller(
        entry.data[CONF_CONTROLLER_TYPE], entry.data[CONF_HOST], entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
    )
    keepalive = float(entry.options.get(CONF_KEEPALIVE, DEFAULT_KEEPALIVE))
    sender = DDPSender(
        build_strip(entry),
        keepalive_interval=keepalive or None,
        repeat_frames=FRAME_REPEATS,
        repeat_interval=FRAME_REPEAT_INTERVAL,
    )
    try:
        await sender.open()
    except DDPSendError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="socket_failed",
            translation_placeholders={"host": entry.data[CONF_HOST], "error": str(err)},
        ) from err
    segments = [
        SegmentConfig(
            s[SEGMENT_KEY],
            s[SEGMENT_NAME],
            Segment(s[SEGMENT_KEY], s[SEGMENT_START], s[SEGMENT_LENGTH]),
        )
        for s in entry.data[CONF_SEGMENTS]
    ]
    scenes = DDPScenesCoordinator(hass, entry, controller) if controller is not None else None
    runtime = DDPRuntime(sender, segments, int(entry.options.get(CONF_FPS, DEFAULT_FPS)), controller, scenes)
    entry.runtime_data = runtime
    if scenes is not None:
        runtime.commands = ControllerCommands(runtime, scenes, entry)
        # Not a setup gate: the DDP lights don't need the controller's HTTP UI, so a silent one leaves the controller
        # device's entities unavailable until a poll reaches them, instead of failing the entry. The mode is then
        # unknown: a DDP light turned on tries the controller's Off best effort and goes ahead if it does not answer.
        await scenes.async_refresh()
        runtime.scene_mode = scene_mode(scenes.data.effect if scenes.data is not None else None)
        # Polls reach the DDP lights through the commands, whichever controller entities are enabled.
        scenes.on_poll = runtime.commands.follow_poll
        entry.async_on_unload(_stop_following(scenes))
        entry.async_on_unload(runtime.commands.cancel_restart)
    _remove_stale_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _remove_stale_entities(hass: HomeAssistant, entry: DDPConfigEntry) -> None:
    """Drop the Power switch and Intensity number of 0.4.0/0.4.1: their platforms are gone, so no platform setup
    can prune them."""
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.domain in (Platform.SWITCH, Platform.NUMBER):
            registry.async_remove(registered.entity_id)


def _stop_following(scenes: DDPScenesCoordinator) -> Callable[[], None]:
    """An unload callback that detaches the poll hook."""

    def stop() -> None:
        scenes.on_poll = None

    return stop


async def async_migrate_entry(hass: HomeAssistant, entry: DDPConfigEntry) -> bool:
    """1 -> 2: entries gain a controller type; a discovered NDB-Pro gets the NDB-Pro type, the rest are Generic."""
    if entry.version == 1:  # a newer version is refused by Home Assistant before this runs
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_CONTROLLER_TYPE: type_for_model(entry.data.get(CONF_MODEL))}, version=2
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DDPConfigEntry) -> bool:
    """Close the socket once the lights are gone. Never blanks the lights, so a reload doesn't flash the house."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.sender.close()  # the light entities stopped their runners as they were removed
    return unloaded
