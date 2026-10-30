# hass-ddp

Home Assistant custom integration (`custom_components/ddp`, domain `ddp`) that exposes one DDP pixel controller as a
device with one light entity per segment.

## Protocol lives in pyddp

All DDP code (header encoding, chunking, color pipeline, brightness cap, effects, discovery) lives in
[pyddp](https://github.com/amattas/pyddp). This repo builds no bytes. Import only from top-level `pyddp`, never from
its submodules. If something is missing or wrong in the protocol layer, fix it in pyddp, not here.

## Layout

- `__init__.py`: `DDPRuntime` (`entry.runtime_data`): one `Strip`, one open `DDPSender`, segments, fps, runners
- `config_flow.py`: host -> `pyddp.discover`, manual fallback; options flow (cap, color order, fps, keepalive)
- `light.py`: `DDPSegmentLight` (`LightEntity` + `RestoreEntity`), RGB color mode, pyddp effects
- Design spec: `docs/superpowers/specs/2026-10-28-hass-ddp-design.md` (this release is a smaller cut of it)

## Rules

- Setup and unload never send frames; only explicit light calls and state restore do
- Effect runners are the only background tasks and must all stop on unload
- `pyddp` is pinned by git commit in `manifest.json` and `requirements.test.txt`; keep them identical

## Development

Conda env `hass-ddp`:

```bash
python -m pytest -q
ruff check .
black --check .
mypy
```

Tests use a real loopback UDP listener (`tests/conftest.py`) and assert DDP bytes, comparing against
`pyddp.process_frame` rather than hardcoded values.
