"""Diagnostics: the entry, the DDP sender, and (for a vendor controller) its raw config and status documents."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import DDPConfigEntry
from .const import CONF_HTTP_PORT
from .controller import CONTROLLER_ERRORS

TO_REDACT_ENTRY = {CONF_HOST, CONF_HTTP_PORT}

# Controller documents are allowlisted: any key not listed (addresses, MAC, Wi-Fi, user-set names, fields a firmware
# adds later) is replaced by REDACTED, so its presence stays visible but its value never leaves.
CONTROLLER_SAFE_KEYS = frozenset(
    {"config", "status", "ports", "p", "ts", "l", "ss", "rev"}
    | {"proto", "univ", "chip", "t0h", "t1h", "tbit", "tres", "conv"}
    | {"man", "mod", "ver", "push", "fuses", "vin", "amp", "links"}
)


def redact_controller_document(value: Any) -> Any:
    """Keep the values of allowlisted keys (recursing into containers) and redact every other key's value."""
    if isinstance(value, dict):
        return {
            key: redact_controller_document(item) if key in CONTROLLER_SAFE_KEYS else REDACTED
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_controller_document(item) for item in value]
    return value


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
            data["controller"] = redact_controller_document(
                {"config": await runtime.controller.config(), "status": await runtime.controller.status()}
            )
        except CONTROLLER_ERRORS as err:
            data["controller"] = {"error": type(err).__name__}  # the message embeds the host and port
    return data
