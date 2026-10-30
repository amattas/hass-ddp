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

- `__init__.py`: `async_setup` registers the `ddp.recall_preset` entity service; `DDPRuntime` (`entry.runtime_data`):
  one `Strip`, one open `DDPSender`, segments, fps, runners, the `Controller` built from the entry's stored
  `controller_type` (`None` for Generic) and its `DDPScenesCoordinator`; `async_migrate_entry` (1 -> 2)
- `coordinator.py`: `DDPScenesCoordinator` (30 s poll of the controller); `lock` serializes polls and commands, so a
  stale poll cannot undo a command. `entity.py`: `DDPEntity` base (device info, unique IDs). `diagnostics.py`: redacted entry, sender, and controller documents
- `controller.py`: controller type registry, `Controller` protocol, `detect`, `create_controller`, `fetch_layout`
- `config_flow.py`: host -> `aioddp.discover` + `detect` -> controller type step (detected preselected, Generic always
  offered) -> entry, or manual fallback; reconfigure changes the type; options flow (cap, color order, fps, keepalive)
- `light.py`: `DDPSegmentLight` (`LightEntity` + `RestoreEntity`), RGB color mode, aioddp effects; `ScenesLight`
  (a coordinator entity for a vendor controller's built-in effects, `ddp.recall_preset`), which hands the pixels back from DDP by turning
  segment lights off without sending and calling `DDPSender.release()`
- Design spec: `docs/superpowers/specs/2026-10-28-hass-ddp-design.md` (this release is a smaller cut of it)

## Rules

- `quality_scale.yaml` is the honest status of every Integration Quality Scale rule; update it when behaviour changes
- Every raised `HomeAssistantError`/`ConfigEntryError`/`UpdateFailed` uses a `translation_key` present in `strings.json`
  and `translations/en.json` (`tests/test_translations.py` checks); keep `icons.json` in step with entities and actions

- Setup and unload never send frames; only explicit light calls and state restore do. Setup only reads the controller's
  state; it never commands it, and it never probes (the stored type decides)
- Effect runners are the only background tasks and must all stop on unload
- `aioddp` and `pyminleon` are pinned by git commit in `manifest.json` and `requirements.test.txt`; keep them identical

## Development

Conda env `hass-ddp`:

```bash
python -m pytest -q --cov=custom_components.ddp --cov-report=json
python scripts/check_coverage.py   # every module at least 95%
ruff check .
black --check .
mypy
```

Tests use a real loopback UDP listener (`tests/conftest.py`) and assert DDP bytes, comparing against
`aioddp.process_frame` rather than hardcoded values. `fake_controller` is a loopback NDB-Pro HTTP UI serving
`tests/fixtures/ndbpro/` chunked like the firmware; commands change the pages so polls read them back.
