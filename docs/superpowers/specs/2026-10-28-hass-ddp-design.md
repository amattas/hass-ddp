# hass-ddp design spec

Date: 2026-10-28
Status: Draft
Companion: `aioddp/docs/superpowers/specs/2026-10-28-aioddp-design.md`

## Summary

hass-ddp is a HACS custom integration (`custom_components/ddp`) that turns one DDP pixel controller into one Home Assistant device with one light entity per segment. It depends on aioddp via `manifest.json` and contains no protocol code, the same split as hass-dmp over pydmp.

First target is a Minleon NDB-Pro on a roofline (roofline segment plus a garage peak segment), with no Minleon-specific code.

### What this is NOT

- Not a show player. xLights or FPP handle sequenced shows
- Not a DDP receiver or E1.31/Art-Net bridge
- Not audio-reactive

## Goals

- Native HA light entities: on/off, brightness, RGB, color temperature, effect list, transitions
- Segments as separate entities on one controller
- UI-only setup through config and options flows
- Survives HA restarts by restoring state and re-sending it
- Never stalls the HA event loop

## Integration metadata

```json
{
  "domain": "ddp",
  "name": "DDP Pixel Controller",
  "codeowners": ["@amattas"],
  "config_flow": true,
  "documentation": "https://amattas.github.io/hass-ddp/",
  "integration_type": "device",
  "iot_class": "assumed_state",
  "requirements": ["aioddp==0.1.0"],
  "version": "0.1.0"
}
```

`iot_class` is `assumed_state` because DDP is send-only with no read-back. HA's state is the truth.

## Config flow

- User step: name, host, pixel count, data type (RGB or RGBW), color order
- Validation: host resolves and the UDP socket opens, then one all-off frame is sent. UDP gives no ack, so this cannot prove the controller is there; the flow says so
- Unique ID: host (one entry per controller)
- No discovery in v1. DDP has a query/reply mechanism but receiver support is spotty

## Options flow

- Segments: add, edit, remove (name, start pixel, length, reverse). Default is one segment covering the whole string
- Brightness cap, percent of full output, default 60
- Effect fps cap, default 30
- Gamma on/off (default per aioddp's finding for the controller)
- Keepalive interval, off by default

Any options change reloads the entry.

## Runtime

| Piece | Role |
|---|---|
| `DDPRuntime` (`entry.runtime_data`) | Owns one `aioddp.DDPSender`, the strip config, and one `EffectRunner` slot per segment |
| `DDPSegmentLight` | `LightEntity` + `RestoreEntity` per segment |
| Device | One per controller; segments are its entities |
| Diagnostics | Config (host redacted only if asked), segment map, last frame time, packets sent, send errors |

### Light entity

- Color modes: `rgb` and `color_temp` for RGB strips; `rgbw` and `color_temp` for RGBW
- `effect_list` built from the aioddp effect registry; `None` effect means solid color
- Supports `transition` for solid color and brightness changes (rendered as a short fade through the runner)
- `assumed_state = True`, `should_poll = False`
- Unique ID: `<entry_id>_<segment_name>`; renaming a segment in options keeps the entity if the slug is unchanged

### Behavior

- Solid colors send once on change, plus keepalive if enabled
- Effects run as an `EffectRunner` task, cancelled on any state change, entity removal, or entry unload
- Turning off sends zeros for that segment's range and stops its runner
- All sends on the event loop, no executor threads. aioddp's render-time budget is the guarantee that effects don't stall HA
- Only one sender per controller. If something else (FPP, xLights) is driving the NDB-Pro, hass-ddp's entities must be off first; this is documented, not detected

## Lifecycle

- `async_setup_entry`: build the runtime, open the sender, forward to the light platform; entities restore last state and re-send it
- `async_unload_entry`: stop all runners, close the socket. Does not blank the lights, so a reload doesn't flash the house
- `async_remove_entry`: nothing extra; turning lights off is the user's call

## Services

None in v1. Everything goes through standard `light.turn_on` / `light.turn_off` with `effect`, `rgb_color`, `color_temp_kelvin`, `brightness`, `transition`. A `ddp.set_effect_params` service is a candidate for later if effect params need tuning from automations.

## Testing

- `pytest-homeassistant-custom-component`, `pytest.ini` with `asyncio_mode = auto` like hass-dmp
- Config flow: success, bad host, duplicate entry
- Options flow: add, edit, remove segment, validation of overlap and range
- Entity: turn on solid, change color, set effect, turn off, restore after restart
- Runner: started on effect, cancelled on state change and on unload, no orphaned tasks
- aioddp sender mocked at the boundary; one integration test against a local UDP listener to assert real bytes
- `requirements.test.txt` pins aioddp exactly, matching `manifest.json`

## Repo conventions (mirror hass-dmp)

- `custom_components/ddp/` with `__init__.py`, `config_flow.py`, `const.py`, `light.py`, `diagnostics.py`, `manifest.json`, `strings.json`, `translations/en.json`
- `custom_components/quality_scale.yaml` tracking Bronze rules, target Bronze for 0.1.0
- `hacs.json` (`homeassistant` minimum set to current stable at scaffold time, `render_readme: true`)
- `tests/` with `conftest.py`, one test module per area
- ruff in CI; black and mypy aligned with aioddp's config rather than hass-dmp's older pre-commit set
- Docs: Zensical + mike at `https://amattas.github.io/hass-ddp/` with `docs/guide/installation.md`, `controller-configuration.md`, `features.md`
- GitHub: `ci.yml`, `docs.yml`, `hassfest.yaml`, `release.yml` manual-only, `dependabot.yml`
- `CLAUDE.md` and `AGENTS.md` at the root pointing at aioddp for anything protocol-related
- Apache-2.0 license

## Milestones

1. **0.1.0:** config flow, one segment, solid color, brightness, color temp, restore state. Done when the roofline runs on a sunset automation for a week without intervention
2. **0.2.0:** options flow for segments, effects, brightness cap, diagnostics. Done when roofline and peak run independently
3. **Publish:** HACS custom repo, README with an NDB-Pro walkthrough, hassfest green

Out of scope until after school: discovery, custom palette UI, multi-controller sync, `set_effect_params` service, HACS default listing.

## Open questions

- [ ] Exact pixel count on the installed run and the pixel range of the garage peak
- [ ] Which HA host runs it and which VLAN the NDB-Pro is on (UDP 4048 rule needed if it's the IoT VLAN)
- [ ] Should a controller-wide "all segments" light entity exist alongside the per-segment ones, or is an HA light group enough?
- [ ] Domain name: `ddp` is generic; confirm nothing in core or HACS already claims it
