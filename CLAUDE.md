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
  `ddp.recall_preset` / `ddp.set_colors` (light domain, the controller light); `DDPRuntime` (`entry.runtime_data`): one
  `Strip`, one open `DDPSender` (every frame a burst of `FRAME_REPEATS` frames `FRAME_REPEAT_INTERVAL` apart, const.py;
  tests/conftest.py sets 1 unless a test is marked `frame_repeats`), segments, fps, runners, `sync_holds()` (releases with `drain=True` when no DDP light is on; the byte ranges keepalive resends follow the
  DDP lights' state, read from `DDPSender.held`), the `Controller` built from the entry's stored `controller_type`
  (`None` for Generic), its `DDPScenesCoordinator` and `ControllerCommands`; `async_migrate_entry` (1 -> 2).
  `PLATFORMS` = light, select
- `commands.py`: `ControllerCommands`, the controller commands shared by the controller device's entities (turn on
  with an effect, turn off = the controller's Off, recall a preset, set the intensity, the palette or one slot). Scene
  mode and DDP mode are exclusive (DECISIONS.md 2026-11-19): a command that starts a scene turns every DDP light off
  once the controller took it (`_hand_back`, nothing sent, nothing held); `follow_poll` (the coordinator's `on_poll`
  hook) does the same for a scene started outside HA; `leave_scene` turns the controller Off before a DDP light
  drives pixels (best effort when the mode is unknown). `runtime.scene_mode` (True/False/None) is the DDP lights' model
  of the controller, set by setup, polls and commands (scene mode is entered only under `ownership`) (a taken scene command counts even if its
  read-back fails). Everything runs under the coordinator lock and publishes the result
- `coordinator.py`: `DDPScenesCoordinator` (30 s poll of the controller state and palette); `lock` serializes polls and
  commands, so a stale poll cannot undo a command; `on_poll`; `last_effect`. `diagnostics.py`: redacted entry, sender,
  and controller documents
- `entity.py`: `DDPEntity` base (unique IDs; `device_info` picks the device, the DDP device by default). Devices: the
  DDP device `(DOMAIN, host)`, named "<title> Generic DDP" (the title alone for Generic), with All outputs; with more
  than one output one device per output `(DOMAIN, host:<segment key>)` (`output_identifier`, `output_device_info`),
  named "<title> <output name>", its light the main entity and its pixel lights "Light N" (`register_output_device`:
  a newly created one takes the DDP device's area and user-set name + output name; existing or restored ones untouched); for a vendor entry the
  controller device `(DOMAIN, host:controller)` for the controller light, Color 1-5 and Scene. Child devices link by
  `via_device_id` (`child_device_info`); `ControllerEntity` is the controller entities' coordinator-entity base.
  `DeviceInfo` is built in one call (since HA 2026.11 it is not a dict)
- `controller.py`: controller type registry, `Controller` protocol, `detect`, `create_controller`, `fetch_layout`
- `config_flow.py`: host -> `aioddp.discover` + `detect` -> controller type step (detected preselected, Generic always
  offered) -> for every type the manual steps (prefilled from the detected layout if any, keeping its title and
  device info): pixel type + number of outputs, then one `output` step per output (name, 1-based first/last pixel;
  gaps allowed, overlaps refused; pixel count = highest last pixel; a prefilled output keeps its segment key; a 0.6.0
  `reversed` flag in stored segments is ignored and no longer written);
  reconfigure changes host/type, re-reads the layout for a vendor type, and walks the same manual/output steps,
  prefilled from the read layout (stored names kept per key, its device info stored) or else the stored segments;
  options flow (cap, color order, fps, keepalive, `pixel_lights`)
- `light.py`: `DDPSegmentLight` (`LightEntity` + `RestoreEntity`), RGB or RGBW color mode from the entry's data type,
  aioddp effects, `ddp.set_pattern` (kept in restore state); a `source` of `off`/`ddp`; every change runs under
  `_drive`: in DDP mode (`runtime.scene_mode` False) only `ownership`, never waiting on the controller; otherwise the
  coordinator lock, `leave_scene`, then `ownership`; restore sends nothing in scene mode and syncs the holds; `AllOutputsLight` over the whole strip when there is more than one segment (DDP lights
  that share pixels hand them to each other without sending); `PaletteLight` "Color 1".."Color 5", one palette slot
  each: RGB, or color temperature (2700 K warm, 6500 K cool) while it holds a white. `PixelLight` (option
  `pixel_lights`): one per pixel of every output, Light N = DDP pixel `start + N - 1` (the controller applies a port
  reverse itself), "Light N" on its output's device (`OutputPixelLight`: "<output> Light N" on the DDP device when
  there is one output), unique ID `<segment key>_pixel<N>`, no effects or patterns; it never turns its output off (freezes an effect)
  (DECISIONS.md 2026-11-18). Overlap lists are computed once at setup (`link`). Setup removes any light of the entry
  it did not create and any device it no longer has (an output's, the controller's), detaching the lights it keeps
- `light.py` also has `ControllerLight`, the controller device's main entity (no name; brightness = intensity): on = a scene runs, off = the controller's
  Off; `colors`/`background` attributes, `last_effect` restore, `ddp.recall_preset`, `ddp.set_colors`.
  `select.py`: `SceneSelect` (the controller's effects without Off).
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
