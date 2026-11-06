# Decisions

## 2026-10-30: controller types and the pyminleon split

- pyddp is DDP-only: no HTTP client, no vendor code. `discover()` is the DDP query alone; `parse_config` stays public.
- Minleon NDB-Pro code moves to a new library, `pyminleon` (public repo amattas/pyminleon, Apache-2.0, zero deps).
  It also owns reading the NDB-Pro layout over HTTP (raw `/api/config` and `/api/status` documents).
- Device/API code never lives in hass-ddp (HA rule: device communication belongs in a library).
- hass-ddp owns the vendor-neutral `Controller` protocol and a config-flow "Controller type" step: detected type
  preselected, "Generic DDP" always offered. Stored in entry data; entries migrated.
- pyddp changes go on the existing branch `feature/ha-effect-color`; committed locally, pushed only on request.

## 2026-10-30: pyddp renamed to aioddp

- The `pyddp` name on PyPI belongs to an unrelated 2015 Meteor DDP client; `aioddp` is free and follows the asyncio naming convention (aiohttp, aiohue).
- The exception base class is now `DDPError` (was `PyDDPError`); the other exception names are unchanged.
- The GitHub repo is renamed to amattas/aioddp; the dependency pin is now `aioddp @ git+https://github.com/amattas/aioddp.git@<sha>`.

## 2026-10-30: shared CI, ruff, and requirement pins

- `aioddp` and `pyminleon` stay pinned by git commit (`pkg@git+https://...@<sha>`, no spaces: hassfest rejects requirements containing a space) until their first PyPI releases, then switch to `==` PyPI pins in `manifest.json` and `pyproject.toml`.

## 2026-11-05: first field test feedback (patterns, controller effects, both outputs, white)

Findings from the live NDB-Pro (diagnostics): entry is `rgb`, controller input conversion `conv=13` (3-channel RGB
in, controller derives white for its RGBW+ pixels); the Scenes light already lists the controller's 17 effects, but
the controller's 6-slot color palette (`/pcolors.html`, with Cool White / Warm White presets) is not exposed.

- Multi-color DDP patterns: a `ddp.set_pattern` entity action on segment lights (list of colors + block size, rendered
  by aioddp's `palette` effect, kept in restore state). No fixed "alternate"-style effects.
- aioddp effects: expose their parameters through the same action; new effects are a later phase, chosen by name.
- Both outputs together: a built-in "All outputs" light spanning the whole strip (one DDP frame, patterns continue
  across outputs), not HA's Light Group helper.
- Controller effects and palette: two HA devices under one entry, the controller (Scenes light + its 6 palette colors,
  via a `ddp.set_colors` action, current palette as attributes; `via_device` the DDP device) and the DDP output device
  (segment lights, All outputs). Palette read/write is added to pyminleon (it still never saves to flash).
- White channel: the user switches the NDB-Pro to a 4-channel input mode in its UI and reconfigures the entry;
  segment lights then get HA's RGBW color mode. Solid colors and patterns carry W; animated aioddp effects stay RGB.
  Reconfigure re-reads the layout so the data type and segment slots follow the controller.
- Implementation notes (2026-11-05): the controller device links to the DDP device with `via_device_id` (HA 2026.10
  deprecates `via_device`), so the Scenes light of a new entry is `light.<title>_<type label>_scenes`; existing
  entities keep their IDs. The NDB-Pro's Warm White preset sends `#ffffff` and Cool White `#feffff` (the gold swatch
  is UI only); pyminleon exposes both as constants and hass-ddp as the named colors `warm_white` / `cool_white`.
  Light setup removes any light of the entry it did not create (dropped output, All outputs with one segment left,
  Scenes on Generic). A pattern color with W is refused before aioddp sees it on an RGB entry or a non-RGBW effect.
