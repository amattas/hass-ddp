# hass-ddp

Home Assistant custom integration (`custom_components/ddp`, domain `ddp`) that exposes one DDP pixel controller as a
device with one light entity per segment.

## Protocol lives in pyddp

All DDP code (header encoding, chunking, color pipeline, brightness cap, effects, discovery) lives in
[pyddp](https://github.com/amattas/pyddp). This repo builds no bytes. Import only from top-level `pyddp`, never from
its submodules. If something is missing or wrong in the protocol layer, fix it in pyddp, not here.

## Device communication lives in libraries

HA rule: no device/API code in the integration. NDB-Pro HTTP control lives in
[pyminleon](https://github.com/amattas/pyminleon). `controller.py` is glue only: the vendor-neutral `Controller`
protocol, `CONTROLLER_ERRORS`, and the `CONTROLLER_TYPES` registry (Generic DDP has no controller; adding a vendor is
one registry entry). See `DECISIONS.md`.

## Layout

- `__init__.py`: `DDPRuntime` (`entry.runtime_data`): one `Strip`, one open `DDPSender`, segments, fps, runners, and the
  `Controller` built from the entry's stored `controller_type` (`None` for Generic); `async_migrate_entry` (1 -> 2)
- `controller.py`: controller type registry, `Controller` protocol, `detect`, `create_controller`, `fetch_layout`
- `config_flow.py`: host -> `pyddp.discover` + `detect` -> controller type step (detected preselected, Generic always
  offered) -> entry, or manual fallback; reconfigure changes the type; options flow (cap, color order, fps, keepalive)
- `light.py`: `DDPSegmentLight` (`LightEntity` + `RestoreEntity`), RGB color mode, pyddp effects; `ScenesLight`
  (a vendor controller's built-in effects, polled, `ddp.recall_preset`), which hands the pixels back from DDP by turning
  segment lights off without sending and calling `DDPSender.release()`
- Design spec: `docs/superpowers/specs/2026-10-28-hass-ddp-design.md` (this release is a smaller cut of it)

## Rules

- Setup and unload never send frames; only explicit light calls and state restore do. Setup only reads the controller's
  state; it never commands it, and it never probes (the stored type decides)
- Effect runners are the only background tasks and must all stop on unload
- `pyddp` and `pyminleon` are pinned by git commit in `manifest.json` and `requirements.test.txt`; keep them identical

## Development

Conda env `hass-ddp`:

```bash
python -m pytest -q
ruff check .
black --check .
mypy
```

Tests use a real loopback UDP listener (`tests/conftest.py`) and assert DDP bytes, comparing against
`pyddp.process_frame` rather than hardcoded values. `fake_controller` is a loopback NDB-Pro HTTP UI serving
`tests/fixtures/ndbpro/` chunked like the firmware; commands change the pages so polls read them back.
