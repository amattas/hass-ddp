"""Config and options flow tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pyddp import DDPQueryError, DDPSendError, parse_config
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ddp.const import DOMAIN

from .conftest import FakeController

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
def flow_controller(fake_controller: FakeController) -> Iterator[FakeController]:
    """The flow's detection and HTTP layout fetch talk to this loopback NDB-Pro, offline (so Generic) by default.

    The flow also opens (and closes) a real UDP socket to the host; it never sends.
    """
    fake_controller.offline = True
    with patch("custom_components.ddp.config_flow.DEFAULT_HTTP_PORT", fake_controller.port):
        yield fake_controller


async def _start(hass: HomeAssistant) -> config_entries.ConfigFlowResult:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def _enter_host(
    hass: HomeAssistant, layout: Any = None, *, discovered: AsyncMock | None = None
) -> config_entries.ConfigFlowResult:
    """Start the flow and submit the host; DDP discovery answers ``layout`` (or ``discovered``)."""
    result = await _start(hass)
    assert result["step_id"] == "user"
    with patch(DISCOVER, discovered or AsyncMock(return_value=layout)):
        return await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})


def _default_type(result: config_entries.ConfigFlowResult) -> str:
    """The controller type the form preselects (validating an empty submission applies the defaults)."""
    assert result["data_schema"] is not None
    return str(result["data_schema"]({})["controller_type"])


async def test_discovered_controller_creates_entry(hass: HomeAssistant, flow_controller: FakeController) -> None:
    flow_controller.offline = False
    layout = parse_config(NDB_CONFIG, host=HOST, source="ddp", status=NDB_STATUS)
    with patch(DISCOVER, AsyncMock(return_value=layout)) as discover:
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": HOST})
    discover.assert_awaited_once_with(HOST)
    assert result["step_id"] == "controller"
    assert result["description_placeholders"] == {"detected": "Minleon NDB-Pro"}
    selector = result["data_schema"].schema["controller_type"]  # type: ignore[union-attr]
    assert [o["value"] for o in selector.config["options"]] == ["generic", "ndbpro"]  # Generic is always offered

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "NDB-Pro"
    assert result["result"].unique_id == HOST
    assert result["result"].version == 2
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
        "controller_type": "ndbpro",
    }


@pytest.mark.parametrize(
    ("offline", "ddp_layout", "choice", "default", "expected"),
    [
        (False, True, None, "ndbpro", "ndbpro"),
        (False, False, None, "ndbpro", "ndbpro"),  # DDP is silent: the layout comes from the NDB-Pro's HTTP API
        (True, True, None, "generic", "generic"),
        (False, True, "generic", "ndbpro", "generic"),  # the user overrides the detected type
        (True, True, "ndbpro", "generic", "ndbpro"),  # ... or picks a vendor type detection missed
    ],
    ids=["detected-ndbpro", "http-layout", "detected-generic", "override-to-generic", "override-to-ndbpro"],
)
async def test_controller_type_is_preselected_and_overridable(
    hass: HomeAssistant,
    flow_controller: FakeController,
    offline: bool,
    ddp_layout: bool,
    choice: str | None,
    default: str,
    expected: str,
) -> None:
    flow_controller.offline = offline
    layout = parse_config(NDB_CONFIG, host=HOST, source="ddp", status=NDB_STATUS) if ddp_layout else None
    result = await _enter_host(hass, layout)
    assert result["step_id"] == "controller"
    assert _default_type(result) == default

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"controller_type": choice} if choice else {}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["controller_type"] == expected
    assert [s["length"] for s in result["data"]["segments"]] == [80, 42]


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
    result = await _enter_host(hass, discovered=discovered)
    assert result["step_id"] == "controller"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"controller_type": "generic"})
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
        "controller_type": "generic",
    }


@pytest.mark.parametrize(
    ("discovered", "pixel_count", "error"),
    [(True, None, "cannot_connect"), (False, 50, "cannot_connect"), (False, 2**32, "invalid_config")],
    ids=["discovered", "manual", "manual-too-many-pixels"],
)
async def test_socket_check_errors(hass: HomeAssistant, discovered: bool, pixel_count: int | None, error: str) -> None:
    layout = parse_config(NDB_CONFIG, host=HOST, source="ddp", status=NDB_STATUS) if discovered else None
    with patch(OPEN, side_effect=DDPSendError("no route")):
        result = await _enter_host(hass, layout)
        # Detection finds Generic here; the user picks NDB-Pro and the re-shown form must keep that choice.
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"controller_type": "ndbpro"})
        if not discovered:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"pixel_count": pixel_count, "data_type": "rgb"}
            )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == ("controller" if discovered else "manual")
    assert result["errors"] == {"base": error}
    if discovered:
        assert _default_type(result) == "ndbpro"


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


@pytest.mark.parametrize(("current", "target"), [("ndbpro", "generic"), ("generic", "ndbpro")])
async def test_reconfigure_changes_controller_type_and_reloads(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    flow_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    current: str,
    target: str,
) -> None:
    flow_controller.offline = False
    entry = entry_factory(controller=flow_controller, controller_type=current)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert (entity_registry.async_get("light.ndb_pro_scenes") is not None) == (current == "ndbpro")

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    assert result["description_placeholders"] == {"detected": "Minleon NDB-Pro"}
    assert _default_type(result) == current

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"controller_type": target})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["controller_type"] == target
    assert entry.state is ConfigEntryState.LOADED
    assert (entry.runtime_data.controller is not None) == (target == "ndbpro")
    assert (entity_registry.async_get("light.ndb_pro_scenes") is not None) == (target == "ndbpro")
    assert await hass.config_entries.async_unload(entry.entry_id)
