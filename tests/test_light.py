"""Light entity tests against a real loopback UDP listener: assert the DDP bytes on the wire."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_RGB_COLOR,
    EFFECT_OFF,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pyddp import DDPSendError, Strip, process_frame
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from .conftest import Listener

OUT1 = "light.ndb_pro_output_1"
OUT2 = "light.ndb_pro_output_2"
OUT2_OFFSET = 80 * 3


def _effect_tasks() -> list[asyncio.Task[Any]]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("pyddp-effect-") and not t.done()]


async def _turn_on(hass: HomeAssistant, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)


async def _turn_off(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("options", "cap", "order"),
    [({}, 0.6, "RGB"), ({"brightness_cap": 40, "color_order": "GRB", "fps": 30, "keepalive": 0}, 0.4, "GRB")],
    ids=["defaults", "options"],
)
async def test_solid_on_sends_color_through_pipeline(
    hass: HomeAssistant,
    udp_listener: Listener,
    entry_factory: Callable[..., MockConfigEntry],
    options: dict[str, Any],
    cap: float,
    order: str,
) -> None:
    entry = entry_factory(options=options)
    await _setup(hass, entry)

    await _turn_on(hass, OUT2, rgb_color=(255, 100, 0), brightness=128)

    packet = await udp_listener.wait_for(lambda p: p.offset == OUT2_OFFSET)
    strip = Strip("127.0.0.1", 122, port=udp_listener.port, color_order=order, brightness_cap=cap)
    assert packet.push
    assert packet.data_type == 0x0B
    assert packet.data == process_frame(bytes((255, 100, 0)) * 42, strip, brightness=128 / 255)

    state = hass.states.get(OUT2)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128
    assert state.attributes[ATTR_RGB_COLOR] == (255, 100, 0)
    assert state.attributes[ATTR_EFFECT] == EFFECT_OFF
    assert state.attributes[ATTR_EFFECT_LIST] == [EFFECT_OFF, "breathe", "chase", "twinkle", "rainbow", "palette"]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("both", [False, True], ids=["one-of-two", "last-one"])
async def test_turn_off_blanks_segment_and_releases_after_the_last(
    hass: HomeAssistant, udp_listener: Listener, setup_entry: MockConfigEntry, both: bool
) -> None:
    await _turn_on(hass, OUT1, rgb_color=(10, 20, 30))
    await _turn_on(hass, OUT2, rgb_color=(10, 20, 30))
    await udp_listener.wait_for(lambda p: p.offset == OUT2_OFFSET)
    await udp_listener.settle()
    udp_listener.packets.clear()

    await _turn_off(hass, OUT1)
    await udp_listener.wait_for(lambda p: p.offset == 0)
    await udp_listener.settle()

    assert [(p.offset, p.data, p.push) for p in udp_listener.packets] == [(0, bytes(80 * 3), True)]
    assert hass.states.get(OUT1).state == STATE_OFF  # type: ignore[union-attr]
    assert hass.states.get(OUT2).state == STATE_ON  # type: ignore[union-attr]

    if both:
        await _turn_off(hass, OUT2)
        await udp_listener.wait_for(lambda p: p.offset == OUT2_OFFSET and p.data == bytes(42 * 3))
    udp_listener.packets.clear()
    # Keepalive is on by default (1 s) and resends the whole strip while any segment is on. Once none is, the
    # sender is released so the controller's built-in effect can take the pixels back.
    keepalive = (0, bytes(80 * 3) + process_frame(bytes((10, 20, 30)) * 42, setup_entry.runtime_data.sender.strip))
    if both:
        await udp_listener.settle(2.2)
        assert udp_listener.packets == []
    else:
        packet = await udp_listener.wait_for(lambda p: len(p.data) == 122 * 3, timeout=2.5)
        assert (packet.offset, packet.data) == keepalive


async def test_effect_runs_updates_and_stops_on_solid(
    hass: HomeAssistant, udp_listener: Listener, setup_entry: MockConfigEntry
) -> None:
    runtime = setup_entry.runtime_data
    await _turn_on(hass, OUT1, effect="chase", rgb_color=(0, 0, 255), brightness=255)
    runner = runtime.runners["output1"]
    assert runner.running
    assert [t.get_name() for t in _effect_tasks()] == ["pyddp-effect-chase-output1"]
    await udp_listener.wait_for(lambda p: p.offset == 0)
    assert hass.states.get(OUT1).attributes[ATTR_EFFECT] == "chase"  # type: ignore[union-attr]

    # Same effect again: the running runner is updated in place, not restarted.
    await _turn_on(hass, OUT1, brightness=50)
    assert runtime.runners["output1"] is runner
    assert runner.running

    await _turn_on(hass, OUT1, effect=EFFECT_OFF)
    assert "output1" not in runtime.runners
    assert not runner.running
    assert _effect_tasks() == []
    await udp_listener.settle()
    solid = process_frame(bytes((0, 0, 255)) * 80, runtime.sender.strip, brightness=50 / 255)
    assert udp_listener.packets[-1].data == solid

    udp_listener.packets.clear()
    await udp_listener.settle()
    assert udp_listener.packets == []


async def test_restore_resends_last_state(
    hass: HomeAssistant, udp_listener: Listener, entry_factory: Callable[..., MockConfigEntry]
) -> None:
    mock_restore_cache(
        hass,
        [
            State(OUT1, STATE_ON, {ATTR_BRIGHTNESS: 100, ATTR_RGB_COLOR: [0, 255, 0], ATTR_EFFECT: EFFECT_OFF}),
            State(OUT2, STATE_ON, {ATTR_BRIGHTNESS: 200, ATTR_RGB_COLOR: [255, 0, 0], ATTR_EFFECT: "rainbow"}),
        ],
    )
    entry = entry_factory()
    await _setup(hass, entry)

    packet = await udp_listener.wait_for(lambda p: p.offset == 0)
    assert packet.data == process_frame(bytes((0, 255, 0)) * 80, entry.runtime_data.sender.strip, brightness=100 / 255)
    assert entry.runtime_data.runners["output2"].running
    await udp_listener.wait_for(lambda p: p.offset == OUT2_OFFSET)

    out1, out2 = hass.states.get(OUT1), hass.states.get(OUT2)
    assert out1 is not None and out2 is not None
    assert (out1.state, out1.attributes[ATTR_BRIGHTNESS], out1.attributes[ATTR_RGB_COLOR]) == (
        STATE_ON,
        100,
        (0, 255, 0),
    )
    assert (out2.state, out2.attributes[ATTR_EFFECT], out2.attributes[ATTR_RGB_COLOR]) == (
        STATE_ON,
        "rainbow",
        (255, 0, 0),
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_restored_off_light_turns_on_with_last_look(
    hass: HomeAssistant, udp_listener: Listener, entry_factory: Callable[..., MockConfigEntry]
) -> None:
    # An off light's state attributes are all None, so the last look survives only in extra restore data.
    off = {ATTR_BRIGHTNESS: None, ATTR_RGB_COLOR: None, ATTR_EFFECT: None}
    mock_restore_cache_with_extra_data(
        hass,
        [
            (State(OUT1, STATE_OFF, off), {"brightness": 100, "rgb_color": [0, 255, 0], "effect": EFFECT_OFF}),
            (State(OUT2, STATE_OFF, off), {"brightness": 200, "rgb_color": [255, 0, 0], "effect": "chase"}),
        ],
    )
    entry = entry_factory()
    await _setup(hass, entry)
    await udp_listener.settle()
    assert udp_listener.packets == []

    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: [OUT1, OUT2]}, blocking=True)

    packet = await udp_listener.wait_for(lambda p: p.offset == 0)
    assert packet.data == process_frame(bytes((0, 255, 0)) * 80, entry.runtime_data.sender.strip, brightness=100 / 255)
    assert entry.runtime_data.runners["output2"].running
    out2 = hass.states.get(OUT2)
    assert out2 is not None
    assert (out2.attributes[ATTR_BRIGHTNESS], out2.attributes[ATTR_RGB_COLOR], out2.attributes[ATTR_EFFECT]) == (
        200,
        (255, 0, 0),
        "chase",
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_stops_runners_without_blanking(
    hass: HomeAssistant, udp_listener: Listener, setup_entry: MockConfigEntry
) -> None:
    await udp_listener.settle()
    assert udp_listener.packets == []  # setup with nothing to restore sends nothing

    await _turn_on(hass, OUT1, effect="rainbow")
    await udp_listener.wait_for(lambda p: p.offset == 0)
    sender = setup_entry.runtime_data.sender

    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    frames = sender.stats.frames_sent
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    assert _effect_tasks() == []
    assert not sender.is_open

    await udp_listener.settle()
    assert sender.stats.frames_sent == frames
    assert all(any(p.data) for p in udp_listener.packets)  # rainbow is never all-black; a blanking frame would be


@pytest.mark.parametrize(
    ("data", "error"),
    [({"rgb_color": (1, 2, 3)}, HomeAssistantError), ({"effect": "strobe"}, ServiceValidationError)],
    ids=["send-fails", "unknown-effect"],
)
async def test_turn_on_errors(
    hass: HomeAssistant, setup_entry: MockConfigEntry, data: dict[str, Any], error: type[Exception]
) -> None:
    if error is HomeAssistantError:
        await setup_entry.runtime_data.sender.close()  # every send now raises DDPSendError
    with pytest.raises(error):
        await _turn_on(hass, OUT1, **data)
    assert hass.states.get(OUT1).state == STATE_OFF  # type: ignore[union-attr]


async def test_setup_retries_when_socket_will_not_open(
    hass: HomeAssistant, entry_factory: Callable[..., MockConfigEntry]
) -> None:
    entry = entry_factory()
    with patch("custom_components.ddp.DDPSender.open", side_effect=DDPSendError("no route")):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
