"""DDP Pixel Controller: one DDP controller as a device with one light per segment. Protocol code lives in aioddp."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import probatio
from aioddp import DDP_PORT, DataType, DDPSender, DDPSendError, EffectRunner, Segment, Strip
from homeassistant.components.light.const import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import service
from homeassistant.helpers.typing import ConfigType

from .const import (
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
    PRESET_COUNT,
    SEGMENT_KEY,
    SEGMENT_LENGTH,
    SEGMENT_NAME,
    SEGMENT_START,
    SERVICE_RECALL_PRESET,
)
from .controller import Controller, create_controller, type_for_model
from .coordinator import DDPScenesCoordinator

if TYPE_CHECKING:
    from .light import DDPSegmentLight

PLATFORMS: list[Platform] = [Platform.LIGHT]
CONFIG_VERSION = 2  # the config flow's VERSION


@dataclass(frozen=True, slots=True)
class SegmentConfig:
    """One light's slice of the strip: ``key`` is stable (unique IDs), ``name`` is for display."""

    key: str
    name: str
    segment: Segment


@dataclass(slots=True)
class DDPRuntime:
    """Per-entry runtime: the one open sender, the segments, and at most one effect runner per segment key.

    ``controller`` drives a vendor controller's built-in effects (``None`` for Generic DDP) and ``scenes`` polls it,
    and ``segment_lights`` lets the controller light hand the pixels back from DDP.
    """

    sender: DDPSender
    segments: list[SegmentConfig]
    fps: int
    controller: Controller | None = None
    scenes: DDPScenesCoordinator | None = None
    runners: dict[str, EffectRunner] = field(default_factory=dict)
    segment_lights: list[DDPSegmentLight] = field(default_factory=list)

    def release_if_idle(self) -> None:
        """Stop keepalive resends once no segment light is on, so the controller's own effect comes back."""
        if not any(light.is_on for light in self.segment_lights):
            self.sender.release()

    async def stop_runner(self, key: str) -> None:
        """Stop and forget the runner for ``key``, if any. Never blanks the segment."""
        runner = self.runners.pop(key, None)
        if runner is not None:
            await runner.stop()


type DDPConfigEntry = ConfigEntry[DDPRuntime]


def make_strip(
    host: str,
    pixel_count: int,
    data_type: str,
    *,
    port: int = DDP_PORT,
    color_order: str | None = None,
    brightness_cap: int = DEFAULT_BRIGHTNESS_CAP,
) -> Strip:
    """A aioddp strip from stored values: ``data_type`` is "rgb"/"rgbw", ``brightness_cap`` a percent."""
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
    """Register the integration's action once, so it exists whether or not an entry is loaded."""
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
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DDPConfigEntry) -> bool:
    """Open the sender and forward to the light platform. Sends nothing; lights restore their own state."""
    # Built before the socket opens, so a host it rejects leaks nothing. No probing: the stored type decides.
    controller = create_controller(
        entry.data[CONF_CONTROLLER_TYPE], entry.data[CONF_HOST], entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
    )
    keepalive = float(entry.options.get(CONF_KEEPALIVE, DEFAULT_KEEPALIVE))
    sender = DDPSender(build_strip(entry), keepalive_interval=keepalive or None)
    try:
        await sender.open()
    except DDPSendError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="socket_failed",
            translation_placeholders={"host": entry.data[CONF_HOST], "error": str(err)},
        ) from err
    segments = [
        SegmentConfig(s[SEGMENT_KEY], s[SEGMENT_NAME], Segment(s[SEGMENT_KEY], s[SEGMENT_START], s[SEGMENT_LENGTH]))
        for s in entry.data[CONF_SEGMENTS]
    ]
    scenes = DDPScenesCoordinator(hass, entry, controller) if controller is not None else None
    runtime = DDPRuntime(sender, segments, int(entry.options.get(CONF_FPS, DEFAULT_FPS)), controller, scenes)
    entry.runtime_data = runtime
    if scenes is not None:
        # Not a setup gate: the DDP lights don't need the controller's HTTP UI, so a silent one leaves the Scenes
        # light unavailable until a poll reaches it, instead of failing the entry.
        await scenes.async_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: DDPConfigEntry) -> bool:
    """1 -> 2: entries gain a controller type; a discovered NDB-Pro keeps its Scenes light, the rest are Generic."""
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
