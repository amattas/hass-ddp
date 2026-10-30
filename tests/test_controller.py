"""The controller "Scenes" light: built-in effects over the fake HTTP UI, and handing the pixels back from DDP."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    ColorMode,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pyddp import DDPSendError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.ddp import DDPRuntime
from custom_components.ddp.const import DOMAIN

from .conftest import FakeController, Listener

SCENES = "light.ndb_pro_scenes"
OUT1 = "light.ndb_pro_output_1"
OUT2 = "light.ndb_pro_output_2"
FULL_STRIP = 122 * 3  # a keepalive resends the whole strip from offset 0
EFFECTS = [
    "Test",
    "Show1",
    "Show2",
    "Bands",
    "Bars",
    "Circles",
    "Color Change",
    "Color Wave",
    "Fixed Colors",
    "Glow",
    "Paint",
    "Ping Pong",
    "Pulsate",
    "Shift",
    "Snow",
    "Twist",
    "Worms",
]


def _control(body: str) -> tuple[str, str, bytes]:
    return ("POST", "/api/control", body.encode())


async def _call(hass: HomeAssistant, service: str, entity_id: str, domain: str = LIGHT_DOMAIN, **data: Any) -> None:
    await hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _poll(entry: MockConfigEntry) -> None:
    """One poll of the controller, as the 30 s timer does."""
    await entry.runtime_data.scenes.async_refresh()


def _state(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None
    return state


@pytest.fixture
async def ndb_entry(
    hass: HomeAssistant, fake_controller: FakeController, entry_factory: Callable[..., MockConfigEntry]
) -> AsyncIterator[MockConfigEntry]:
    """A loaded NDB-Pro entry against the fake controller; unloaded at teardown."""
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("controller_type", ["ndbpro", "generic"])
async def test_scenes_light_only_for_vendor_types(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    controller_type: str,
) -> None:
    # The stored type decides; a reachable NDB-Pro under a Generic entry is never probed.
    entry = entry_factory(controller=fake_controller, controller_type=controller_type)
    await _setup(hass, entry)

    if controller_type == "generic":
        assert hass.states.get(SCENES) is None
        assert entry.runtime_data.controller is None
        assert fake_controller.requests == []
    else:
        state = _state(hass, SCENES)
        assert state.state == STATE_ON
        assert state.attributes[ATTR_EFFECT] == "Glow"  # the fixture page's selected effect
        assert state.attributes[ATTR_EFFECT_LIST] == EFFECTS
        assert state.attributes[ATTR_BRIGHTNESS] == 255  # intensity 100
        assert state.attributes[ATTR_COLOR_MODE] == ColorMode.BRIGHTNESS
        registered = entity_registry.async_get(SCENES)
        assert registered is not None
        assert registered.unique_id == f"{entry.entry_id}_controller"
        assert registered.device_id == entity_registry.async_get(OUT1).device_id  # type: ignore[union-attr]
        assert fake_controller.commands() == []  # setup only reads
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("restored", "data", "commands", "effect", "brightness"),
    [
        (None, {ATTR_EFFECT: "Show1", ATTR_BRIGHTNESS: 128}, ['{"int":"50"}', '{"fx":"Show1"}'], "Show1", 128),
        ("Show2", {}, ['{"fx":"Show2"}'], "Show2", 255),
        (None, {}, ['{"fx":"Test"}'], "Test", 255),
        ("Show2", {ATTR_BRIGHTNESS: 1}, ['{"int":"1"}', '{"fx":"Show2"}'], "Show2", 3),
    ],
    ids=["effect-and-brightness", "restored-effect", "first-effect", "restored-min-brightness"],
)
async def test_turn_on_selects_effect_and_intensity(
    hass: HomeAssistant,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    restored: str | None,
    data: dict[str, Any],
    commands: list[str],
    effect: str,
    brightness: int,
) -> None:
    if restored is not None:
        mock_restore_cache_with_extra_data(hass, [(State(SCENES, STATE_OFF), {"last_effect": restored})])
    fake_controller.select("Off")  # restart finds the controller dark: a bare turn_on needs a remembered scene
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)
    assert _state(hass, SCENES).state == STATE_OFF
    assert fake_controller.commands() == []  # restoring never commands the controller

    await _call(hass, SERVICE_TURN_ON, SCENES, **data)

    assert fake_controller.commands() == [_control(body) for body in commands]
    for _ in range(2):  # the command's assumed state, then the poll reading it back
        state = _state(hass, SCENES)
        assert (state.state, state.attributes[ATTR_EFFECT], state.attributes[ATTR_BRIGHTNESS]) == (
            STATE_ON,
            effect,
            brightness,
        )
        await _poll(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_turn_off_selects_off_and_blanks_segments(
    hass: HomeAssistant, udp_listener: Listener, fake_controller: FakeController, ndb_entry: MockConfigEntry
) -> None:
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(255, 0, 0))
    await udp_listener.wait_for(lambda p: p.offset == 0)
    udp_listener.packets.clear()

    await _call(hass, SERVICE_TURN_OFF, SCENES)

    assert fake_controller.commands() == [_control('{"fx":"Off"}')]
    await udp_listener.wait_for(lambda p: p.offset == 0 and p.data == bytes(80 * 3))
    assert _state(hass, SCENES).state == STATE_OFF
    assert _state(hass, OUT1).state == STATE_OFF
    await _poll(ndb_entry)
    assert _state(hass, SCENES).state == STATE_OFF  # the controller reports "Off"; it is not an effect


async def _turn_on_scene(hass: HomeAssistant) -> None:
    await _call(hass, SERVICE_TURN_ON, SCENES, effect="Show1")


async def _recall_preset(hass: HomeAssistant) -> None:
    await _call(hass, "recall_preset", SCENES, domain=DOMAIN, preset=3)


@pytest.mark.parametrize(
    ("action", "command"),
    [(_turn_on_scene, _control('{"fx":"Show1"}')), (_recall_preset, ("GET", "/pshows.html?r=2", b""))],
    ids=["turn_on", "recall_preset"],
)
async def test_scene_takes_the_pixels_back_from_ddp(
    hass: HomeAssistant,
    udp_listener: Listener,
    fake_controller: FakeController,
    ndb_entry: MockConfigEntry,
    action: Callable[[HomeAssistant], Awaitable[None]],
    command: tuple[str, str, bytes],
) -> None:
    runtime = ndb_entry.runtime_data
    await _call(hass, SERVICE_TURN_ON, OUT1, effect="chase")
    await _call(hass, SERVICE_TURN_ON, OUT2, rgb_color=(0, 0, 255))
    await udp_listener.wait_for(lambda p: p.offset == 80 * 3)
    fake_controller.requests.clear()

    await action(hass)
    await udp_listener.settle()
    udp_listener.packets.clear()

    assert fake_controller.commands() == [command]
    if command[0] == "GET":  # a recalled preset's effect is unknown until the controller is read again
        assert fake_controller.requests[-2:] == [("GET", "/pfx2.html", b""), ("GET", "/pcolors.html", b"")]
    assert runtime.runners == {}
    assert [t for t in asyncio.all_tasks() if t.get_name().startswith("pyddp-effect-")] == []
    assert _state(hass, OUT1).state == STATE_OFF
    assert _state(hass, OUT2).state == STATE_OFF
    assert _state(hass, SCENES).state == STATE_ON
    # Neither a black frame nor a keepalive (default 1 s) follows: the controller's own scene takes over.
    await udp_listener.settle(2.2)
    assert udp_listener.packets == []


async def test_segment_turned_on_during_hand_back_keeps_its_keepalive(
    hass: HomeAssistant, udp_listener: Listener, ndb_entry: MockConfigEntry
) -> None:
    # An HA scene holding both lights applies them concurrently, so OUT1 can turn on while the hand-back is
    # stopping OUT2's runner (after it already handed OUT1 back). Force exactly that interleaving.
    await _call(hass, SERVICE_TURN_ON, OUT2, effect="chase")
    stop_runner = DDPRuntime.stop_runner

    async def stop_then_turn_on_out1(self: DDPRuntime, key: str) -> None:
        await stop_runner(self, key)
        if key == "output2":
            await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(0, 255, 0))

    with patch.object(DDPRuntime, "stop_runner", stop_then_turn_on_out1):
        await _call(hass, SERVICE_TURN_ON, SCENES, effect="Show1")

    assert _state(hass, OUT1).state == STATE_ON
    udp_listener.packets.clear()
    await udp_listener.wait_for(lambda p: p.offset == 0 and len(p.data) == FULL_STRIP, timeout=2.5)


@pytest.mark.parametrize("service", [SERVICE_TURN_OFF, SERVICE_TURN_ON])
async def test_poll_in_flight_cannot_undo_a_command(
    hass: HomeAssistant, fake_controller: FakeController, ndb_entry: MockConfigEntry, service: str
) -> None:
    # Live, a scheduled turn_off landed while the 30 s poll was reading the old page; the stale read won.
    data = {ATTR_EFFECT: "Snow"} if service == SERVICE_TURN_ON else {}
    fake_controller.page_delay = 0.2
    poll = hass.async_create_task(_poll(ndb_entry))
    await asyncio.sleep(0.05)
    fake_controller.page_delay = 0.0
    await _call(hass, service, SCENES, **data)
    await poll
    await hass.async_block_till_done()

    state = _state(hass, SCENES)
    expected = (STATE_OFF, None) if service == SERVICE_TURN_OFF else (STATE_ON, "Snow")
    assert (state.state, state.attributes.get(ATTR_EFFECT)) == expected


async def test_controller_down_at_setup_recovers_on_poll(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
) -> None:
    fake_controller.offline = True
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert _state(hass, SCENES).state == STATE_UNAVAILABLE
    assert _state(hass, OUT1).state == STATE_OFF  # the DDP lights don't depend on the HTTP UI

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)  # polls run as background tasks
    assert _state(hass, SCENES).state == STATE_UNAVAILABLE
    unavailable = [
        r for r in caplog.records if r.levelno >= logging.WARNING and r.name.startswith("custom_components.ddp")
    ]
    assert len(unavailable) == 1  # logged once, not on every poll
    caplog.clear()

    fake_controller.offline = False
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=62))
    await hass.async_block_till_done(wait_background_tasks=True)
    state = _state(hass, SCENES)
    assert (state.state, state.attributes[ATTR_EFFECT], state.attributes[ATTR_EFFECT_LIST]) == (
        STATE_ON,
        "Glow",
        EFFECTS,
    )
    assert any("recovered" in r.getMessage() for r in caplog.records)  # logged once when it is back
    assert await hass.config_entries.async_unload(entry.entry_id)


async def _strobe(hass: HomeAssistant) -> None:
    await _call(hass, SERVICE_TURN_ON, SCENES, effect="Strobe")


async def _off(hass: HomeAssistant) -> None:
    await _call(hass, SERVICE_TURN_OFF, SCENES)


async def _recall_on_segment(hass: HomeAssistant) -> None:
    await _call(hass, "recall_preset", OUT1, domain=DOMAIN, preset=1)


@pytest.mark.parametrize(
    ("action", "offline", "segment_fails", "error", "out1_after"),
    [
        (_strobe, False, False, ServiceValidationError, STATE_ON),
        (_recall_on_segment, False, False, ServiceValidationError, STATE_ON),
        # A controller that never took the command must not cost the DDP look.
        (_turn_on_scene, True, False, HomeAssistantError, STATE_ON),
        (_recall_preset, True, False, HomeAssistantError, STATE_ON),
        (_off, True, False, HomeAssistantError, STATE_OFF),
        # A segment that cannot be blanked must not stop the controller's "Off".
        (_off, False, True, HomeAssistantError, STATE_ON),
    ],
    ids=["unknown-effect", "recall-on-segment", "turn_on-down", "recall-down", "turn_off-down", "segment-send-fails"],
)
async def test_errors(
    hass: HomeAssistant,
    fake_controller: FakeController,
    ndb_entry: MockConfigEntry,
    action: Callable[[HomeAssistant], Awaitable[None]],
    offline: bool,
    segment_fails: bool,
    error: type[Exception],
    out1_after: str,
) -> None:
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(0, 255, 0))
    fake_controller.requests.clear()
    fake_controller.offline = offline
    with (
        (
            patch.object(ndb_entry.runtime_data.sender, "off", side_effect=DDPSendError("boom"))
            if segment_fails
            else contextlib.nullcontext()
        ),
        pytest.raises(error) as raised,
    ):
        await action(hass)
    if error is ServiceValidationError:
        assert fake_controller.requests == []
    else:
        assert not isinstance(raised.value, ServiceValidationError)
    if segment_fails:
        assert fake_controller.commands() == [_control('{"fx":"Off"}')]
    assert _state(hass, OUT1).state == out1_after
