"""The controller device (main light, Color 1-5, Scene): built-in effects and palette over the fake
HTTP UI, and handing the pixels back from DDP."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from aioddp import process_frame
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_SUPPORTED_COLOR_MODES,
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
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import color as color_util
from homeassistant.util import dt as dt_util
from pyminleon import ControllerError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.ddp.const import DOMAIN, LightSource
from custom_components.ddp.controller import detect, fetch_layout
from custom_components.ddp.coordinator import SCAN_INTERVAL

from .conftest import SEGMENTS, FakeController, Listener, assert_holds_follow_lights

MAIN = "light.ndb_pro_minleon_ndb_pro"  # the controller device "NDB-Pro Minleon NDB-Pro"'s main entity
SCENE = "select.ndb_pro_minleon_ndb_pro_scene"
COLORS = [f"light.ndb_pro_minleon_ndb_pro_color_{slot}" for slot in range(1, 6)]
OUT1 = "light.ndb_pro_output_1"  # the main entity of the output's own device, "NDB-Pro Output 1"
OUT2 = "light.ndb_pro_output_2"
ALL = "light.ndb_pro_generic_ddp_all_outputs"  # on the DDP device, "NDB-Pro Generic DDP" for a vendor entry
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
    # One device per output, named after the entry and the output, connected through the DDP device; its light is
    # the main entity. All outputs stays on the DDP device.
    for number, entity_id in enumerate((OUT1, OUT2), start=1):
        output = device_registry.async_get_device_by_identifier((DOMAIN, f"127.0.0.1:output{number}"), entry.entry_id)
        assert output is not None
        assert (output.name, output.manufacturer, output.model, output.sw_version, output.via_device_id) == (
            f"NDB-Pro Output {number}",
            "Minleon",
            "NDBPRO",
            "1.2.3",
            ddp_device.id,
        )
        registered = entity_registry.async_get(entity_id)
        assert registered is not None
        assert (_state(hass, entity_id).name, registered.unique_id, registered.device_id) == (
            f"NDB-Pro Output {number}",
            f"{entry.entry_id}_output{number}",
            output.id,
        )
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert len(devices) == (3 if controller_type == "generic" else 4)
    if controller_type == "generic":
        # The DDP device is named after the entry; no controller entities.
        assert ddp_device.name == "NDB-Pro"
        assert _state(hass, "light.ndb_pro_all_outputs").name == "NDB-Pro All outputs"
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
        ALL: ("NDB-Pro Generic DDP All outputs", "strip", ddp_device),
        MAIN: ("NDB-Pro Minleon NDB-Pro", "controller", controller_device),
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
    # 3 DDP lights, then Power (the controller device's main entity), Color 1-5 and Scene.
    assert len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == 10
    main = _state(hass, MAIN)
    assert (main.state, main.attributes["brightness"]) == (STATE_ON, 255)  # intensity 100 %
    assert main.attributes["supported_color_modes"] == [ColorMode.BRIGHTNESS]
    assert (main.attributes["colors"], main.attributes["background"]) == (FIXTURE_COLORS, EMPTY)
    scene = _state(hass, SCENE)
    assert (scene.state, scene.attributes["options"]) == ("Glow", EFFECTS)  # the fixture page's selected effect
    assert [_state(hass, e).state for e in COLORS] == [STATE_ON] + [STATE_OFF] * 4  # bc1 #be38f3, the rest empty
    assert fake_controller.commands() == []  # setup only reads
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("controller_type", "prefix"),
    [("generic", "ndb_pro_"), ("ndbpro", "ndb_pro_generic_ddp_")],
    ids=["generic", "vendor"],
)
async def test_upgrade_moves_the_output_lights_to_their_devices_keeping_entity_ids(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    controller_type: str,
    prefix: str,
) -> None:
    # A 0.6.0 entry: every DDP light on the DDP device (a vendor entry's named "NDB-Pro Generic DDP ..."), which the
    # user put in an area and renamed.
    entry = entry_factory(controller=fake_controller, controller_type=controller_type, options={"pixel_lights": True})
    garden, porch = ar.async_get(hass).async_create("Garden"), ar.async_get(hass).async_create("Porch")
    ddp_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "127.0.0.1")}
    )
    device_registry.async_update_device(ddp_device.id, name_by_user="My Strip", area_id=garden.id)
    old = {
        key: entity_registry.async_get_or_create(
            LIGHT_DOMAIN,
            DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            device_id=ddp_device.id,
            suggested_object_id=f"{prefix}{object_id}",
        ).entity_id
        for key, object_id in [
            ("output1", "output_1"),
            ("output2_pixel5", "output_2_light_5"),
            ("strip", "all_outputs"),
        ]
    }
    await _setup(hass, entry)

    def device_of(entity_id: str) -> dr.DeviceEntry:
        registered = entity_registry.async_get(entity_id)
        assert registered is not None
        device = device_registry.async_get(registered.device_id)  # type: ignore[arg-type]
        assert device is not None
        return device

    # Same unique IDs and entity IDs; the output lights and pixel lights now sit on their output's device, created in
    # the DDP device's area and, as it was renamed, named after its name: area targeting and friendly names hold.
    for key, entity_id in old.items():
        assert entity_registry.async_get(entity_id).unique_id == f"{entry.entry_id}_{key}"  # type: ignore[union-attr]
        device = device_of(entity_id)
        output = key.split("_")[0]
        expected = {(DOMAIN, "127.0.0.1")} if key == "strip" else {(DOMAIN, f"127.0.0.1:{output}")}
        assert device.identifiers == expected
        assert device.area_id == garden.id
        assert _state(hass, entity_id).state == STATE_OFF
    assert device_of(old["output1"]).name_by_user == "My Strip Output 1"
    names = [_state(hass, old[key]).name for key in ("output1", "output2_pixel5", "strip")]
    assert names == ["My Strip Output 1", "My Strip Output 2 Light 5", "My Strip All outputs"]

    async def reload(**changes: Any) -> None:
        hass.config_entries.async_update_entry(entry, **changes)
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    # What the user sets on an output device later is theirs: a reload, and dropping to one output and back (HA
    # restores the removed device with its name and area), never copy the DDP device's again.
    output1 = device_of(old["output1"])
    device_registry.async_update_device(output1.id, name_by_user="Eave", area_id=porch.id)
    await reload()
    segments = entry.data["segments"]
    await reload(data={**entry.data, "segments": segments[:1], "pixel_count": 80})
    assert device_of(old["output1"]).identifiers == {(DOMAIN, "127.0.0.1")}
    await reload(data={**entry.data, "segments": segments, "pixel_count": 122})
    restored = device_of(old["output1"])
    assert (restored.id, restored.name_by_user, restored.area_id) == (output1.id, "Eave", porch.id)
    assert _state(hass, old["output1"]).name == "Eave"
    assert "deprecated" not in caplog.text  # only the device registry's public API
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("named", [False, True], ids=["default-name", "renamed"])
async def test_a_new_output_device_takes_the_ddp_devices_area(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entry_factory: Callable[..., MockConfigEntry],
    named: bool,
) -> None:
    # A Generic entry going from one output to two: the new output devices take the DDP device's area, and its
    # user-set name (else their default name).
    garden = ar.async_get(hass).async_create("Garden")
    entry = entry_factory(segments=[{"key": "output1", "name": "Output 1", "start": 0, "length": 80}])
    await _setup(hass, entry)
    ddp_device = device_registry.async_get_device_by_identifier((DOMAIN, "127.0.0.1"), entry.entry_id)
    assert ddp_device is not None
    device_registry.async_update_device(ddp_device.id, area_id=garden.id, name_by_user="My Strip" if named else None)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "segments": SEGMENTS, "pixel_count": 122})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    output2 = device_registry.async_get_device_by_identifier((DOMAIN, "127.0.0.1:output2"), entry.entry_id)
    assert output2 is not None
    assert (output2.area_id, output2.name, output2.name_by_user) == (
        garden.id,
        "NDB-Pro Output 2",
        "My Strip Output 2" if named else None,
    )
    entity_id = er.async_get(hass).async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{entry.entry_id}_output2")
    assert entity_id is not None
    assert _state(hass, entity_id).name == ("My Strip Output 2" if named else "NDB-Pro Output 2")
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(("brightness", "percent"), [(128, 50), (1, 1)], ids=["half", "minimum"])
async def test_brightness_sets_the_controller_intensity(
    hass: HomeAssistant,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    brightness: int,
    percent: int,
) -> None:
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)

    await _call(hass, SERVICE_TURN_ON, MAIN, brightness=brightness)

    # The intensity, then the last scene as a bare turn_on does.
    assert fake_controller.commands() == [_control(f'{{"int":"{percent}"}}'), _control('{"fx":"Glow"}')]
    for _ in range(2):  # the command's published value, then the poll reading it back
        main = _state(hass, MAIN)
        assert (main.state, main.attributes["brightness"]) == (STATE_ON, round(percent * 255 / 100))
        await _poll(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)


def _color(slot: int, color: str) -> tuple[str, str, bytes]:
    return _control(f'{{"color":{{"i":{slot},"c":"{color}"}}}}')


@pytest.mark.parametrize(
    ("running", "restored", "domain", "service", "entity_id", "data", "body", "effect"),
    [
        ("Off", None, "light", SERVICE_TURN_ON, MAIN, {}, '{"fx":"Test"}', "Test"),  # no last scene: the first
        ("Off", "Show2", "light", SERVICE_TURN_ON, MAIN, {}, '{"fx":"Show2"}', "Show2"),  # restored last scene
        ("Glow", None, "light", SERVICE_TURN_OFF, MAIN, {}, '{"fx":"Off"}', None),
        ("Off", None, "select", "select_option", SCENE, {"option": "Snow"}, '{"fx":"Snow"}', "Snow"),
        ("Glow", None, "select", "select_option", SCENE, {"option": "Snow"}, '{"fx":"Snow"}', "Snow"),
    ],
    ids=["on-first", "on-last", "off", "select-from-off", "select-other"],
)
async def test_main_light_and_scene_command_the_controller(
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
        mock_restore_cache_with_extra_data(hass, [(State(MAIN, STATE_OFF), {"last_effect": restored})])
    fake_controller.select(running)
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(255, 0, 0))  # DDP drives Output 1
    fake_controller.requests.clear()

    await _call(hass, service, entity_id, domain=domain, **data)

    assert fake_controller.commands() == [_control(body)]
    on = effect is not None
    for _ in range(2):  # the command's assumed state, then the poll reading it back
        power, scene = _state(hass, MAIN), _state(hass, SCENE)
        assert power.state == (STATE_ON if on else STATE_OFF)
        assert scene.state == (effect or STATE_UNKNOWN)
        # A scene takes the pixels back from DDP (Output 1 goes off); the controller's Off leaves DDP mode alone.
        assert _state(hass, OUT1).state == (STATE_OFF if on else STATE_ON)
        assert entry.runtime_data.runners == {}
        await _poll(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("shown", "state", "color_mode", "value"),
    [
        ("#000000", STATE_OFF, ColorMode.RGB, None),
        ("#feffff", STATE_ON, ColorMode.COLOR_TEMP, 6500),
        ("#c69740", STATE_ON, ColorMode.COLOR_TEMP, 2700),  # the colors page once Warm White is set
        ("#ffffff", STATE_ON, ColorMode.COLOR_TEMP, 2700),
        ("#123456", STATE_ON, ColorMode.RGB, (0x12, 0x34, 0x56)),
    ],
    ids=["empty", "cool-white", "warm-white-shown", "warm-white-sent", "color"],
)
async def test_color_reads_its_palette_slot(
    hass: HomeAssistant,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    shown: str,
    state: str,
    color_mode: ColorMode,
    value: int | tuple[int, int, int] | None,
) -> None:
    fake_controller.set_color(2, shown)
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)

    # Color 2 has a Color and a Temperature tab: a white reads as warm 2700 K or cool 6500 K.
    color = _state(hass, COLORS[1])
    assert color.state == state
    assert color.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.COLOR_TEMP, ColorMode.RGB]
    assert (color.attributes[ATTR_MIN_COLOR_TEMP_KELVIN], color.attributes[ATTR_MAX_COLOR_TEMP_KELVIN]) == (2700, 6500)
    assert ATTR_EFFECT_LIST not in color.attributes
    if state == STATE_ON:
        attribute = ATTR_RGB_COLOR if color_mode is ColorMode.RGB else ATTR_COLOR_TEMP_KELVIN
        assert (color.attributes[ATTR_COLOR_MODE], color.attributes[attribute]) == (color_mode, value)
        assert color.attributes[ATTR_BRIGHTNESS] == 255
    assert _state(hass, COLORS[0]).attributes[ATTR_RGB_COLOR] == (0xBE, 0x38, 0xF3)  # its own slot only
    assert await hass.config_entries.async_unload(entry.entry_id)


def _light(service: str, **data: Any) -> tuple[str, str, int, dict[str, Any]]:
    return (LIGHT_DOMAIN, service, 1, data)


def _kelvin_hex(kelvin: int, brightness: int = 255) -> str:
    """HA's RGB for ``kelvin`` at ``brightness``, as the slot's hex."""
    rgb = [round(round(value) * brightness / 255) for value in color_util.color_temperature_to_rgb(kelvin)]
    return "#" + "".join(f"{value:02x}" for value in rgb)


@pytest.mark.parametrize(
    ("shown", "restored", "commands", "sent", "state", "mode"),
    [
        # Color mode: an RGB light.
        ("#000000", None, [_light(SERVICE_TURN_ON, rgb_color=(255, 0, 0))], ["#ff0000"], STATE_ON, "color"),
        (
            "#000000",
            None,
            [_light(SERVICE_TURN_ON, rgb_color=(255, 0, 0), brightness=128)],
            ["#800000"],
            STATE_ON,
            "color",
        ),
        ("#123456", None, [_light(SERVICE_TURN_ON, brightness=51)], ["#040a11"], STATE_ON, "color"),  # scaled
        ("#123456", None, [_light(SERVICE_TURN_OFF)], ["#000000"], STATE_OFF, "color"),
        ("#000000", {"last_color": [0, 0, 255]}, [_light(SERVICE_TURN_ON)], ["#0000ff"], STATE_ON, "color"),
        # The restored last color can be a white: it comes back.
        ("#000000", {"last_color": [254, 255, 255]}, [_light(SERVICE_TURN_ON)], ["#feffff"], STATE_ON, "cool_white"),
        ("#000000", None, [_light(SERVICE_TURN_ON)], ["#ff0000"], STATE_ON, "color"),  # none: red (white is a mode)
        # A temperature: the ends are the controller's whites, sent exact; in between HA's kelvin -> RGB, scaled by a
        # brightness, which reads back as a color.
        ("#000000", None, [_light(SERVICE_TURN_ON, color_temp_kelvin=2700)], ["#ffffff"], STATE_ON, "warm_white"),
        ("#123456", None, [_light(SERVICE_TURN_ON, color_temp_kelvin=4000)], [_kelvin_hex(4000)], STATE_ON, "color"),
        (
            "#000000",
            None,
            [_light(SERVICE_TURN_ON, color_temp_kelvin=4000, brightness=128)],
            [_kelvin_hex(4000, 128)],
            STATE_ON,
            "color",
        ),
        ("#c69740", None, [_light(SERVICE_TURN_ON, color_temp_kelvin=6500)], ["#feffff"], STATE_ON, "cool_white"),
        ("#000000", None, [_light(SERVICE_TURN_ON, color_temp_kelvin=9000)], ["#feffff"], STATE_ON, "cool_white"),
        # On a white: bare re-sends the white exact (brightness too), a color leaves it, off clears the slot.
        ("#c69740", None, [_light(SERVICE_TURN_ON)], ["#ffffff"], STATE_ON, "warm_white"),
        ("#feffff", None, [_light(SERVICE_TURN_ON, brightness=51)], ["#feffff"], STATE_ON, "cool_white"),
        ("#feffff", None, [_light(SERVICE_TURN_ON, rgb_color=(255, 0, 0))], ["#ff0000"], STATE_ON, "color"),
        ("#feffff", None, [_light(SERVICE_TURN_OFF)], ["#000000"], STATE_OFF, "color"),
        # Old restore data carried a ``last_custom`` too: ignored.
        (
            "#000000",
            {"last_color": [0, 0, 255], "last_custom": [255, 0, 0]},
            [_light(SERVICE_TURN_ON)],
            ["#0000ff"],
            STATE_ON,
            "color",
        ),
    ],
    ids=[
        "rgb",
        "rgb-brightness",
        "brightness",
        "off",
        "bare-restored",
        "bare-restored-white",
        "bare-default",
        "kelvin-2700",
        "kelvin-4000",
        "kelvin-4000-brightness",
        "kelvin-6500",
        "kelvin-past-max",
        "white-bare",
        "white-brightness",
        "white-rgb",
        "white-off",
        "bare-restored-ignores-legacy-last-custom",
    ],
)
async def test_color_sets_its_palette_slot(
    hass: HomeAssistant,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    shown: str,
    restored: dict[str, list[int]] | None,
    commands: list[tuple[str, str, int, dict[str, Any]]],
    sent: list[str],
    state: str,
    mode: str,
) -> None:
    if restored is not None:
        mock_restore_cache_with_extra_data(hass, [(State(COLORS[1], STATE_OFF), restored)])
    fake_controller.set_color(2, shown)
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(0, 255, 0))
    fake_controller.requests.clear()

    for domain, service, index, data in commands:
        await _call(hass, service, COLORS[index], domain=domain, **data)

    assert fake_controller.commands() == [_color(2, color) for color in sent]
    final = sent[-1] if sent else shown
    slot = [int(final[i : i + 2], 16) for i in (1, 3, 5)]
    for _ in range(2):  # the command's published palette, then the poll reading the page back
        color = _state(hass, COLORS[1])
        assert color.state == state
        # A white reads as a temperature, anything else as a color.
        if state == STATE_ON and mode == "color":
            assert (color.attributes[ATTR_COLOR_MODE], list(color.attributes[ATTR_RGB_COLOR])) == (ColorMode.RGB, slot)
        elif state == STATE_ON:
            kelvin = 2700 if mode == "warm_white" else 6500
            assert (color.attributes[ATTR_COLOR_MODE], color.attributes[ATTR_COLOR_TEMP_KELVIN]) == (
                ColorMode.COLOR_TEMP,
                kelvin,
            )
        # Power's palette attribute updates at once; the other slots and DDP are untouched.
        if sent:
            assert _state(hass, MAIN).attributes["colors"] == [FIXTURE_COLORS[0], slot, EMPTY, EMPTY, EMPTY]
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
    fake_controller.select("Off")  # Power need not be on
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)

    await _call(hass, "set_colors", MAIN, domain=DOMAIN, **data)

    assert fake_controller.commands() == [_color(slot, color) for slot, color in enumerate(sent, start=1)]
    for _ in range(2):  # the command's assumed palette, then the poll reading the page back
        state = _state(hass, MAIN)
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
        await _call(hass, "set_colors", MAIN, domain=DOMAIN, **data)
    assert fake_controller.commands() == []


async def _select(hass: HomeAssistant, option: str) -> None:
    await _call(hass, "select_option", SCENE, domain="select", option=option)


async def _turn_on_scene(hass: HomeAssistant) -> None:
    await _select(hass, "Show1")


async def _off(hass: HomeAssistant) -> None:
    await _call(hass, SERVICE_TURN_OFF, MAIN, domain="light")


async def _recall_preset(hass: HomeAssistant) -> None:
    await _call(hass, "recall_preset", MAIN, domain=DOMAIN, preset=3)


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
    # Scene mode: every DDP light is off, the ones DDP drove included, and nothing is held.
    assert [_state(hass, entity_id).state for entity_id in DDP_LIGHTS] == [STATE_OFF] * 3
    assert runtime.sender.held == ()
    assert _state(hass, MAIN).state == STATE_ON
    # Neither a black frame nor a keepalive (default 1 s) follows: the controller's own scene takes over.
    await udp_listener.settle(2.2)
    assert udp_listener.packets == []


async def test_polled_scene_reaches_the_ddp_lights_with_the_main_light_disabled(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, fake_controller: FakeController, ndb_entry: MockConfigEntry
) -> None:
    # The poll hook belongs to the commands, not to an entity: disabling the main entity keeps polls working.
    entity_registry.async_update_entity(MAIN, disabled_by=er.RegistryEntryDisabler.USER)
    assert await hass.config_entries.async_reload(ndb_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(MAIN) is None
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(255, 0, 0))

    await _poll_after(fake_controller, ndb_entry, "Show1")

    assert ndb_entry.runtime_data.segment_lights[0].source is LightSource.OFF
    assert _state(hass, OUT1).state == STATE_OFF
    assert ndb_entry.runtime_data.runners == {}


PIXEL = f"{OUT2}_light_1"


@pytest.mark.parametrize("offline", [False, True], ids=["controller-turns-off", "controller-down"])
@pytest.mark.parametrize(
    ("entity_id", "service", "data"),
    [
        (OUT1, SERVICE_TURN_ON, {"rgb_color": (255, 0, 0)}),
        (ALL, SERVICE_TURN_ON, {}),
        (PIXEL, SERVICE_TURN_ON, {"rgb_color": (255, 0, 0)}),
        (OUT2, "set_pattern", {"colors": [[255, 0, 0], [0, 0, 255]]}),
    ],
    ids=["output-color", "all-outputs-bare", "pixel-light", "pattern"],
)
async def test_a_ddp_light_turned_on_during_a_scene_turns_the_controller_off(
    hass: HomeAssistant,
    udp_listener: Listener,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    entity_id: str,
    service: str,
    data: dict[str, Any],
    offline: bool,
) -> None:
    entry = entry_factory(controller=fake_controller, options={"pixel_lights": True})
    await _setup(hass, entry)  # the controller runs "Glow": scene mode, every DDP light off
    assert _state(hass, MAIN).state == STATE_ON
    assert [_state(hass, e).state for e in [*DDP_LIGHTS, PIXEL]] == [STATE_OFF] * 4
    fake_controller.requests.clear()
    fake_controller.offline = offline
    domain = DOMAIN if service == "set_pattern" else LIGHT_DOMAIN

    if offline:  # the controller cannot leave its scene: nothing is sent
        with pytest.raises(HomeAssistantError) as raised:
            await _call(hass, service, entity_id, domain=domain, **data)
        assert raised.value.translation_key == "controller_failed"
        await udp_listener.settle()
        assert udp_listener.packets == []
        assert _state(hass, entity_id).state == STATE_OFF
    else:  # DDP mode: the controller is off first, then the light drives its pixels
        await _call(hass, service, entity_id, domain=domain, **data)
        assert fake_controller.commands() == [_control('{"fx":"Off"}')]
        await udp_listener.wait_for(lambda p: any(p.data))
        assert (_state(hass, MAIN).state, _state(hass, SCENE).state) == (STATE_OFF, STATE_UNKNOWN)
        assert _state(hass, entity_id).state == STATE_ON
        await _poll(entry)  # the controller reports "Off": DDP keeps the pixels
        assert _state(hass, entity_id).state == STATE_ON
        assert fake_controller.commands() == [_control('{"fx":"Off"}')]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("concurrent", [False, True], ids=["sequential", "gather"])
async def test_held_pixels_follow_the_light_states(
    hass: HomeAssistant,
    udp_listener: Listener,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    seed: int,
    concurrent: bool,
) -> None:
    # Random commands and polls (fixed seeds), one at a time or two or three at once (an HA scene applies lights
    # concurrently): once they settle, scene mode and DDP mode are exclusive (no DDP light is on while the controller
    # runs a scene) and the sender holds exactly the pixels of the lights that hold. Odd seeds add pixel lights (each
    # output's first and last, and one in between) to the lights the commands pick from.
    pixels = seed % 2 == 1
    entry = entry_factory(controller=fake_controller, options={"keepalive": 0.5, "pixel_lights": pixels})
    await _setup(hass, entry)
    targets = DDP_LIGHTS + ([f"{OUT1}_light_1", f"{OUT1}_light_80", f"{OUT2}_light_7"] if pixels else [])
    runtime = entry.runtime_data
    rng = random.Random(seed)  # noqa: S311
    ops: list[Callable[[str], Awaitable[None]]] = [
        lambda e: _select(hass, rng.choice(["Show1", "Test"])),
        lambda e: _off(hass),
        lambda e: _poll_after(fake_controller, entry, rng.choice(["Show1", "Test", "Off"])),
        lambda e: _poll(entry),
        lambda e: _call(hass, SERVICE_TURN_ON, e, effect="chase"),
        lambda e: _call(hass, SERVICE_TURN_ON, e, rgb_color=(255, 0, 0)),
        lambda e: _call(hass, SERVICE_TURN_ON, e),
        lambda e: _call(hass, SERVICE_TURN_OFF, e),
        lambda e: _call(hass, SERVICE_TURN_OFF, e),
    ]
    for step in range(40):
        count = rng.randint(2, 3) if concurrent else 1
        await asyncio.gather(*(rng.choice(ops)(rng.choice(targets)) for _ in range(count)))
        await udp_listener.settle()  # an effect's first frame
        assert_holds_follow_lights(runtime, step)
        assert not (runtime.scene_running and runtime.ddp_driving), step
        assert (_state(hass, MAIN).state == STATE_ON) == runtime.scene_running, step
        running = fake_controller.selected() not in (None, "Off")  # what the controller itself runs
        assert running == runtime.scene_running, (step, fake_controller.selected())
    assert await hass.config_entries.async_unload(entry.entry_id)


async def _poll_after(fake_controller: FakeController, entry: MockConfigEntry, effect: str) -> None:
    """The controller's own UI selects ``effect``; then the 30 s poll sees it."""
    fake_controller.select(effect)
    await _poll(entry)


@pytest.mark.parametrize(
    ("effect", "light", "source", "sent"),
    [
        ("Glow", OUT1, "ddp", False),
        ("Off", OUT1, "ddp", True),
        ("Off", PIXEL, "ddp", True),
        ("Off", OUT1, "scene", False),
    ],
    ids=["scene-running", "controller-off", "pixel-light-only", "stored-scene-source"],
)
async def test_restart_follows_the_controller_mode(
    hass: HomeAssistant,
    udp_listener: Listener,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    effect: str,
    light: str,
    source: str,
    sent: bool,
) -> None:
    # One light was on (red over DDP, or, stored by an earlier version, showing the scene); Output 2 was blanked by
    # an earlier version. Setup only reads the controller: a running scene keeps every DDP light off and unsent.
    on = {ATTR_BRIGHTNESS: 255, ATTR_RGB_COLOR: [255, 0, 0], ATTR_EFFECT: "off"}
    mock_restore_cache_with_extra_data(
        hass, [(State(light, STATE_ON, on), {"source": source}), (State(OUT2, STATE_OFF), {"source": "blanked"})]
    )
    fake_controller.select(effect)
    entry = entry_factory(controller=fake_controller, options={"pixel_lights": True})
    await _setup(hass, entry)
    await udp_listener.settle()

    red = process_frame(bytes((255, 0, 0)), entry.runtime_data.sender.strip)
    offset = OUT2_OFFSET if light == PIXEL else 0
    assert any(p.offset == offset and p.data[:3] == red for p in udp_listener.packets) == sent
    assert all(not any(p.data) or p.offset == offset for p in udp_listener.packets)  # else only black
    assert fake_controller.commands() == []
    assert [_state(hass, e).state for e in [*DDP_LIGHTS, PIXEL]] == [
        STATE_ON if sent and e == light else STATE_OFF for e in [*DDP_LIGHTS, PIXEL]
    ]
    # The restored light's pixels are held, and the off lights' black next to them.
    assert_holds_follow_lights(entry.runtime_data, "restart")
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("reachable", [True, False], ids=["controller-answers", "controller-down"])
async def test_a_ddp_light_turned_on_while_the_controller_state_is_unknown(
    hass: HomeAssistant,
    udp_listener: Listener,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    reachable: bool,
) -> None:
    fake_controller.offline = True  # its page says Glow runs, but setup cannot read it
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)
    fake_controller.offline = not reachable

    # Best effort: the controller is told Off if it answers; DDP drives the pixels either way, without an error.
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(255, 0, 0))
    await udp_listener.wait_for(lambda p: p.offset == 0 and any(p.data))
    assert fake_controller.commands() == ([_control('{"fx":"Off"}')] if reachable else [])
    assert _state(hass, OUT1).state == STATE_ON

    # The first good poll decides: still a scene (it never took the Off) turns the DDP lights off.
    fake_controller.offline = False
    await _poll(entry)
    assert _state(hass, OUT1).state == (STATE_ON if reachable else STATE_OFF)
    assert_holds_follow_lights(entry.runtime_data)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_ddp_light_does_not_wait_for_a_poll(
    hass: HomeAssistant,
    udp_listener: Listener,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
) -> None:
    fake_controller.select("Off")
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)  # DDP mode
    fake_controller.page_delay = 2.0
    poll = hass.async_create_task(_poll(entry))
    await asyncio.sleep(0.05)  # the poll holds the coordinator lock, waiting for its page

    async with asyncio.timeout(0.5):  # well under the page delay: no controller I/O when no scene is known to run
        await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(255, 0, 0))
    assert not poll.done()
    await poll
    assert _state(hass, OUT1).state == STATE_ON
    assert fake_controller.commands() == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("effect", [None, "Snow"], ids=["power-off", "select"])
async def test_poll_in_flight_cannot_undo_a_command(
    hass: HomeAssistant, fake_controller: FakeController, ndb_entry: MockConfigEntry, effect: str | None
) -> None:
    # Live, a scheduled turn_off landed while the 30 s poll was reading the old page; the stale read won.
    fake_controller.page_delay = 0.2
    poll = hass.async_create_task(_poll(ndb_entry))
    await asyncio.sleep(0.05)
    fake_controller.page_delay = 0.0
    await (_off(hass) if effect is None else _select(hass, effect))
    await poll
    await hass.async_block_till_done()

    expected = (STATE_OFF, STATE_UNKNOWN) if effect is None else (STATE_ON, effect)
    assert (_state(hass, MAIN).state, _state(hass, SCENE).state) == expected


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
    assert _state(hass, MAIN).state == STATE_UNAVAILABLE
    assert _state(hass, OUT1).state == STATE_OFF  # the DDP lights don't depend on the HTTP UI

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)  # polls run as background tasks
    assert _state(hass, MAIN).state == STATE_UNAVAILABLE
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
    assert _state(hass, MAIN).attributes["colors"] == [FIXTURE_COLORS[0], [255, 0, 0], EMPTY, EMPTY, EMPTY]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=62))
    await hass.async_block_till_done(wait_background_tasks=True)
    scene = _state(hass, SCENE)
    assert (_state(hass, MAIN).state, scene.state, scene.attributes["options"]) == (STATE_ON, "Glow", EFFECTS)
    assert any("recovered" in r.getMessage() for r in caplog.records)  # logged once when it is back
    assert _state(hass, OUT1).state == STATE_OFF  # scene mode: the DDP lights stay off

    # A command that succeeds after a failed poll logs the same recovery as a successful poll would.
    fake_controller.offline = True
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=93))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _state(hass, MAIN).state == STATE_UNAVAILABLE
    caplog.clear()
    scenes = entry.runtime_data.scenes
    assert scenes is not None and scenes.data is not None
    scenes.publish(scenes.data)
    assert _state(hass, MAIN).state == STATE_ON
    assert sum("recovered" in r.getMessage() for r in caplog.records) == 1
    scenes.publish(scenes.data)  # already up: no second message
    assert sum("recovered" in r.getMessage() for r in caplog.records) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def _stale_last_scene(hass: HomeAssistant) -> None:
    # A remembered scene the controller no longer lists (its shows changed while HA was down).
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    entry.runtime_data.scenes.last_effect = "Strobe"
    await _call(hass, SERVICE_TURN_ON, MAIN)


async def _set_colors(hass: HomeAssistant) -> None:
    await _call(hass, "set_colors", MAIN, domain=DOMAIN, colors=["warm_white"])


async def _set_intensity(hass: HomeAssistant) -> None:
    await _call(hass, SERVICE_TURN_ON, MAIN, brightness=102)


async def _set_slot(hass: HomeAssistant) -> None:
    await _call(hass, SERVICE_TURN_ON, COLORS[0], rgb_color=(255, 0, 0))


async def _pattern_on_slot(hass: HomeAssistant) -> None:
    await _call(hass, "set_pattern", COLORS[0], domain=DOMAIN, colors=[[255, 0, 0]])


@pytest.mark.parametrize(
    ("action", "offline", "error", "out1_after"),
    [
        (_stale_last_scene, False, ServiceValidationError, STATE_ON),
        # A controller that never took the command must not cost the DDP look.
        (_turn_on_scene, True, HomeAssistantError, STATE_ON),
        (_recall_preset, True, HomeAssistantError, STATE_ON),
        (_off, True, HomeAssistantError, STATE_ON),
        (_set_colors, True, HomeAssistantError, STATE_ON),  # the palette is not DDP's: OUT1 stays on
        (_set_slot, True, HomeAssistantError, STATE_ON),
        (_set_intensity, True, HomeAssistantError, STATE_ON),  # a setting: DDP keeps the pixels
        (_pattern_on_slot, False, ServiceValidationError, STATE_ON),
    ],
    ids=[
        "unknown-last-scene",
        "turn_on-down",
        "recall-down",
        "turn_off-down",
        "set_colors-down",
        "color-slot-down",
        "intensity-down",
        "pattern-on-color-slot",
    ],
)
async def test_errors(
    hass: HomeAssistant,
    fake_controller: FakeController,
    ndb_entry: MockConfigEntry,
    action: Callable[[HomeAssistant], Awaitable[None]],
    offline: bool,
    error: type[Exception],
    out1_after: str,
) -> None:
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(0, 255, 0))
    fake_controller.requests.clear()
    fake_controller.offline = offline
    with pytest.raises(error) as raised:
        await action(hass)
    if error is ServiceValidationError:
        assert fake_controller.requests == []
    else:
        assert not isinstance(raised.value, ServiceValidationError)
    if offline:
        assert raised.value.translation_key == "controller_failed"
    assert _state(hass, OUT1).state == out1_after


SCENE_COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255)]


async def _apply_scene_palette(hass: HomeAssistant) -> None:
    """Color 1-5 set at once, as Home Assistant applies a scene."""
    await asyncio.gather(
        *(_call(hass, SERVICE_TURN_ON, light, rgb_color=rgb) for light, rgb in zip(COLORS, SCENE_COLORS, strict=True))
    )


async def _scene_stops(hass: HomeAssistant, entry: MockConfigEntry, fake_controller: FakeController) -> None:
    fake_controller.select("Off")
    await _poll(entry)


async def _unload(hass: HomeAssistant, entry: MockConfigEntry, fake_controller: FakeController) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)


async def _go_offline(hass: HomeAssistant, entry: MockConfigEntry, fake_controller: FakeController) -> None:
    fake_controller.offline = True


async def _another_scene(hass: HomeAssistant, entry: MockConfigEntry, fake_controller: FakeController) -> None:
    await _turn_on_scene(hass)


async def _controller_off(hass: HomeAssistant, entry: MockConfigEntry, fake_controller: FakeController) -> None:
    await _off(hass)


async def _preset(hass: HomeAssistant, entry: MockConfigEntry, fake_controller: FakeController) -> None:
    await _recall_preset(hass)


async def _preset_read_back_fails(hass: HomeAssistant, entry: MockConfigEntry, fake_controller: FakeController) -> None:
    with patch.object(entry.runtime_data.controller, "state", side_effect=ControllerError("gone")):
        await _recall_preset(hass)


async def _ddp_light_takes_over(hass: HomeAssistant, entry: MockConfigEntry, fake_controller: FakeController) -> None:
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(0, 0, 255))


async def _set_slot_unavailable(hass: HomeAssistant) -> None:
    """Color 1 set while its entity is unavailable (the controller was never read), straight through the commands."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await entry.runtime_data.commands.set_color(COLORS[0], 1, (255, 0, 0))


RED_SLOT_1 = [_color(1, "#ff0000")]


@pytest.mark.parametrize(
    ("running", "unread", "action", "writes", "meanwhile", "restarted", "selected"),
    [
        ("Glow", False, _set_slot, RED_SLOT_1, None, True, "Glow"),
        (
            "Glow",
            False,
            _apply_scene_palette,
            [_color(i, "#" + bytes(c).hex()) for i, c in enumerate(SCENE_COLORS, 1)],
            None,
            True,
            "Glow",
        ),
        (
            "Glow",
            False,
            _set_colors,
            [_color(1, "#ffffff"), *(_color(i, "#000000") for i in range(2, 6))],
            None,
            True,
            "Glow",
        ),
        ("Off", False, _set_slot, RED_SLOT_1, None, False, "Off"),  # DDP mode: no scene to restart
        ("Glow", False, _set_slot, RED_SLOT_1, _scene_stops, False, "Off"),  # the effect is read when it is due
        ("Glow", False, _set_slot, RED_SLOT_1, _unload, False, "Glow"),  # nothing outlives the entry
        ("Glow", False, _set_slot, RED_SLOT_1, _go_offline, False, "Glow"),  # logged: no caller to raise to
        # A command that sets the effect inside the window drops the pending restart.
        ("Glow", False, _set_slot, RED_SLOT_1, _another_scene, False, "Show1"),
        ("Glow", False, _set_slot, RED_SLOT_1, _controller_off, False, "Off"),
        ("Glow", False, _set_slot, RED_SLOT_1, _preset, False, "Show1"),
        ("Glow", False, _set_slot, RED_SLOT_1, _preset_read_back_fails, False, "Show1"),
        ("Glow", False, _set_slot, RED_SLOT_1, _ddp_light_takes_over, False, "Off"),
        # Never read at setup: the mode stays unknown, so no restart, even after the read-back sees a scene.
        ("Glow", True, _set_slot_unavailable, RED_SLOT_1, None, False, "Glow"),
    ],
    ids=[
        "slot",
        "ha-scene",
        "set_colors",
        "ddp-mode",
        "scene-stopped",
        "unloaded",
        "restart-fails",
        "another-scene",
        "controller-off",
        "preset",
        "preset-read-back-fails",
        "ddp-light",
        "unread-at-setup",
    ],
)
async def test_a_palette_change_restarts_the_running_scene_once(
    hass: HomeAssistant,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    caplog: pytest.LogCaptureFixture,
    running: str,
    unread: bool,
    action: Callable[[HomeAssistant], Awaitable[None]],
    writes: list[tuple[str, str, bytes]],
    meanwhile: Callable[[HomeAssistant, MockConfigEntry, FakeController], Awaitable[None]] | None,
    restarted: bool,
    selected: str,
) -> None:
    """A running show keeps some old colors until it is selected again (seen on the NDB-Pro): one re-select after
    the last palette write, however many writes a change makes, unless a command set the effect meanwhile."""
    fake_controller.select(running)
    fake_controller.offline = unread
    entry = entry_factory(controller=fake_controller)
    await _setup(hass, entry)
    fake_controller.offline = False
    runtime = entry.runtime_data
    fake_controller.requests.clear()

    await action(hass)
    assert sorted(fake_controller.commands()) == sorted(writes)  # concurrent writes land in any order
    if meanwhile is not None:
        await meanwhile(hass, entry, fake_controller)
    before, mode = len(fake_controller.commands()), runtime.scene_mode
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert fake_controller.commands()[before:] == ([_control(f'{{"fx":"{running}"}}')] if restarted else [])
    assert fake_controller.selected() == selected
    assert (f"Could not restart the scene on {entry.title}" in caplog.text) == (meanwhile is _go_offline)
    assert runtime.scene_mode is mode  # the restart changes no mode
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def _slow_scene_palette(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """An HA scene setting Color 1-5 while each write takes longer than the restart delay."""
    controller = entry.runtime_data.controller
    write = controller.set_color

    async def slow(*args: Any) -> None:
        await asyncio.sleep(0.4)
        await write(*args)

    with patch.object(controller, "set_color", slow):
        await _apply_scene_palette(hass)


async def _two_writes_apart(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A second write 0.2 s after the first pushes the restart back."""
    await _set_slot(hass)
    await asyncio.sleep(0.2)
    await _call(hass, SERVICE_TURN_ON, COLORS[1], rgb_color=(0, 255, 0))


@pytest.mark.parametrize("change", [_slow_scene_palette, _two_writes_apart], ids=["slow-writes", "push-back"])
async def test_the_scene_restarts_once_after_the_last_palette_write(
    hass: HomeAssistant,
    fake_controller: FakeController,
    ndb_entry: MockConfigEntry,
    change: Callable[[HomeAssistant, MockConfigEntry], Awaitable[None]],
) -> None:
    """Real time: the restart is due 0.3 s after the last write, however long the writes took or how far apart."""
    fake_controller.select("Glow")
    await _poll(ndb_entry)
    fake_controller.requests.clear()
    restart = _control('{"fx":"Glow"}')

    await change(hass, ndb_entry)
    await asyncio.sleep(0.2)
    assert restart not in fake_controller.commands()
    await asyncio.sleep(0.3)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [c for c in fake_controller.commands() if c == restart] == [restart]
    assert fake_controller.commands()[-1] == restart


async def test_recall_preset_read_back_failure_marks_the_controller_unavailable(
    hass: HomeAssistant, udp_listener: Listener, fake_controller: FakeController, ndb_entry: MockConfigEntry
) -> None:
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(255, 0, 0))  # DDP mode: the controller is Off
    # The preset was recalled; only the read that follows fails. The action succeeds and Power says it is unsure.
    with patch.object(ndb_entry.runtime_data.controller, "state", side_effect=ControllerError("gone")):
        await _recall_preset(hass)
    assert _state(hass, MAIN).state == STATE_UNAVAILABLE
    assert _state(hass, OUT1).state == STATE_OFF
    # The controller took the preset, so a scene runs: a DDP light turned on now turns the controller off first.
    fake_controller.requests.clear()
    await _call(hass, SERVICE_TURN_ON, OUT1, rgb_color=(0, 255, 0))
    assert fake_controller.commands() == [_control('{"fx":"Off"}')]
    assert _state(hass, OUT1).state == STATE_ON


@pytest.mark.parametrize("host", ["127.0.0.1", "not a host"], ids=["silent", "host-the-library-rejects"])
async def test_http_layout_and_detection_of_an_unusable_controller(fake_controller: FakeController, host: str) -> None:
    fake_controller.offline = True
    assert await fetch_layout("ndbpro", host, fake_controller.port) is None
    assert await detect(host, fake_controller.port) == "generic"


@pytest.mark.parametrize(
    ("service", "data"), [("recall_preset", {"preset": 3}), ("set_colors", {"colors": ["warm_white"]})]
)
@pytest.mark.parametrize("target", [OUT1, ALL, COLORS[0]], ids=["output", "all-outputs", "color-slot"])
async def test_controller_actions_are_refused_off_the_main_light(
    hass: HomeAssistant,
    fake_controller: FakeController,
    ndb_entry: MockConfigEntry,
    service: str,
    data: dict[str, Any],
    target: str,
) -> None:
    with pytest.raises(ServiceValidationError):
        await _call(hass, service, target, domain=DOMAIN, **data)
    assert fake_controller.commands() == []


async def test_stale_entities_are_removed(
    hass: HomeAssistant, fake_controller: FakeController, entry_factory: Callable[..., MockConfigEntry]
) -> None:
    """0.4.1's Mode selects, Power switch and Intensity number are gone; setup drops them and keeps Scene."""
    entry = entry_factory(controller=fake_controller)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    stale = [
        registry.async_get_or_create(domain, DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry)
        for domain, key in [("select", "mode_1"), ("switch", "power"), ("number", "intensity")]
    ]
    await _setup(hass, entry)
    assert [registry.async_get(e.entity_id) for e in stale] == [None] * 3
    assert registry.async_get(SCENE) is not None
    assert registry.async_get(MAIN) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
