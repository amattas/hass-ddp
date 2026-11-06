# hass-ddp

Home Assistant custom integration (`custom_components/ddp`, domain `ddp`) that exposes one DDP pixel controller as a
device with one light entity per segment.

## Protocol lives in aioddp

All DDP code (header encoding, chunking, color pipeline, brightness cap, effects, discovery) lives in
[aioddp](https://github.com/amattas/aioddp). This repo builds no bytes. Import only from top-level `aioddp`, never from
its submodules. If something is missing or wrong in the protocol layer, fix it in aioddp, not here.

## Device communication lives in libraries

HA rule: no device/API code in the integration. NDB-Pro HTTP control lives in
[pyminleon](https://github.com/amattas/pyminleon). `controller.py` is glue only: the vendor-neutral `Controller`
protocol, `CONTROLLER_ERRORS`, and the `CONTROLLER_TYPES` registry (Generic DDP has no controller; adding a vendor is
one registry entry). See `DECISIONS.md`.

## Layout

- `__init__.py`: `async_setup` registers the entity services: `ddp.set_pattern` (light domain, the DDP lights) and
  `ddp.recall_preset` / `ddp.set_colors` (switch domain, the Power switch); `DDPRuntime` (`entry.runtime_data`): one
  `Strip`, one open `DDPSender`, segments, fps, runners, `sync_holds()` (the byte ranges keepalive resends follow the
  DDP lights' state, read from `DDPSender.held`), the `Controller` built from the entry's stored `controller_type`
  (`None` for Generic), its `DDPScenesCoordinator` and `ControllerCommands`; `async_migrate_entry` (1 -> 2).
  `PLATFORMS` = light, number, select, switch
- `commands.py`: `ControllerCommands`, the controller commands shared by the controller device's entities (turn on
  with an effect, turn off = the controller's Off plus blanking the DDP-driven segments, recall a preset, set the
  intensity, the palette or one slot) and the slot-to-mode mapping (Color / Warm White / Cool White). A command that
  starts a scene hands the pixels back from DDP once the controller took it; `follow_poll` (the coordinator's
  `on_poll` hook) does the same for changes made outside HA. Everything runs under the coordinator lock and publishes
  the result
- `coordinator.py`: `DDPScenesCoordinator` (30 s poll of the controller state and palette); `lock` serializes polls and
  commands, so a stale poll cannot undo a command; `on_poll`; `last_effect`. `diagnostics.py`: redacted entry, sender,
  and controller documents
- `entity.py`: `DDPEntity` base (unique IDs). Two devices per vendor entry: the DDP device `(DOMAIN, host)`, named
  "<title> Generic DDP" (the title alone for Generic), and the controller device `(DOMAIN, host:controller)`, linked
  by `via_device_id`, for Power, Color/Mode 1-5, Scene and Intensity; `ControllerEntity` is their coordinator-entity
  base. `DeviceInfo` is built in one call (since HA 2026.11 it is not a dict)
- `controller.py`: controller type registry, `Controller` protocol, `detect`, `create_controller`, `fetch_layout`
- `config_flow.py`: host -> `aioddp.discover` + `detect` -> controller type step (detected preselected, Generic always
  offered) -> entry, or manual fallback; reconfigure changes host/type and re-reads the layout; options flow (cap,
  color order, fps, keepalive)
- `light.py`: `DDPSegmentLight` (`LightEntity` + `RestoreEntity`), RGB or RGBW color mode from the entry's data type,
  aioddp effects, `ddp.set_pattern` (kept in restore state); a `source` of `off`/`ddp`/`scene`/`blanked` (see
  DECISIONS.md 2026-11-06); `AllOutputsLight` over the whole strip when there is more than one segment (DDP lights
  that share pixels hand them to each other without sending); `PaletteLight` "Color 1".."Color 5", one palette slot
  each: RGB, or color temperature (2700 K warm, 6500 K cool) while it holds a white. Setup removes any light of the entry it did not create
- `switch.py`: `PowerSwitch`, the controller device's main entity (no name): on = a scene runs, off = the controller's
  Off; `colors`/`background` attributes, `last_effect` restore, `ddp.recall_preset`, `ddp.set_colors`.
  `select.py`: `SceneSelect` (the controller's effects without Off) and `ModeSelect` 1-5. `number.py`:
  `IntensityNumber` (percent, config category)
- Design spec: `docs/superpowers/specs/2026-10-28-hass-ddp-design.md` (this release is a smaller cut of it)

## Rules

- `quality_scale.yaml` is the honest status of every Integration Quality Scale rule; update it when behaviour changes
- Every raised `HomeAssistantError`/`ConfigEntryError`/`UpdateFailed` uses a `translation_key` present in `strings.json`
  and `translations/en.json` (`tests/test_translations.py` checks); keep `icons.json` in step with entities and actions

- Setup and unload never send frames; only explicit light calls and state restore do. Setup only reads the controller's
  state; it never commands it, and it never probes (the stored type decides)
- Effect runners are the only background tasks and must all stop on unload
- `aioddp` and `pyminleon` are pinned by git commit in `manifest.json` and `pyproject.toml` (`dev` extra); keep them identical

## Development

Conda env `hass-ddp`:

```bash
python -m pip install -e ".[dev]"   # test, lint and type-check tools (pyproject.toml)
python -m pytest -q                 # coverage gate: fail_under in pyproject.toml
ruff check .
ruff format --check .
python -m mypy
```

Tests use a real loopback UDP listener (`tests/conftest.py`) and assert DDP bytes, comparing against
`aioddp.process_frame` rather than hardcoded values. `fake_controller` is a loopback NDB-Pro HTTP UI serving
`tests/fixtures/ndbpro/` chunked like the firmware; commands change the pages so polls read them back.
