"""Light entity tests against a real loopback UDP listener: assert the DDP bytes on the wire."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any
from unittest.mock import patch

import pytest
from aioddp import DataType, DDPSendError, Strip, process_frame
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_SUPPORTED_COLOR_MODES,
    EFFECT_OFF,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    ColorMode,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.ddp.const import DOMAIN

from .conftest import SEGMENTS, Listener

OUT1 = "light.ndb_pro_output_1"
OUT2 = "light.ndb_pro_output_2"
ALL = "light.ndb_pro_all_outputs"
OUT2_OFFSET = 80 * 3
RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)
WHITE_W = (0, 0, 0, 255)  # the white channel alone: an RGBW light's default look


def _color_attr(color: tuple[int, ...]) -> str:
    """The light attribute (and turn_on key) for ``color``: RGBW for 4 components, else RGB."""
    return ATTR_RGBW_COLOR if len(color) == 4 else ATTR_RGB_COLOR


def _effect_tasks() -> list[asyncio.Task[Any]]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("aioddp-effect-") and not t.done()]


async def _turn_on(hass: HomeAssistant, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)


async def _turn_off(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True)


async def _set_pattern(hass: HomeAssistant, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(DOMAIN, "set_pattern", {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)


def _blocks(colors: list[tuple[int, ...]], block_size: int, pixels: int) -> bytes:
    """The raw RGB (or, for RGBW colors, RGBW) bytes of ``colors`` repeated in blocks of ``block_size``."""
    pattern = b"".join(bytes(color) * block_size for color in colors)
    return (pattern * pixels)[: pixels * len(colors[0])]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("data_type", "options", "cap", "order", "color", "sent"),
    [
        ("rgb", {}, 0.6, "RGB", (255, 100, 0), (255, 100, 0)),
        (
            "rgb",
            {"brightness_cap": 40, "color_order": "GRB", "fps": 30, "keepalive": 0},
            0.4,
            "GRB",
            (255, 100, 0),
            (255, 100, 0),
        ),
        ("rgbw", {"color_order": "GRBW"}, 0.6, "GRBW", (255, 100, 0, 50), (255, 100, 0, 50)),  # W reaches the wire
        ("rgbw", {}, 0.6, "RGBW", None, WHITE_W),  # a bare turn_on: white on the W channel only
    ],
    ids=["defaults", "options", "rgbw", "rgbw-default-white"],
)
async def test_solid_on_sends_color_through_pipeline(
    hass: HomeAssistant,
    udp_listener: Listener,
    entry_factory: Callable[..., MockConfigEntry],
    data_type: str,
    options: dict[str, Any],
    cap: float,
    order: str,
    color: tuple[int, ...] | None,
    sent: tuple[int, ...],
) -> None:
    entry = entry_factory(options=options, data_type=data_type)
    await _setup(hass, entry)

    await _turn_on(hass, OUT2, brightness=128, **({_color_attr(color): color} if color else {}))

    channels = len(sent)
    packet = await udp_listener.wait_for(lambda p: p.offset == 80 * channels)
    strip = Strip(
        "127.0.0.1",
        122,
        port=udp_listener.port,
        data_type=DataType.RGBW if channels == 4 else DataType.RGB,
        color_order=order,
        brightness_cap=cap,
    )
    assert packet.push
    assert packet.data_type == strip.wire_data_type
    assert packet.data == process_frame(bytes(sent) * 42, strip, brightness=128 / 255)

    state = hass.states.get(OUT2)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128
    assert state.attributes[_color_attr(sent)] == sent
    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.RGBW if channels == 4 else ColorMode.RGB]
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


@pytest.mark.parametrize("color", [BLUE, (0, 0, 255, 200)], ids=["rgb", "rgbw"])
async def test_effect_runs_updates_and_stops_on_solid(
    hass: HomeAssistant, udp_listener: Listener, entry_factory: Callable[..., MockConfigEntry], color: tuple[int, ...]
) -> None:
    entry = entry_factory(data_type="rgbw" if len(color) == 4 else "rgb")
    await _setup(hass, entry)
    runtime = entry.runtime_data
    # chase renders RGB only: on an RGBW light it takes the color's first three channels and ignores W.
    await _turn_on(hass, OUT1, effect="chase", brightness=255, **{_color_attr(color): color})
    runner = runtime.runners["output1"]
    assert runner.running
    assert [t.get_name() for t in _effect_tasks()] == ["aioddp-effect-chase-output1"]
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
    solid = process_frame(bytes(color) * 80, runtime.sender.strip, brightness=50 / 255)
    assert udp_listener.packets[-1].data == solid  # the solid look carries W again

    udp_listener.packets.clear()
    await udp_listener.settle()
    assert udp_listener.packets == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(("first", "second"), [(GREEN, RED), ((0, 255, 0, 40), (255, 0, 0, 255))], ids=["rgb", "rgbw"])
async def test_restore_resends_last_state(
    hass: HomeAssistant,
    udp_listener: Listener,
    entry_factory: Callable[..., MockConfigEntry],
    first: tuple[int, ...],
    second: tuple[int, ...],
) -> None:
    attr = _color_attr(first)
    mock_restore_cache(
        hass,
        [
            State(OUT1, STATE_ON, {ATTR_BRIGHTNESS: 100, attr: list(first), ATTR_EFFECT: EFFECT_OFF}),
            State(OUT2, STATE_ON, {ATTR_BRIGHTNESS: 200, attr: list(second), ATTR_EFFECT: "rainbow"}),
        ],
    )
    entry = entry_factory(data_type="rgbw" if len(first) == 4 else "rgb")
    await _setup(hass, entry)

    packet = await udp_listener.wait_for(lambda p: p.offset == 0)
    assert packet.data == process_frame(bytes(first) * 80, entry.runtime_data.sender.strip, brightness=100 / 255)
    assert entry.runtime_data.runners["output2"].running
    await udp_listener.wait_for(lambda p: p.offset == 80 * len(first))

    out1, out2 = hass.states.get(OUT1), hass.states.get(OUT2)
    assert out1 is not None and out2 is not None
    assert (out1.state, out1.attributes[ATTR_BRIGHTNESS], out1.attributes[attr]) == (STATE_ON, 100, first)
    assert (out2.state, out2.attributes[ATTR_EFFECT], out2.attributes[attr]) == (STATE_ON, "rainbow", second)
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
            (
                State(OUT2, STATE_OFF, off),
                {
                    "brightness": 200,
                    "rgb_color": [255, 0, 0],
                    "effect": "palette",
                    "pattern": {"colors": [[255, 0, 0], [0, 0, 255]], "block_size": 2, "params": {}},
                },
            ),
        ],
    )
    entry = entry_factory()
    await _setup(hass, entry)
    await udp_listener.settle()
    assert udp_listener.packets == []

    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: [OUT1, OUT2]}, blocking=True)

    strip = entry.runtime_data.sender.strip
    packet = await udp_listener.wait_for(lambda p: p.offset == 0)
    assert packet.data == process_frame(bytes((0, 255, 0)) * 80, strip, brightness=100 / 255)
    packet = await udp_listener.wait_for(lambda p: p.offset == OUT2_OFFSET)
    assert packet.data == process_frame(_blocks([RED, BLUE], 2, 42), strip, brightness=200 / 255)
    out2 = hass.states.get(OUT2)
    assert out2 is not None
    assert (out2.attributes[ATTR_BRIGHTNESS], out2.attributes[ATTR_RGB_COLOR], out2.attributes[ATTR_EFFECT]) == (
        200,
        (255, 0, 0),
        "palette",
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


@pytest.mark.parametrize(
    ("color", "extra", "send_fails", "logged"),
    [
        ([1, 2], {}, False, None),  # two components: not an RGB color
        ([255, 0, 0], {}, True, "Could not restore"),
        ([255, 0, 0], {"effect": "palette", "pattern": {"colors": [[1, 2]]}}, False, None),
        (
            [255, 0, 0],
            {"effect": "palette", "pattern": {"colors": [[1, 2, 3]], "block_size": 0, "params": {}}},
            False,
            "Dropping the stored pattern",
        ),
        (  # stored while the entry was RGBW: white cannot reach 3-channel pixels
            [255, 0, 0],
            {"effect": "palette", "pattern": {"colors": [[1, 2, 3, 4]], "block_size": 1, "params": {}}},
            False,
            "Dropping the stored pattern",
        ),
    ],
    ids=["malformed-color", "send-fails", "malformed-pattern", "invalid-pattern", "rgbw-pattern-on-rgb-entry"],
)
async def test_restore_tolerates_bad_data(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    udp_listener: Listener,
    entry_factory: Callable[..., MockConfigEntry],
    color: list[int],
    extra: dict[str, Any],
    send_fails: bool,
    logged: str | None,
) -> None:
    attributes = {ATTR_BRIGHTNESS: 100, ATTR_RGB_COLOR: color}
    mock_restore_cache_with_extra_data(hass, [(State(OUT1, STATE_ON, attributes), extra)])
    entry = entry_factory()
    with (
        patch("custom_components.ddp.DDPSender.fill", side_effect=DDPSendError("boom")) if send_fails else nullcontext()
    ):
        await _setup(hass, entry)

    state = hass.states.get(OUT1)
    assert state is not None
    assert state.state == (STATE_OFF if send_fails else STATE_ON)  # a light that cannot be restored stays off
    assert ("Could not restore" in caplog.text) == send_fails
    assert logged is None or logged in caplog.text
    if color == [1, 2]:
        assert state.attributes[ATTR_RGB_COLOR] == (255, 255, 255)  # the default, not the malformed color
    if extra:  # a pattern that cannot be used is dropped: the effect shows the light's one color
        packet = await udp_listener.wait_for(lambda p: p.offset == 0)
        assert packet.data == process_frame(bytes(RED) * 80, entry.runtime_data.sender.strip, brightness=100 / 255)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("colors", "solid"),
    [([RED, GREEN], BLUE), ([RED, WHITE_W], (0, 0, 255, 80))],  # RGBW: an RGB color in the pattern gets W=0
    ids=["rgb", "rgbw"],
)
@pytest.mark.parametrize(("entity_id", "pixels"), [(OUT1, 80), (ALL, 122)], ids=["output", "all-outputs"])
async def test_set_pattern_sends_blocks_until_a_color_replaces_it(
    hass: HomeAssistant,
    udp_listener: Listener,
    entry_factory: Callable[..., MockConfigEntry],
    entity_id: str,
    pixels: int,
    colors: list[tuple[int, ...]],
    solid: tuple[int, ...],
) -> None:
    entry = entry_factory(data_type="rgbw" if len(solid) == 4 else "rgb")
    await _setup(hass, entry)
    strip = entry.runtime_data.sender.strip
    await _set_pattern(hass, entity_id, colors=[list(color) for color in colors], brightness=128)

    # Every other pixel; on All outputs one frame runs on across both outputs.
    packet = await udp_listener.wait_for(lambda p: p.offset == 0)
    padded = [color + (0,) * (len(solid) - len(color)) for color in colors]
    assert packet.data == process_frame(_blocks(padded, 1, pixels), strip, brightness=128 / 255)
    state = hass.states.get(entity_id)
    assert state is not None
    assert (state.state, state.attributes[ATTR_EFFECT], state.attributes[ATTR_BRIGHTNESS]) == (STATE_ON, "palette", 128)

    # A color without an effect change replaces the pattern with that one color.
    udp_listener.packets.clear()
    await _turn_on(hass, entity_id, **{_color_attr(solid): solid})
    frame = process_frame(bytes(solid) * pixels, strip, brightness=128 / 255)
    await udp_listener.wait_for(lambda p: p.offset == 0 and p.data == frame)
    assert hass.states.get(entity_id).attributes[ATTR_EFFECT] == "palette"  # type: ignore[union-attr]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("data_type", "data", "translation_key"),
    [
        ("rgb", {"params": {"speed": 4}}, "invalid_effect_params"),  # palette does not move
        ("rgb", {"effect": "chase", "params": {"speed": 500}}, "invalid_effect_params"),
        ("rgb", {"effect": "chase", "block_size": 2}, "invalid_effect_params"),  # chase has no blocks
        ("rgb", {"effect": "rainbow"}, "effect_without_colors"),
        ("rgb", {"effect": "strobe"}, "unknown_effect"),
        ("rgb", {"colors": [[1, 2, 3, 4]]}, "rgbw_needs_rgbw_pixels"),  # W on 3-channel pixels
        ("rgbw", {"colors": [[1, 2, 3], [1, 2, 3, 4]], "effect": "chase"}, "rgbw_effect_unsupported"),
    ],
    ids=[
        "unknown-param",
        "out-of-range",
        "no-block-size",
        "no-colors",
        "unknown-effect",
        "rgbw-on-rgb-entry",
        "rgbw-on-rgb-effect",
    ],
)
async def test_set_pattern_rejects_bad_patterns(
    hass: HomeAssistant,
    udp_listener: Listener,
    entry_factory: Callable[..., MockConfigEntry],
    data_type: str,
    data: dict[str, Any],
    translation_key: str,
) -> None:
    entry = entry_factory(data_type=data_type)
    await _setup(hass, entry)
    with pytest.raises(ServiceValidationError) as raised:
        await _set_pattern(hass, OUT1, **{"colors": [[1, 2, 3]], **data})
    assert raised.value.translation_key == translation_key
    assert hass.states.get(OUT1).state == STATE_OFF  # type: ignore[union-attr]
    await udp_listener.settle()
    assert udp_listener.packets == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("segments", [SEGMENTS, SEGMENTS[:1]], ids=["two-outputs", "one-output"])
async def test_all_outputs_light_only_for_several_outputs(
    hass: HomeAssistant,
    udp_listener: Listener,
    entry_factory: Callable[..., MockConfigEntry],
    segments: list[dict[str, Any]],
) -> None:
    entry = entry_factory(segments=segments)
    await _setup(hass, entry)

    state = hass.states.get(ALL)
    if len(segments) == 1:
        assert state is None
    else:
        assert state is not None
        assert (state.state, state.name) == (STATE_OFF, "NDB-Pro All outputs")
        assert er.async_get(hass).async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{entry.entry_id}_strip") == ALL
    await udp_listener.settle()
    assert udp_listener.packets == []  # setup sends nothing
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("before", "taker"), [([OUT1, OUT2], ALL), ([ALL], OUT1)], ids=["all-outputs-takes", "an-output-takes"]
)
async def test_all_outputs_and_the_outputs_take_the_pixels_from_each_other(
    hass: HomeAssistant, udp_listener: Listener, setup_entry: MockConfigEntry, before: list[str], taker: str
) -> None:
    runtime = setup_entry.runtime_data
    for entity_id in before:
        await _turn_on(hass, entity_id, effect="chase")
    assert len(runtime.runners) == len(before)
    await udp_listener.settle()
    udp_listener.packets.clear()

    await _turn_on(hass, taker, rgb_color=BLUE)
    await udp_listener.settle()

    assert [hass.states.get(e).state for e in before] == [STATE_OFF] * len(before)  # type: ignore[union-attr]
    assert hass.states.get(taker).state == STATE_ON  # type: ignore[union-attr]
    assert list(runtime.runners) == [] and _effect_tasks() == []  # the taker is solid; the chases stopped
    # Handed back, not blanked: no black frame, the taker's color last, and no chase frame after it.
    pixels = 122 if taker == ALL else 80
    assert not any(p.data == bytes(len(p.data)) for p in udp_listener.packets)
    last = udp_listener.packets[-1]
    assert (last.offset, last.data) == (0, process_frame(bytes(BLUE) * pixels, runtime.sender.strip))
    udp_listener.packets.clear()
    await udp_listener.settle()
    assert udp_listener.packets == []
