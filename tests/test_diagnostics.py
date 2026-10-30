"""Diagnostics: useful state with the host, MAC, and network settings redacted."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.ddp.diagnostics import redact_controller_document

from .conftest import FakeController


@pytest.mark.parametrize("variant", ["ndbpro", "generic", "controller-down"])
async def test_diagnostics_redacts_identifying_data(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_controller: FakeController,
    entry_factory: Callable[..., MockConfigEntry],
    variant: str,
) -> None:
    fake_controller.offline = variant == "controller-down"
    entry = entry_factory(controller=fake_controller, controller_type="generic" if variant == "generic" else None)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics["entry"]["data"]["host"] == REDACTED
    assert diagnostics["entry"]["data"]["segments"][0]["length"] == 80
    assert diagnostics["strip"] == {
        "pixel_count": 122,
        "data_type": "RGB",
        "color_order": "RGB",
        "brightness_cap": 0.6,
        "host": REDACTED,
    } | {"port": diagnostics["strip"]["port"]}
    assert diagnostics["sender"]["is_open"] is True
    assert "last_frame" not in diagnostics["sender"]["stats"]
    assert diagnostics["runners"] == []
    if variant == "ndbpro":
        assert diagnostics["scenes"]["effect"] == "Glow"
        controller = diagnostics["controller"]
        status, config = controller["status"]["status"], controller["config"]["config"]
        assert status["mac"] == config["ssid"] == config["ip"] == config["gw"] == REDACTED
        assert status["mod"] == "NDBPRO"
        assert status["name"] == REDACTED  # user-set, may be a house name
    elif variant == "generic":
        assert diagnostics["scenes"] is None
        assert diagnostics["controller"] is None
    else:
        assert diagnostics["scenes"]["available"] is False
        assert diagnostics["controller"] == {"error": "ControllerError"}
        serialized = json.dumps(diagnostics)
        assert "127.0.0.1" not in serialized
        assert str(fake_controller.port) not in serialized
    assert "5c:01:3b" not in str(diagnostics)
    assert "SkyDevices" not in str(diagnostics)
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_controller_documents_are_allowlisted() -> None:
    document = {
        "config": {"proto": 0, "newfield": "x", "pass": "s", "ports": [{"p": 0, "l": 80, "label": "Garage"}]},
        "status": {"mod": "NDBPRO", "name": "Smith house", "vin": "24.27"},
    }
    assert redact_controller_document(document) == {
        "config": {"proto": 0, "newfield": REDACTED, "pass": REDACTED, "ports": [{"p": 0, "l": 80, "label": REDACTED}]},
        "status": {"mod": "NDBPRO", "name": REDACTED, "vin": "24.27"},
    }
