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

- `aioddp` and `pyminleon` stay pinned by git commit (`pkg@git+https://...@<sha>`, no spaces: hassfest rejects requirements containing a space) until their first PyPI releases, then switch to `==` PyPI pins in `manifest.json` and `requirements.test.txt`.
