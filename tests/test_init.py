"""Entry migration."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ddp.const import DOMAIN


@pytest.mark.parametrize(
    ("data", "version", "state", "controller_type"),
    [
        ({"model": "NDBPRO"}, 1, ConfigEntryState.LOADED, "ndbpro"),
        ({"model": "NDB-Pro"}, 1, ConfigEntryState.LOADED, "generic"),
        ({}, 1, ConfigEntryState.LOADED, "generic"),  # a manual entry has no model
        ({"model": "NDBPRO"}, 3, ConfigEntryState.MIGRATION_ERROR, None),  # written by a newer release
    ],
    ids=["ndbpro", "other-model", "manual", "downgrade"],
)
async def test_migrate_v1_entry(
    hass: HomeAssistant,
    udp_listener: Any,
    data: dict[str, Any],
    version: int,
    state: ConfigEntryState,
    controller_type: str | None,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=version,
        unique_id="127.0.0.1",
        data={
            "host": "127.0.0.1",
            "port": udp_listener.port,
            "pixel_count": 10,
            "data_type": "rgb",
            "segments": [{"key": "all", "name": "All", "start": 0, "length": 10}],
            "http_port": 1,  # nothing listens: the Scenes light starts unavailable, which setup tolerates
            **data,
        },
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is state
    assert entry.data.get("controller_type") == controller_type
    if controller_type is not None:
        assert entry.version == 2
        assert (entry.runtime_data.controller is not None) == (controller_type == "ndbpro")
        assert await hass.config_entries.async_unload(entry.entry_id)
