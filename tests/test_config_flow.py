"""Config and options flow tests."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aioddp import DDPQueryError, DDPSendError, parse_config
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_socket import socket_allow_hosts

from custom_components.ddp.const import DOMAIN

from .conftest import FIXTURES, FakeController

HOST = "127.0.0.1"
# The controller device's entities (platform, unique-ID suffix), which only a vendor controller type has.
CONTROLLER_ENTITIES = [
    ("switch", "power"),
    *(("light", f"color_{slot}") for slot in range(1, 6)),
    ("select", "scene"),
    ("number", "intensity"),
]
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

    DDP discovery finds nothing unless a test patches it. The flow also opens (and closes) a real UDP socket to the
    host; it never sends.
    """
    fake_controller.offline = True
    with (
        patch("custom_components.ddp.config_flow.DEFAULT_HTTP_PORT", fake_controller.port),
        patch(DISCOVER, AsyncMock(return_value=None)),
    ):
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
    device_registry: dr.DeviceRegistry,
    flow_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    current: str,
    target: str,
) -> None:
    flow_controller.offline = False
    entry = entry_factory(controller=flow_controller, controller_type=current)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _controller_entities(entity_registry, entry) == (CONTROLLER_ENTITIES if current == "ndbpro" else [])
    controller_device = (DOMAIN, f"{HOST}:controller")
    assert (device_registry.async_get_device_by_identifier(controller_device, entry.entry_id) is not None) == (
        current == "ndbpro"
    )

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    assert result["description_placeholders"] == {
        "detected": "Minleon NDB-Pro",
        "data_type": "rgb",
        # Generic DDP has no HTTP layout and DDP discovery is silent, so only the NDB-Pro type reports one.
        "layout": "rgb, 2 outputs, 122 pixels" if current == "ndbpro" else "none",
    }
    assert _default_type(result) == current

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"controller_type": target})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["controller_type"] == target
    assert entry.state is ConfigEntryState.LOADED
    assert (entry.runtime_data.controller is not None) == (target == "ndbpro")
    assert _controller_entities(entity_registry, entry) == (CONTROLLER_ENTITIES if target == "ndbpro" else [])
    # Switching to Generic removes the controller device with its entities; the DDP device stays, renamed.
    assert (device_registry.async_get_device_by_identifier(controller_device, entry.entry_id) is not None) == (
        target == "ndbpro"
    )
    ddp_device = device_registry.async_get_device_by_identifier((DOMAIN, HOST), entry.entry_id)
    assert ddp_device is not None
    assert ddp_device.name == ("NDB-Pro Generic DDP" if target == "ndbpro" else "NDB-Pro")
    assert await hass.config_entries.async_unload(entry.entry_id)


def _controller_entities(registry: er.EntityRegistry, entry: MockConfigEntry) -> list[tuple[str, str]]:
    return [
        (platform, key)
        for platform, key in CONTROLLER_ENTITIES
        if registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}") is not None
    ]


@pytest.mark.parametrize("title", [HOST, "Roofline"], ids=["title-is-host", "custom-title"])
@pytest.mark.parametrize(
    ("other_entry", "socket_fails", "result_type", "reason_or_error"),
    [
        (False, False, FlowResultType.ABORT, "reconfigure_successful"),
        (True, False, FlowResultType.ABORT, "already_configured"),
        (False, True, FlowResultType.FORM, "cannot_connect"),
    ],
    ids=["moved", "host-taken", "unreachable"],
)
async def test_reconfigure_changes_host_and_keeps_the_device(
    hass: HomeAssistant,
    flow_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    title: str,
    other_entry: bool,
    socket_fails: bool,
    result_type: FlowResultType,
    reason_or_error: str,
) -> None:
    socket_allow_hosts(["127.0.0.1", "127.0.0.2"])  # the new host is a second loopback address
    flow_controller.offline = False
    entry = entry_factory(controller=flow_controller)
    hass.config_entries.async_update_entry(entry, title=title)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = dr.async_get(hass)
    devices = {
        suffix: registry.async_get_device_by_identifier((DOMAIN, f"{HOST}{suffix}"), entry.entry_id)
        for suffix in ("", ":controller")
    }
    assert None not in devices.values()
    if other_entry:
        MockConfigEntry(domain=DOMAIN, unique_id="127.0.0.2", data={"host": "127.0.0.2"}).add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch(OPEN, side_effect=DDPSendError("no route")) if socket_fails else contextlib.nullcontext():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": " 127.0.0.2 ", "controller_type": "ndbpro"}
        )
    await hass.async_block_till_done()

    assert result["type"] is result_type
    if result_type is FlowResultType.FORM:
        assert result["errors"] == {"base": reason_or_error}
        assert entry.data["host"] == HOST
    else:
        assert result["reason"] == reason_or_error
        moved = reason_or_error == "reconfigure_successful"
        assert entry.data["host"] == ("127.0.0.2" if moved else HOST)
        assert entry.unique_id == ("127.0.0.2" if moved else HOST)
        assert entry.title == ("127.0.0.2" if moved and title == HOST else title)  # follows only an untouched title
        # Both devices (the DDP device and the controller device) follow the host, keeping their IDs.
        for suffix, device in devices.items():
            new = registry.async_get_device_by_identifier((DOMAIN, f"127.0.0.2{suffix}"), entry.entry_id)
            old = registry.async_get_device_by_identifier((DOMAIN, f"{HOST}{suffix}"), entry.entry_id)
            ids = (new.id if new else None, old.id if old else None)
            assert ids == ((device.id, None) if moved else (None, device.id))  # type: ignore[union-attr]
    assert await hass.config_entries.async_unload(entry.entry_id)


def _serve_config(controller: FakeController, conv: int, ports: list[tuple[int, int]]) -> None:
    """Make the fake NDB-Pro report input conversion ``conv`` and outputs of (length, start slot), the rest empty."""
    config = json.loads((FIXTURES / "api_config.json").read_text())
    config["config"]["conv"] = conv
    for port, (length, slot) in zip(config["config"]["ports"], ports + [(0, 1)] * 8, strict=False):
        port["l"], port["ss"] = length, slot
    controller.pages["/api/config"] = json.dumps(config)


@pytest.mark.parametrize(
    ("conv", "ports", "data_type", "keys", "options", "kept_options"),
    [
        # Switched to a 4-channel input mode: output 2 moves to slot 321 (80 px x 4 channels), same two outputs.
        (2, [(80, 1), (42, 321)], "rgbw", ["output1", "output2"], {"color_order": "GRB", "fps": 20}, {"fps": 20}),
        (13, [(80, 1)], "rgb", ["output1"], {"color_order": "GRB"}, {"color_order": "GRB"}),  # output 2 dropped
    ],
    ids=["rgbw-drops-rgb-color-order", "output-dropped"],
)
async def test_reconfigure_rereads_the_layout(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    flow_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    conv: int,
    ports: list[tuple[int, int]],
    data_type: str,
    keys: list[str],
    options: dict[str, Any],
    kept_options: dict[str, Any],
) -> None:
    flow_controller.offline = False
    entry = entry_factory(controller=flow_controller, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = {
        key: entity_registry.async_get_entity_id("light", DOMAIN, f"{entry.entry_id}_{key}")
        for key in ("output1", "output2", "strip", "color_1")
    }
    assert None not in before.values()

    _serve_config(flow_controller, conv, ports)
    result = await entry.start_reconfigure_flow(hass)
    assert result["description_placeholders"]["data_type"] == "rgb"  # type: ignore[index]
    assert result["description_placeholders"]["layout"] == (  # type: ignore[index]
        f"{data_type}, {len(keys)} output{'s' if len(keys) > 1 else ''}, {80 if len(keys) == 1 else 122} pixels"
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"controller_type": "ndbpro"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["data_type"] == data_type
    assert [s["key"] for s in entry.data["segments"]] == keys
    assert entry.data["pixel_count"] == (122 if len(keys) > 1 else 80)
    assert (entry.data["manufacturer"], entry.data["model"], entry.data["firmware"]) == ("3WL", "NDBPRO", "2.0")
    assert entry.options == kept_options
    assert entry.runtime_data.sender.strip.channels == (4 if data_type == "rgbw" else 3)
    # Unchanged outputs keep their entities (unique IDs are the layout's segment names); a dropped output and,
    # with one output left, All outputs are removed. The controller's Color lights stay.
    after = {key: entity_registry.async_get_entity_id("light", DOMAIN, f"{entry.entry_id}_{key}") for key in before}
    expected = {key: before[key] if key in keys or key == "color_1" or len(keys) > 1 else None for key in before}
    assert after == expected
    out1 = hass.states.get(before["output1"])  # type: ignore[arg-type]
    assert out1 is not None
    assert out1.attributes["supported_color_modes"] == [data_type]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_without_a_layout_keeps_the_stored_one(
    hass: HomeAssistant, flow_controller: FakeController, entry_factory: Callable[..., MockConfigEntry]
) -> None:
    entry = entry_factory(controller=flow_controller, data_type="rgbw", options={"color_order": "GRBW"})
    stored = dict(entry.data)
    result = await entry.start_reconfigure_flow(hass)  # the controller is offline: no layout over DDP or HTTP
    assert result["description_placeholders"] == {"detected": "Generic DDP", "data_type": "rgbw", "layout": "none"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"controller_type": "ndbpro"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.data) == stored
    assert entry.options == {"color_order": "GRBW"}
    assert await hass.config_entries.async_unload(entry.entry_id)
