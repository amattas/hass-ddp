"""Config and options flow tests."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pyddp import DDPQueryError, DDPSendError
from pyddp.discovery import parse_config
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ddp.const import DOMAIN

HOST = "127.0.0.1"
DISCOVER = "custom_components.ddp.config_flow.discover"
OPEN = "custom_components.ddp.config_flow.DDPSender.open"

NDB_CONFIG = {
    "config": {
        "conv": 13,
        "proto": 0,
        "ports": [
            {"p": 0, "l": 80, "ss": 1},
            {"p": 1, "l": 42, "ss": 241},
            {"p": 2, "l": 0, "ss": 1},
        ],
    }
}
NDB_STATUS = {"status": {"man": "Minleon", "mod": "NDB-Pro", "ver": "1.2.3"}}


@pytest.fixture(autouse=True)
def _loopback_sockets(socket_enabled: None) -> None:
    """The flow opens (and closes) a real UDP socket to the host; it never sends."""


async def _start(hass: HomeAssistant) -> config_entries.ConfigFlowResult:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_discovered_controller_creates_entry(hass: HomeAssistant) -> None:
    layout = parse_config(NDB_CONFIG, host=HOST, source="ddp", status=NDB_STATUS)
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(DISCOVER, AsyncMock(return_value=layout)) as discover:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})
    await hass.async_block_till_done()

    discover.assert_awaited_once_with(HOST)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "NDB-Pro"
    assert result["result"].unique_id == HOST
    assert result["data"] == {
        "host": HOST,
        "pixel_count": 122,
        "data_type": "rgb",
        "segments": [
            {"key": "output1", "name": "Output 1", "start": 0, "length": 80},
            {"key": "output2", "name": "Output 2", "start": 80, "length": 42},
        ],
        "manufacturer": "Minleon",
        "model": "NDB-Pro",
        "firmware": "1.2.3",
    }


MONO_CONFIG = {"config": {"conv": 3, "ports": [{"p": 0, "l": 10, "ss": 1}]}}
EMPTY_CONFIG = {"config": {"conv": 13, "ports": [{"p": 0, "l": 0, "ss": 1}]}}


@pytest.mark.parametrize(
    ("discovered", "data_type"),
    [
        (AsyncMock(return_value=None), "rgb"),
        (AsyncMock(side_effect=DDPQueryError("garbled reply")), "rgbw"),
        (AsyncMock(return_value=parse_config(MONO_CONFIG, host=HOST, source="ddp")), "rgb"),
        (AsyncMock(return_value=parse_config(EMPTY_CONFIG, host=HOST, source="http")), "rgb"),
    ],
    ids=["no-reply", "query-error", "undrivable-data-type", "no-active-outputs"],
)
async def test_manual_step_when_discovery_gives_nothing_usable(
    hass: HomeAssistant, discovered: AsyncMock, data_type: str
) -> None:
    result = await _start(hass)
    with patch(DISCOVER, discovered):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pixel_count": 300, "data_type": data_type}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == HOST
    assert result["data"] == {
        "host": HOST,
        "pixel_count": 300,
        "data_type": data_type,
        "segments": [{"key": "all", "name": "All", "start": 0, "length": 300}],
    }


@pytest.mark.parametrize(
    ("discovered", "pixel_count", "error"),
    [(True, None, "cannot_connect"), (False, 50, "cannot_connect"), (False, 2**32, "invalid_config")],
    ids=["discovered", "manual", "manual-too-many-pixels"],
)
async def test_socket_check_errors(hass: HomeAssistant, discovered: bool, pixel_count: int | None, error: str) -> None:
    layout = parse_config(NDB_CONFIG, host=HOST, source="ddp", status=NDB_STATUS) if discovered else None
    result = await _start(hass)
    with patch(DISCOVER, AsyncMock(return_value=layout)), patch(OPEN, side_effect=DDPSendError("no route")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})
        if not discovered:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"pixel_count": pixel_count, "data_type": "rgb"}
            )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == ("user" if discovered else "manual")
    assert result["errors"] == {"base": error}


async def test_duplicate_host_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=HOST, data={"host": HOST}).add_to_hass(hass)
    result = await _start(hass)
    with patch(DISCOVER, AsyncMock()) as discover:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    discover.assert_not_awaited()


async def test_options_flow_saves_and_reloads(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    old_sender = setup_entry.runtime_data.sender
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"brightness_cap": 40, "color_order": "GRB", "fps": 20, "keepalive": 0.2}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"keepalive": "invalid_keepalive"}

    options = {"brightness_cap": 40, "color_order": "GRB", "fps": 20, "keepalive": 2.5}
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_entry.options == options
    assert setup_entry.state is ConfigEntryState.LOADED

    runtime = setup_entry.runtime_data
    assert runtime.sender is not old_sender
    assert not old_sender.is_open
    assert runtime.sender.strip.brightness_cap == pytest.approx(0.4)
    assert runtime.sender.strip.color_order == "GRB"
    assert runtime.fps == 20


@pytest.mark.parametrize(("data_type", "count"), [("rgb", 6), ("rgbw", 24)])
async def test_options_color_orders_follow_data_type(
    hass: HomeAssistant, entry_factory: Callable[..., MockConfigEntry], data_type: str, count: int
) -> None:
    entry = entry_factory()
    hass.config_entries.async_update_entry(entry, data={**entry.data, "data_type": data_type})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]
    assert schema is not None
    selector = schema.schema["color_order"]
    assert len(selector.config["options"]) == count
