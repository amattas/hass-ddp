"""Entry migration."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ddp.const import DOMAIN
from custom_components.ddp.controller import fetch_layout


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
    registry = er.async_get(hass)
    scenes_unique_id = f"{entry.entry_id}_controller"
    if controller_type == "ndbpro":  # the Scenes light a v1 NDB-Pro entry already had
        old = registry.async_get_or_create(
            "light", DOMAIN, scenes_unique_id, config_entry=entry, suggested_object_id="legacy_scenes"
        )

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is state
    assert entry.data.get("controller_type") == controller_type
    if controller_type is not None:
        assert entry.version == 2
        assert (entry.runtime_data.controller is not None) == (controller_type == "ndbpro")
        if controller_type == "ndbpro":
            assert registry.async_get_entity_id("light", DOMAIN, scenes_unique_id) == old.entity_id
            assert old.entity_id == "light.legacy_scenes"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unknown_controller_type_fails_setup_cleanly(hass: HomeAssistant, udp_listener: Any) -> None:
    """A type this release doesn't know (after a downgrade) is a permanent config error, not a KeyError."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="127.0.0.1",
        data={
            "host": "127.0.0.1",
            "port": udp_listener.port,
            "pixel_count": 10,
            "data_type": "rgb",
            "segments": [{"key": "all", "name": "All", "start": 0, "length": 10}],
            "controller_type": "from-the-future",
        },
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "from-the-future" in (entry.reason or "")
    assert await fetch_layout("from-the-future", "127.0.0.1") is None


async def test_action_is_registered_by_async_setup_without_an_entry(hass: HomeAssistant) -> None:
    """The action exists once the integration is set up, whether or not any entry is loaded."""
    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.services.has_service(DOMAIN, "recall_preset")
