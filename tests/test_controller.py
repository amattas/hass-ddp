"""The controller device (light, Power, Scene, Color 1-5): built-in effects and palette over the fake HTTP UI, and
handing the pixels back from DDP."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from aioddp import DDPSendError, process_frame
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_RGB_COLOR,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    ColorMode,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pyminleon import ControllerError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.ddp import DDPRuntime
from custom_components.ddp.const import DOMAIN, LightSource
from custom_components.ddp.controller import detect, fetch_layout
from custom_components.ddp.coordinator import SCAN_INTERVAL

from .conftest import FakeController, Listener

SCENES = "light.ndb_pro_minleon_ndb_pro"  # the controller device "NDB-Pro Minleon NDB-Pro"'s main entity
POWER = "switch.ndb_pro_minleon_ndb_pro_power"
SCENE = "select.ndb_pro_minleon_ndb_pro_scene"
COLORS = [f"light.ndb_pro_minleon_ndb_pro_color_{slot}" for slot in range(1, 6)]
OUT1 = "light.ndb_pro_generic_ddp_output_1"  # the DDP device of a vendor entry is "NDB-Pro Generic DDP"
OUT2 = "light.ndb_pro_generic_ddp_output_2"
ALL = "light.ndb_pro_generic_ddp_all_outputs"
OUT2_OFFSET = 80 * 3
BLACK_OUT2 = bytes(42 * 3)
DDP_LIGHTS = [OUT1, OUT2, ALL]
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
EMPTY = [0, 0, 0]
FIXTURE_COLORS = [[0xBE, 0x38, 0xF3], EMPTY, EMPTY, EMPTY, EMPTY]  # /pcolors.html: bc1 is #be38f3, bc2-bc6 empty


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
async def test_devices_and_names(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    controller_type: str,
) -> None:
    # The stored type decides; a reachable NDB-Pro under a Generic entry is never probed.
    entry = entry_factory(controller=fake_controller, controller_type=controller_type)
    await _setup(hass, entry)

    ddp_device = device_registry.async_get_device_by_identifier((DOMAIN, "127.0.0.1"), entry.entry_id)
    controller_device = device_registry.async_get_device_by_identifier((DOMAIN, "127.0.0.1:controller"), entry.entry_id)
    assert ddp_device is not None
    assert (ddp_device.manufacturer, ddp_device.model) == ("Minleon", "NDBPRO")
    if controller_type == "generic":
        # One device, named after the entry; no controller entities.
        assert ddp_device.name == "NDB-Pro"
        assert [_state(hass, e).name for e in ["light.ndb_pro_output_1", "light.ndb_pro_all_outputs"]] == [
            "NDB-Pro Output 1",
            "NDB-Pro All outputs",
        ]
        assert controller_device is None
        assert {e.domain for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)} == {"light"}
        assert len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == 3
        assert entry.runtime_data.controller is None
        assert fake_controller.requests == []
        assert await hass.config_entries.async_unload(entry.entry_id)
        return
    assert ddp_device.name == "NDB-Pro Generic DDP"
    assert controller_device is not None
    assert (
        controller_device.name,
        controller_device.manufacturer,
        controller_device.model,
        controller_device.sw_version,
        controller_device.via_device_id,
    ) == ("NDB-Pro Minleon NDB-Pro", "Minleon", "NDBPRO", "1.2.3", ddp_device.id)
    names = {
        OUT1: ("NDB-Pro Generic DDP Output 1", "output1", ddp_device),
        ALL: ("NDB-Pro Generic DDP All outputs", "strip", ddp_device),
        SCENES: ("NDB-Pro Minleon NDB-Pro", "controller", controller_device),
        POWER: ("NDB-Pro Minleon NDB-Pro Power", "power", controller_device),
        SCENE: ("NDB-Pro Minleon NDB-Pro Scene", "scene", controller_device),
        **{e: (f"NDB-Pro Minleon NDB-Pro Color {n}", f"color_{n}", controller_device) for n, e in enumerate(COLORS, 1)},
    }
    for entity_id, (name, key, device) in names.items():
        registered = entity_registry.async_get(entity_id)
        assert registered is not None, entity_id
        assert (_state(hass, entity_id).name, registered.unique_id, registered.device_id) == (
            name,
            f"{entry.entry_id}_{key}",
            device.id,
        )
    state = _state(hass, SCENES)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_EFFECT] == "Glow"  # the fixture page's selected effect
    assert state.attributes[ATTR_EFFECT_LIST] == EFFECTS
    assert state.attributes[ATTR_BRIGHTNESS] == 255  # intensity 100
    assert state.attributes[ATTR_COLOR_MODE] == ColorMode.BRIGHTNESS
    assert (state.attributes["colors"], state.attributes["background"]) == (FIXTURE_COLORS, EMPTY)
    assert _state(hass, POWER).state == STATE_ON
    scene = _state(hass, SCENE)
    assert (scene.state, scene.attributes["options"]) == ("Glow", EFFECTS)
    assert [_state(hass, e).state for e in COLORS] == [STATE_ON] + [STATE_OFF] * 4  # bc1 #be38f3, the rest empty
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


@pytest.mark.parametrize(("entity_id", "pixels"), [(OUT1, 80), (ALL, 122)], ids=["output", "all-outputs"])
async def test_turn_off_selects_off_and_blanks_segments(
    hass: HomeAssistant,
    udp_listener: Listener,
    fake_controller: FakeController,
    ndb_entry: MockConfigEntry,
    entity_id: str,
    pixels: int,
) -> None:
    await _call(hass, SERVICE_TURN_ON, entity_id, rgb_color=(255, 0, 0))
    await udp_listener.wait_for(lambda p: p.offset == 0)
    udp_listener.packets.clear()

    await _call(hass, SERVICE_TURN_OFF, SCENES)

    assert fake_controller.commands() == [_control('{"fx":"Off"}')]
    await udp_listener.wait_for(lambda p: p.offset == 0 and p.data == bytes(pixels * 3))
    assert _state(hass, SCENES).state == STATE_OFF
    # The lit light was blanked; the others showed the scene and just go off.
    assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_OFF] * 3
    await _poll(ndb_entry)
    assert _state(hass, SCENES).state == STATE_OFF  # the controller reports "Off"; it is not an effect


def _color(slot: int, color: str) -> tuple[str, str, bytes]:
    return _control(f'{{"color":{{"i":{slot},"c":"{color}"}}}}')


@pytest.mark.parametrize(
    ("running", "restored", "domain", "service", "entity_id", "data", "body", "effect"),
    [
        ("Off", None, "switch", SERVICE_TURN_ON, POWER, {}, '{"fx":"Test"}', "Test"),  # no last scene: the first
        ("Off", "Show2", "switch", SERVICE_TURN_ON, POWER, {}, '{"fx":"Show2"}', "Show2"),  # the light's last scene
        ("Glow", None, "switch", SERVICE_TURN_OFF, POWER, {}, '{"fx":"Off"}', None),
        ("Off", None, "select", "select_option", SCENE, {"option": "Snow"}, '{"fx":"Snow"}', "Snow"),
        ("Glow", None, "select", "select_option", SCENE, {"option": "Snow"}, '{"fx":"Snow"}', "Snow"),
        ("Off", "Show2", LIGHT_DOMAIN, SERVICE_TURN_ON, SCENES, {}, '{"fx":"Show2"}', "Show2"),
        ("Glow", None, LIGHT_DOMAIN, SERVICE_TURN_OFF, SCENES, {}, '{"fx":"Off"}', None),
    ],
    ids=["power-on-first", "power-on-last", "power-off", "select-from-off", "select-other", "light-on", "light-off"],
)
async def test_power_and_scene_mirror_the_controller_light(
    hass: HomeAssistant,
    udp_listener: Listener,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    running: str,
    restored: str | None,
    domain: str,
    service: str,
    entity_id: str,
    data: dict[str, Any],
    body: str,
    effect: str | None,
) -> None:
    if restored is not None:
        mock_restore_cache_with_extra_data(hass, [(State(SCENES, STATE_OFF), {"last_effect": restored})])
    fake_controller.select(running)
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(255, 0, 0))  # DDP drives Output 1
    fake_controller.requests.clear()

    await _call(hass, service, entity_id, domain=domain, **data)

    assert fake_controller.commands() == [_control(body)]
    on = effect is not None
    for _ in range(2):  # the command's assumed state, then the poll reading it back
        light, power, scene = _state(hass, SCENES), _state(hass, POWER), _state(hass, SCENE)
        assert (light.state, light.attributes.get(ATTR_EFFECT)) == (STATE_ON if on else STATE_OFF, effect)
        assert power.state == (STATE_ON if on else STATE_OFF)
        assert scene.state == (effect or STATE_UNKNOWN)
        # A scene takes the pixels back from DDP (Output 1 shows it); the controller's Off blanks and shows it off.
        assert _state(hass, OUT1).state == (STATE_ON if on else STATE_OFF)
        assert entry.runtime_data.runners == {}
        await _poll(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("shown", "state", "effect", "rgb"),
    [
        ("#000000", STATE_OFF, None, None),
        ("#feffff", STATE_ON, "Cool White", (254, 255, 255)),
        ("#c69740", STATE_ON, "Warm White", (198, 151, 64)),  # what the colors page shows once Warm White is set
        ("#ffffff", STATE_ON, "Warm White", (255, 255, 255)),
        ("#123456", STATE_ON, None, (0x12, 0x34, 0x56)),
    ],
    ids=["empty", "cool-white", "warm-white-shown", "warm-white-sent", "color"],
)
async def test_color_light_reads_its_palette_slot(
    hass: HomeAssistant,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    shown: str,
    state: str,
    effect: str | None,
    rgb: tuple[int, int, int] | None,
) -> None:
    fake_controller.set_color(2, shown)
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)

    color = _state(hass, COLORS[1])
    assert color.state == state
    assert color.attributes[ATTR_EFFECT_LIST] == ["Cool White", "Warm White"]
    if state == STATE_ON:
        assert (color.attributes[ATTR_EFFECT], color.attributes[ATTR_RGB_COLOR]) == (effect, rgb)
        assert (color.attributes[ATTR_BRIGHTNESS], color.attributes[ATTR_COLOR_MODE]) == (255, ColorMode.RGB)
    assert _state(hass, COLORS[0]).attributes[ATTR_RGB_COLOR] == (0xBE, 0x38, 0xF3)  # its own slot only
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("shown", "restored", "service", "data", "sent", "state", "effect"),
    [
        ("#000000", None, SERVICE_TURN_ON, {ATTR_RGB_COLOR: (255, 0, 0)}, "#ff0000", STATE_ON, None),
        ("#000000", None, SERVICE_TURN_ON, {ATTR_EFFECT: "Cool White"}, "#feffff", STATE_ON, "Cool White"),
        ("#123456", None, SERVICE_TURN_ON, {ATTR_EFFECT: "Warm White"}, "#ffffff", STATE_ON, "Warm White"),
        (
            "#000000",
            None,
            SERVICE_TURN_ON,
            {ATTR_RGB_COLOR: (255, 0, 0), ATTR_BRIGHTNESS: 128},
            "#800000",
            STATE_ON,
            None,
        ),
        ("#123456", None, SERVICE_TURN_ON, {ATTR_BRIGHTNESS: 51}, "#040a11", STATE_ON, None),  # its own color, scaled
        ("#123456", None, SERVICE_TURN_OFF, {}, "#000000", STATE_OFF, None),
        ("#000000", [0, 0, 255], SERVICE_TURN_ON, {}, "#0000ff", STATE_ON, None),  # the restored last color
        ("#000000", None, SERVICE_TURN_ON, {}, "#ffffff", STATE_ON, "Warm White"),  # none: white
    ],
    ids=["rgb", "cool-white", "warm-white", "rgb-brightness", "brightness", "off", "bare-restored", "bare-default"],
)
async def test_color_light_sets_its_palette_slot(
    hass: HomeAssistant,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    shown: str,
    restored: list[int] | None,
    service: str,
    data: dict[str, Any],
    sent: str,
    state: str,
    effect: str | None,
) -> None:
    if restored is not None:
        mock_restore_cache_with_extra_data(hass, [(State(COLORS[1], STATE_OFF), {"last_color": restored})])
    fake_controller.set_color(2, shown)
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(0, 255, 0))
    fake_controller.requests.clear()

    await _call(hass, service, COLORS[1], **data)

    assert fake_controller.commands() == [_color(2, sent)]
    slot = [int(sent[i : i + 2], 16) for i in (1, 3, 5)]
    for _ in range(2):  # the command's published palette, then the poll reading the page back
        color = _state(hass, COLORS[1])
        assert (color.state, color.attributes.get(ATTR_EFFECT)) == (state, effect)
        if effect is None and state == STATE_ON:
            assert list(color.attributes[ATTR_RGB_COLOR]) == slot
        # The controller light's palette updates at once; the other slots and DDP are untouched.
        assert _state(hass, SCENES).attributes["colors"] == [FIXTURE_COLORS[0], slot, EMPTY, EMPTY, EMPTY]
        assert _state(hass, COLORS[0]).state == STATE_ON
        assert _state(hass, OUT1).state == STATE_ON
        await _poll(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("data", "sent", "colors", "background"),
    [
        (
            {"colors": ["warm_white", [255, 0, 0], "warm_white", [255, 0, 0]]},
            ["#ffffff", "#ff0000", "#ffffff", "#ff0000", "#000000"],
            [[255, 255, 255], [255, 0, 0], [255, 255, 255], [255, 0, 0], EMPTY],
            EMPTY,
        ),
        (
            {"colors": ["cool_white"], "background": [0, 0, 255]},
            ["#feffff", "#000000", "#000000", "#000000", "#000000", "#0000ff"],
            [[254, 255, 255], EMPTY, EMPTY, EMPTY, EMPTY],
            [0, 0, 255],
        ),
        ({"colors": []}, ["#000000"] * 5, [EMPTY] * 5, EMPTY),  # all empty: the controller picks random colors
    ],
    ids=["named-whites", "background", "clear"],
)
async def test_set_colors_sends_the_palette(
    hass: HomeAssistant,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    data: dict[str, Any],
    sent: list[str],
    colors: list[list[int]],
    background: list[int],
) -> None:
    fake_controller.select("Off")  # the controller light need not be on
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)

    await _call(hass, "set_colors", SCENES, domain=DOMAIN, **data)

    assert fake_controller.commands() == [_color(slot, color) for slot, color in enumerate(sent, start=1)]
    for _ in range(2):  # the command's assumed palette, then the poll reading the page back
        state = _state(hass, SCENES)
        assert (state.state, state.attributes["colors"], state.attributes["background"]) == (
            STATE_OFF,
            colors,
            background,
        )
        await _poll(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "data",
    [
        {"colors": [[1, 2, 3]] * 6},
        {"colors": ["blue"]},
        {"colors": [[256, 0, 0]]},
        {"colors": [[1, 2]]},
        {"colors": [], "background": "warm_white"},
    ],
    ids=["six-colors", "unknown-name", "out-of-range", "two-components", "named-background"],
)
async def test_set_colors_rejects_bad_input(
    hass: HomeAssistant, fake_controller: FakeController, ndb_entry: MockConfigEntry, data: dict[str, Any]
) -> None:
    with pytest.raises(probatio.Invalid):
        await _call(hass, "set_colors", SCENES, domain=DOMAIN, **data)
    assert fake_controller.commands() == []


async def _turn_on_scene(hass: HomeAssistant) -> None:
    await _call(hass, SERVICE_TURN_ON, SCENES, effect="Show1")


async def _recall_preset(hass: HomeAssistant) -> None:
    await _call(hass, "recall_preset", SCENES, domain=DOMAIN, preset=3)


def _external_scene(fake_controller: FakeController) -> Callable[[HomeAssistant], Awaitable[None]]:
    """Select a scene in the controller's own UI, then let the 30 s poll see it."""

    async def change(hass: HomeAssistant) -> None:
        fake_controller.select("Show1")
        async_fire_time_changed(hass, dt_util.utcnow() + SCAN_INTERVAL + timedelta(seconds=1))
        await hass.async_block_till_done(wait_background_tasks=True)

    return change


@pytest.mark.parametrize(
    ("action", "command"),
    [
        (_turn_on_scene, _control('{"fx":"Show1"}')),
        (_recall_preset, ("GET", "/pshows.html?r=2", b"")),
        (None, None),  # selected in the controller's own UI and seen by a poll
    ],
    ids=["turn_on", "recall_preset", "polled"],
)
@pytest.mark.parametrize(
    "lit",
    [[(OUT1, {ATTR_EFFECT: "chase"}), (OUT2, {"rgb_color": (0, 0, 255)})], [(ALL, {ATTR_EFFECT: "chase"})]],
    ids=["outputs", "all-outputs"],
)
async def test_scene_takes_the_pixels_back_from_ddp(
    hass: HomeAssistant,
    udp_listener: Listener,
    fake_controller: FakeController,
    ndb_entry: MockConfigEntry,
    action: Callable[[HomeAssistant], Awaitable[None]] | None,
    command: tuple[str, str, bytes] | None,
    lit: list[tuple[str, dict[str, Any]]],
) -> None:
    runtime = ndb_entry.runtime_data
    action = action or _external_scene(fake_controller)
    for entity_id, data in lit:
        await _call(hass, SERVICE_TURN_ON, entity_id, **data)
    await udp_listener.wait_for(lambda p: p.offset == 0)
    fake_controller.requests.clear()

    await action(hass)
    await udp_listener.settle()
    udp_listener.packets.clear()

    assert fake_controller.commands() == ([] if command is None else [command])
    if (
        command is not None and command[0] == "GET"
    ):  # a recalled preset's effect is unknown until the controller is read again
        assert fake_controller.requests[-3:] == [
            ("GET", f"/{page}.html", b"") for page in ("pfx2", "pcolors", "pcolors")
        ]
    assert runtime.runners == {}
    assert [t for t in asyncio.all_tasks() if t.get_name().startswith("aioddp-effect-")] == []
    # Every DDP light shows the scene, the ones DDP drove included.
    assert [_state(hass, entity_id).state for entity_id in DDP_LIGHTS] == [STATE_ON] * 3
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
    assert _state(hass, OUT2).state == STATE_ON  # showing the scene
    udp_listener.packets.clear()
    packet = await udp_listener.wait_for(lambda p: p.offset == 0, timeout=2.5)
    assert len(packet.data) == 80 * 3  # keepalive resends OUT1's range only; OUT2's pixels are the scene's
    assert all(p.offset == 0 for p in udp_listener.packets)


@pytest.fixture
async def fast_keepalive_entry(
    hass: HomeAssistant, fake_controller: FakeController, entry_factory: Callable[..., MockConfigEntry]
) -> AsyncIterator[MockConfigEntry]:
    """An NDB-Pro entry whose keepalive fires every 0.5 s (the minimum), so a test can watch which pixels it holds."""
    entry = entry_factory(controller=fake_controller, options={"keepalive": 0.5})
    await _setup(hass, entry)
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def _keepalive_ranges(udp_listener: Listener) -> set[tuple[int, int]]:
    """The (byte offset, byte length) ranges keepalive resends over the next 1.2 s."""
    await udp_listener.settle()
    udp_listener.packets.clear()
    await udp_listener.settle(1.2)
    return {(p.offset, len(p.data)) for p in udp_listener.packets}


@pytest.mark.parametrize("takers", [DDP_LIGHTS[::-1], [OUT1, OUT2]], ids=["all-outputs-first", "outputs-only"])
async def test_an_output_turned_off_during_a_scene_holds_black_on_its_pixels_only(
    hass: HomeAssistant,
    udp_listener: Listener,
    fake_controller: FakeController,
    fast_keepalive_entry: MockConfigEntry,
    takers: list[str],
) -> None:
    sender = fast_keepalive_entry.runtime_data.sender
    await _turn_on_scene(hass)
    assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_ON] * 3
    assert sender.stats.frames_sent == 0

    # Both outputs taken over DDP (a light taking pixels shows the lights sharing them off, All outputs too while it
    # showed the scene): a poll of the same scene leaves them on DDP.
    for entity_id in takers:
        await _call(hass, SERVICE_TURN_ON, entity_id, rgb_color=(255, 0, 0))
    await _poll(fast_keepalive_entry)
    assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_ON, STATE_ON, STATE_OFF]
    assert await _keepalive_ranges(udp_listener) == {(0, 122 * 3)}
    # Both off with no light showing the scene: released, so the controller's scene comes back; the next poll of the
    # same scene shows it on every DDP light, sending nothing.
    await _call(hass, SERVICE_TURN_OFF, OUT1)
    await _call(hass, SERVICE_TURN_OFF, OUT2)
    assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_OFF] * 3
    assert await _keepalive_ranges(udp_listener) == set()
    # Output 1 back on DDP before that poll: the off lights hold black again (Output 2's was released), until it is off.
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(255, 0, 0))
    assert await _keepalive_ranges(udp_listener) == {(0, 122 * 3)}
    assert udp_listener.packets[-1].data[OUT2_OFFSET:] == BLACK_OUT2
    await _call(hass, SERVICE_TURN_OFF, OUT1)
    assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_OFF] * 3
    frames = sender.stats.frames_sent
    await _poll(fast_keepalive_entry)
    assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_ON] * 3
    assert sender.stats.frames_sent == frames
    assert await _keepalive_ranges(udp_listener) == set()

    # Off during a scene: black for Output 2 only, held by keepalive; Output 1 keeps showing the scene.
    await _call(hass, SERVICE_TURN_OFF, OUT2)
    packet = await udp_listener.wait_for(lambda p: p.offset == OUT2_OFFSET)
    assert packet.data == BLACK_OUT2
    assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_ON, STATE_OFF, STATE_ON]
    assert await _keepalive_ranges(udp_listener) == {(OUT2_OFFSET, 42 * 3)}
    assert all(p.data == BLACK_OUT2 for p in udp_listener.packets)

    # A bare turn_on hands the pixels back to the scene: nothing sent, nothing held.
    await _call(hass, SERVICE_TURN_ON, OUT2)
    assert sender.stats.frames_sent == frames + 1
    assert _state(hass, OUT2).state == STATE_ON
    assert await _keepalive_ranges(udp_listener) == set()

    # A color takes Output 2 to DDP and shows All outputs, which shares its pixels, off; Output 1 still shows the scene.
    await _call(hass, SERVICE_TURN_ON, OUT2, rgb_color=(255, 0, 0))
    packet = await udp_listener.wait_for(lambda p: p.offset == OUT2_OFFSET)
    assert packet.data == process_frame(bytes((255, 0, 0)) * 42, sender.strip)
    assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_ON, STATE_ON, STATE_OFF]
    assert _state(hass, OUT2).attributes[ATTR_RGB_COLOR] == (255, 0, 0)

    # The controller turned "Off" in its own UI: the scene's lights go off, the DDP-driven one keeps running.
    fake_controller.select("Off")
    await _poll(fast_keepalive_entry)
    assert [_state(hass, e).state for e in [*DDP_LIGHTS, SCENES]] == [STATE_OFF, STATE_ON, STATE_OFF, STATE_OFF]
    # With no scene left, the off Output 1 holds black next to the DDP-driven one.
    assert await _keepalive_ranges(udp_listener) == {(0, 122 * 3)}
    assert udp_listener.packets[-1].data[:OUT2_OFFSET] == bytes(80 * 3)


@pytest.mark.parametrize(
    ("off", "unblank"),
    [(ALL, [ALL]), (ALL, [OUT1, OUT2]), (OUT2, [ALL])],
    ids=["all-outputs", "output-by-output", "all-outputs-over-a-disabled-output"],
)
async def test_all_outputs_turned_off_during_a_scene_blanks_the_strip(
    hass: HomeAssistant, udp_listener: Listener, fast_keepalive_entry: MockConfigEntry, off: str, unblank: list[str]
) -> None:
    sender = fast_keepalive_entry.runtime_data.sender
    await _turn_on_scene(hass)

    await _call(hass, SERVICE_TURN_OFF, off)
    if off == ALL:
        packet = await udp_listener.wait_for(lambda p: p.offset == 0)
        assert packet.data == bytes(122 * 3)
        assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_OFF] * 3
    else:  # All outputs still shows the scene on Output 1; a bare turn_on of it brings back every output
        assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_ON, STATE_OFF, STATE_ON]

    if len(unblank) > 1:
        # One output back to the scene: only the other output's black is still held, and All outputs stays off.
        await _call(hass, SERVICE_TURN_ON, OUT1)
        assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_ON, STATE_OFF, STATE_OFF]
        assert await _keepalive_ranges(udp_listener) == {(OUT2_OFFSET, 42 * 3)}
    await _call(hass, SERVICE_TURN_ON, unblank[-1])

    assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_ON] * 3
    assert sender.stats.frames_sent == 1  # only the black frame
    assert await _keepalive_ranges(udp_listener) == set()


def _bytes_of(strip: Any, segments: list[Any]) -> set[int]:
    return {b for s in segments for b in range(strip.byte_offset(s), strip.byte_offset(s) + strip.byte_length(s))}


@pytest.mark.parametrize("seed", range(6))
async def test_held_pixels_follow_the_light_states(
    hass: HomeAssistant,
    fake_controller: FakeController,
    fast_keepalive_entry: MockConfigEntry,
    seed: int,
) -> None:
    # Random commands and polls (fixed seeds): after each one the sender holds exactly the pixels of the lights that
    # hold, and a light showing the scene has no pixel held but those a light sharing them drives or blanks.
    runtime = fast_keepalive_entry.runtime_data
    sender, strip = runtime.sender, runtime.sender.strip
    rng = random.Random(seed)  # noqa: S311
    ops: list[Callable[[str], Awaitable[None]]] = [
        lambda e: _call(hass, SERVICE_TURN_ON, SCENES, effect=rng.choice(["Show1", "Test"])),
        lambda e: _call(hass, SERVICE_TURN_OFF, SCENES),
        lambda e: _poll_after(fake_controller, fast_keepalive_entry, rng.choice(["Show1", "Test", "Off"])),
        lambda e: _poll(fast_keepalive_entry),
        lambda e: _call(hass, SERVICE_TURN_ON, e, effect="chase"),
        lambda e: _call(hass, SERVICE_TURN_ON, e, rgb_color=(255, 0, 0)),
        lambda e: _call(hass, SERVICE_TURN_ON, e),
        lambda e: _call(hass, SERVICE_TURN_OFF, e),
        lambda e: _call(hass, SERVICE_TURN_OFF, e),
    ]
    for step in range(40):
        await rng.choice(ops)(rng.choice(DDP_LIGHTS))
        await asyncio.sleep(0.1)  # an effect's first frame
        lights = runtime.segment_lights
        report = (step, [(light.entity_id, light.source.value) for light in lights], sender._held)
        held = {b for offset, length in sender._held for b in range(offset, offset + length)}
        assert held == _bytes_of(strip, [light.segment for light in lights if light.holding]), report
        for light in lights:
            if light.source is LightSource.SCENE:
                taken = [o.segment for o in lights if o.source in (LightSource.DDP, LightSource.BLANKED)]
                assert held & _bytes_of(strip, [light.segment]) <= _bytes_of(strip, taken), (light.entity_id, report)


async def _poll_after(fake_controller: FakeController, entry: MockConfigEntry, effect: str) -> None:
    """The controller's own UI selects ``effect``; then the 30 s poll sees it."""
    fake_controller.select(effect)
    await _poll(entry)


@pytest.mark.parametrize(
    ("source", "sent"), [("scene", False), ("ddp", True)], ids=["showing-the-scene", "driven-by-ddp"]
)
async def test_restart_during_a_scene(
    hass: HomeAssistant,
    udp_listener: Listener,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    source: str,
    sent: bool,
) -> None:
    # Output 1 was on (showing the scene, or red over DDP); Output 2 was blanked, which is not restored.
    on = {ATTR_BRIGHTNESS: 255, ATTR_RGB_COLOR: [255, 0, 0], ATTR_EFFECT: "off"}
    mock_restore_cache_with_extra_data(
        hass, [(State(OUT1, STATE_ON, on), {"source": source}), (State(OUT2, STATE_OFF), {"source": "blanked"})]
    )
    entry = entry_factory(controller=fake_controller)  # the controller still runs "Glow"
    await _setup(hass, entry)
    await udp_listener.settle()

    assert [p.offset for p in udp_listener.packets] == ([0] if sent else [])
    assert fake_controller.commands() == []
    # Idle lights show the running scene; All outputs shares Output 1's pixels, so it shows them only off DDP.
    assert [_state(hass, e).state for e in DDP_LIGHTS] == [STATE_ON, STATE_ON, STATE_OFF if sent else STATE_ON]
    assert await hass.config_entries.async_unload(entry.entry_id)


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
    # A palette slot set before any read knows no other slot: the palette is read back. HA calls no action on an
    # unavailable entity, so this drives the shared commands directly.
    commands = entry.runtime_data.commands
    assert commands is not None
    await commands.set_color(COLORS[1], 2, (255, 0, 0))
    assert fake_controller.commands() == [_color(2, "#ff0000")]
    assert _state(hass, SCENES).attributes["colors"] == [FIXTURE_COLORS[0], [255, 0, 0], EMPTY, EMPTY, EMPTY]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=62))
    await hass.async_block_till_done(wait_background_tasks=True)
    state = _state(hass, SCENES)
    assert (state.state, state.attributes[ATTR_EFFECT], state.attributes[ATTR_EFFECT_LIST]) == (
        STATE_ON,
        "Glow",
        EFFECTS,
    )
    assert any("recovered" in r.getMessage() for r in caplog.records)  # logged once when it is back
    assert _state(hass, OUT1).state == STATE_ON  # an idle DDP light now shows the scene the poll found

    # A command that succeeds after a failed poll logs the same recovery as a successful poll would.
    fake_controller.offline = True
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=93))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _state(hass, SCENES).state == STATE_UNAVAILABLE
    caplog.clear()
    scenes = entry.runtime_data.scenes
    assert scenes is not None and scenes.data is not None
    scenes.publish(scenes.data)
    assert _state(hass, SCENES).state == STATE_ON
    assert sum("recovered" in r.getMessage() for r in caplog.records) == 1
    scenes.publish(scenes.data)  # already up: no second message
    assert sum("recovered" in r.getMessage() for r in caplog.records) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def _strobe(hass: HomeAssistant) -> None:
    await _call(hass, SERVICE_TURN_ON, SCENES, effect="Strobe")


async def _off(hass: HomeAssistant) -> None:
    await _call(hass, SERVICE_TURN_OFF, SCENES)


async def _recall_on_segment(hass: HomeAssistant) -> None:
    await _call(hass, "recall_preset", OUT1, domain=DOMAIN, preset=1)


async def _pattern_on_scenes(hass: HomeAssistant) -> None:
    await _call(hass, "set_pattern", SCENES, domain=DOMAIN, colors=[[255, 0, 0], [0, 255, 0]])


async def _colors_on_segment(hass: HomeAssistant) -> None:
    await _call(hass, "set_colors", OUT1, domain=DOMAIN, colors=[[255, 0, 0]])


async def _set_colors(hass: HomeAssistant) -> None:
    await _call(hass, "set_colors", SCENES, domain=DOMAIN, colors=["warm_white"])


async def _set_slot(hass: HomeAssistant) -> None:
    await _call(hass, SERVICE_TURN_ON, COLORS[0], rgb_color=(255, 0, 0))


async def _slot_effect(hass: HomeAssistant) -> None:
    await _call(hass, SERVICE_TURN_ON, COLORS[0], effect="Daylight")


async def _pattern_on_slot(hass: HomeAssistant) -> None:
    await _call(hass, "set_pattern", COLORS[0], domain=DOMAIN, colors=[[255, 0, 0]])


async def _recall_on_slot(hass: HomeAssistant) -> None:
    await _call(hass, "recall_preset", COLORS[0], domain=DOMAIN, preset=1)


@pytest.mark.parametrize(
    ("action", "offline", "segment_fails", "error", "out1_after"),
    [
        (_strobe, False, False, ServiceValidationError, STATE_ON),
        (_recall_on_segment, False, False, ServiceValidationError, STATE_ON),
        (_pattern_on_scenes, False, False, ServiceValidationError, STATE_ON),
        (_colors_on_segment, False, False, ServiceValidationError, STATE_ON),
        # A controller that never took the command must not cost the DDP look.
        (_turn_on_scene, True, False, HomeAssistantError, STATE_ON),
        (_recall_preset, True, False, HomeAssistantError, STATE_ON),
        (_off, True, False, HomeAssistantError, STATE_OFF),
        (_set_colors, True, False, HomeAssistantError, STATE_ON),  # the palette is not DDP's: OUT1 stays on
        (_set_slot, True, False, HomeAssistantError, STATE_ON),
        (_slot_effect, False, False, ServiceValidationError, STATE_ON),
        (_pattern_on_slot, False, False, ServiceValidationError, STATE_ON),
        (_recall_on_slot, False, False, ServiceValidationError, STATE_ON),
        # A segment that cannot be blanked must not stop the controller's "Off".
        (_off, False, True, HomeAssistantError, STATE_ON),
    ],
    ids=[
        "unknown-effect",
        "recall-on-segment",
        "pattern-on-scenes",
        "colors-on-segment",
        "turn_on-down",
        "recall-down",
        "turn_off-down",
        "set_colors-down",
        "color-slot-down",
        "color-slot-unknown-effect",
        "pattern-on-color-slot",
        "recall-on-color-slot",
        "segment-send-fails",
    ],
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
    if offline:
        assert raised.value.translation_key == "controller_failed"
    if segment_fails:
        assert fake_controller.commands() == [_control('{"fx":"Off"}')]
    assert _state(hass, OUT1).state == out1_after


async def test_recall_preset_read_back_failure_marks_the_light_unavailable(
    hass: HomeAssistant, ndb_entry: MockConfigEntry
) -> None:
    # The preset was recalled; only the read that follows fails. The action succeeds and the light says it is unsure.
    with patch.object(ndb_entry.runtime_data.controller, "state", side_effect=ControllerError("gone")):
        await _recall_preset(hass)
    assert _state(hass, SCENES).state == STATE_UNAVAILABLE


@pytest.mark.parametrize("host", ["127.0.0.1", "not a host"], ids=["silent", "host-the-library-rejects"])
async def test_http_layout_and_detection_of_an_unusable_controller(fake_controller: FakeController, host: str) -> None:
    fake_controller.offline = True
    assert await fetch_layout("ndbpro", host, fake_controller.port) is None
    assert await detect(host, fake_controller.port) == "generic"
