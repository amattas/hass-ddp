"""DDP Pixel Controller: one DDP controller as a device with one light per segment. Protocol code lives in pyddp."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from pyddp import DDP_PORT, DataType, DDPSender, DDPSendError, EffectRunner, Segment, Strip

from .const import (
    CONF_BRIGHTNESS_CAP,
    CONF_COLOR_ORDER,
    CONF_DATA_TYPE,
    CONF_FPS,
    CONF_KEEPALIVE,
    CONF_PIXEL_COUNT,
    CONF_SEGMENTS,
    DATA_TYPE_RGBW,
    DEFAULT_BRIGHTNESS_CAP,
    DEFAULT_COLOR_ORDER,
    DEFAULT_FPS,
    DEFAULT_KEEPALIVE,
    SEGMENT_KEY,
    SEGMENT_LENGTH,
    SEGMENT_NAME,
    SEGMENT_START,
)

PLATFORMS: list[Platform] = [Platform.LIGHT]


@dataclass(frozen=True, slots=True)
class SegmentConfig:
    """One light's slice of the strip: ``key`` is stable (unique IDs), ``name`` is for display."""

    key: str
    name: str
    segment: Segment


@dataclass(slots=True)
class DDPRuntime:
    """Per-entry runtime: the one open sender, the segments, and at most one effect runner per segment key."""

    sender: DDPSender
    segments: list[SegmentConfig]
    fps: int
    runners: dict[str, EffectRunner] = field(default_factory=dict)

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
    """A pyddp strip from stored values: ``data_type`` is "rgb"/"rgbw", ``brightness_cap`` a percent."""
    return Strip(
        host=host,
        pixel_count=pixel_count,
        port=port,
        data_type=DataType.RGBW if data_type == DATA_TYPE_RGBW else DataType.RGB,
        color_order=color_order or DEFAULT_COLOR_ORDER[data_type],  # pyddp defaults to "RGB" even for RGBW
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


async def async_setup_entry(hass: HomeAssistant, entry: DDPConfigEntry) -> bool:
    """Open the sender and forward to the light platform. Sends nothing; lights restore their own state."""
    keepalive = float(entry.options.get(CONF_KEEPALIVE, DEFAULT_KEEPALIVE))
    sender = DDPSender(build_strip(entry), keepalive_interval=keepalive or None)
    try:
        await sender.open()
    except DDPSendError as err:
        raise ConfigEntryNotReady(f"Cannot open DDP socket to {entry.data[CONF_HOST]}: {err}") from err
    segments = [
        SegmentConfig(s[SEGMENT_KEY], s[SEGMENT_NAME], Segment(s[SEGMENT_KEY], s[SEGMENT_START], s[SEGMENT_LENGTH]))
        for s in entry.data[CONF_SEGMENTS]
    ]
    entry.runtime_data = DDPRuntime(sender, segments, int(entry.options.get(CONF_FPS, DEFAULT_FPS)))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DDPConfigEntry) -> bool:
    """Stop every runner and close the socket. Never blanks the lights, so a reload doesn't flash the house."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime = entry.runtime_data
        for key in list(runtime.runners):
            await runtime.stop_runner(key)
        await runtime.sender.close()
    return unloaded
