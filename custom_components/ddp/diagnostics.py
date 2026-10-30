"""Diagnostics: the entry, the DDP sender, and (for a vendor controller) its raw config and status documents."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import DDPConfigEntry
from .controller import CONTROLLER_ERRORS

# Network identity in a controller's own documents: addresses, MAC, and Wi-Fi name and credentials.
TO_REDACT_ENTRY = {CONF_HOST}
TO_REDACT_CONTROLLER = {"ip", "nm", "gw", "dns", "mac", "bssid", "ssid", "pass", "password", "psk"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: DDPConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    strip = runtime.sender.strip
    stats = dataclasses.asdict(runtime.sender.stats)
    del stats["last_frame"]  # raw pixel bytes: not diagnostic, and not JSON
    data: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT_ENTRY),
            "options": dict(entry.options),
        },
        "strip": async_redact_data(
            {
                "host": strip.host,
                "port": strip.port,
                "pixel_count": strip.pixel_count,
                "data_type": strip.data_type.name,
                "color_order": strip.color_order,
                "brightness_cap": strip.brightness_cap,
            },
            TO_REDACT_ENTRY,
        ),
        "sender": {"is_open": runtime.sender.is_open, "stats": stats},
        "runners": sorted(runtime.runners),
        "scenes": None,
        "controller": None,
    }
    if runtime.scenes is not None and runtime.controller is not None:
        scene = runtime.scenes.data
        data["scenes"] = {
            "available": runtime.scenes.last_update_success,
            "effect": scene.effect if scene else None,
            "intensity": scene.intensity if scene else None,
            "effects": list(scene.effects) if scene else [],
        }
        try:
            data["controller"] = async_redact_data(
                {"config": await runtime.controller.config(), "status": await runtime.controller.status()},
                TO_REDACT_CONTROLLER,
            )
        except CONTROLLER_ERRORS as err:
            data["controller"] = {"error": str(err)}
    return data
