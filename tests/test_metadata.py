"""hassfest-style sanity: every translation key the code uses exists, and the translation files agree."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import yaml

from custom_components.ddp import const

PACKAGE = Path(__file__).parent.parent / "custom_components" / "ddp"


def _load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((PACKAGE / name).read_text())
    return loaded


def _used_keys() -> dict[str, set[str]]:
    """Translation keys in the source: ``exception`` (translation_key=), ``selector`` and ``entity`` (_attr_)."""
    used: dict[str, set[str]] = {"exception": set(), "selector": set(), "entity": set()}
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.keyword) and node.arg == "translation_key":
                value = node.value
                key = value.value if isinstance(value, ast.Constant) else getattr(const, getattr(value, "id", ""))
                used["exception" if "Selector" not in _enclosing_call(path, node) else "selector"].add(key)
            elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_attr_translation_key" for t in node.targets
            ):
                used["entity"].add(ast.literal_eval(node.value))
    return used


def _enclosing_call(path: Path, keyword: ast.keyword) -> str:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call) and any(
            k.arg == keyword.arg and k.value.lineno == keyword.value.lineno for k in node.keywords
        ):
            return ast.unparse(node.func)
    return ""


def test_translations_match_strings() -> None:
    assert _load("translations/en.json") == _load("strings.json")


def test_every_used_key_is_translated() -> None:
    strings, used = _load("strings.json"), _used_keys()
    assert used["exception"] and used["selector"] and used["entity"]  # the scan found something to check
    assert used["exception"] <= set(strings["exceptions"])
    assert used["selector"] <= set(strings["selector"])
    assert used["entity"] <= set(strings["entity"]["light"])
    assert {"cannot_connect", "invalid_config"} <= set(strings["config"]["error"])  # config flow errors
    assert {"already_configured", "reconfigure_successful"} <= set(strings["config"]["abort"])
    assert "invalid_keepalive" in strings["options"]["error"]
    assert all("message" in strings["exceptions"][k] for k in used["exception"])


def test_icons_and_services_line_up() -> None:
    strings, icons = _load("strings.json"), _load("icons.json")
    services_yaml = yaml.safe_load((PACKAGE / "services.yaml").read_text())
    assert set(services_yaml) == set(strings["services"]) == set(icons["services"])
    for platform, entities in icons["entity"].items():
        assert set(entities) <= set(strings["entity"][platform])
    for service, spec in services_yaml.items():
        assert set(spec["fields"]) == set(strings["services"][service]["fields"])
