# DDP Pixel Controller for Home Assistant

A Home Assistant custom integration that drives DDP (Distributed Display Protocol) pixel controllers, such as the
Minleon NDB-Pro, as native light entities. Each controller is one device with one light per output. Every light
supports on/off, brightness, RGB color, and an effect list, so Home Assistant scenes can capture and replay it.

All protocol code lives in [aioddp](https://github.com/amattas/aioddp), and Minleon NDB-Pro control in
[pyminleon](https://github.com/amattas/pyminleon); this repository only maps them onto Home Assistant.

## Supported devices

| Device | Support |
|---|---|
| Minleon NDB-Pro with Rainmin RGB+ pixels | The reference device. DDP lights plus the Scenes light and `ddp.recall_preset` |
| Any other DDP v1 controller (UDP port 4048) | **Generic DDP**: one light per output, or one light for the whole strip. Not tested beyond the DDP spec |

RGB and RGBW pixels are supported. DDP is send-only, so Home Assistant cannot read a controller's state back over DDP;
see Known limitations. See [aioddp's compatibility notes](https://github.com/amattas/aioddp/blob/main/docs/compatibility.md)
for which controllers have been exercised.

## Supported functions

| Entity | What it does |
|---|---|
| **Output N** light (one per output; **All** for a manually described strip) | On/off, brightness, RGB color, and the aioddp effects. Sends DDP; its state is assumed |
| **Scenes** light (vendor controller types only) | Selects the controller's built-in effects and shows, brightness maps to its intensity. Polled |

Action: `ddp.recall_preset` (NDB-Pro Scenes light only). Home Assistant's `light.turn_on` and `light.turn_off` work on
every light, and scenes capture the segment lights. The integration provides no triggers or conditions.

## Installation

Prerequisites: Home Assistant 2026.10.0 or later, the controller reachable from Home Assistant over UDP (port 4048) and,
for the Scenes light, TCP port 80, and `git` available to Home Assistant, because the two libraries are installed from
pinned git commits (`aioddp` is not yet published to PyPI).

**HACS (custom repository)**

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/amattas/hass-ddp` with type **Integration**.
3. Install **DDP Pixel Controller** and restart Home Assistant.

**Manual**

1. Copy the `custom_components/ddp` folder of this repository into `<config>/custom_components/`.
2. Restart Home Assistant.

## Setup

1. **Settings > Devices & services > Add integration > DDP Pixel Controller.**
2. Enter the controller's IP address or hostname.
3. Home Assistant asks the controller for its output layout (a DDP query, then the controller's own API if its type
   is known to serve one) and checks which kind of controller it is.
4. Pick the **Controller type**. The detected type is preselected; **Generic DDP** is always offered.
5. If a layout was found, one light is created per active output ("Output 1", "Output 2", ...). If not, enter the total
   pixel count and pixel type (RGB or RGBW) and one light covering the whole strip is created.

| Field | Step | Notes |
|---|---|---|
| Host | Add | IP address or hostname. One entry per host; adding the same host again is refused |
| Controller type | Add, Reconfigure | Generic DDP or a vendor type (below) |
| Pixel count | Add (manual only) | Total pixels the controller drives |
| Pixel type | Add (manual only) | RGB or RGBW (channels per pixel) |

DDP is send-only UDP with no acknowledgement, so setup can only check that a socket opens. Setup never sends a frame,
and reloading or removing the integration never blanks the lights.

### Controller types

| Type | Lights |
|---|---|
| Generic DDP | One DDP light per output. Works with any DDP controller; nothing else is created |
| Minleon NDB-Pro | The DDP lights, plus the **Scenes** light and the `ddp.recall_preset` service (see below) |

The stored type decides: nothing probes the controller at startup. Adding another vendor's controller means one entry
in the controller type registry (`custom_components/ddp/controller.py`) backed by that vendor's library.

### Reconfigure

**Reconfigure** on the integration entry changes the host (for example after a new DHCP lease) or the controller type;
the entry reloads and keeps its device, entities, and history. Choosing Generic DDP removes the Scenes light. The
pixel layout is not re-read: to change it, remove and re-add the entry.

### Options

| Option | Default | Notes |
|---|---|---|
| Brightness cap | 60% | Applied to every frame, effects included. Lower it to protect power supplies |
| Color order | RGB / RGBW | Channel order the pixels expect |
| Effect frame rate | 30 fps | 1 to 60 |
| Keepalive interval | 1 s | Re-sends the current frame so the controller doesn't fall back to its built-in effect; 0 turns it off, otherwise at least 0.5 s |

Saving options reloads the entry.

### Removal

1. **Settings > Devices & services > DDP Pixel Controller**, open the entry's menu and choose **Delete**.
2. If you installed with HACS, remove **DDP Pixel Controller** in HACS and restart Home Assistant; for a manual
   install, delete `<config>/custom_components/ddp` and restart.

Removing the entry stops the integration's frames but does not blank the lights; the controller falls back to its own
effect (about 20 s on an NDB-Pro). Turn the lights off first if you want them dark.

## Data updates

- **Segment lights never poll.** DDP has no read path, so Home Assistant's state is the truth: it is restored after a
  restart and, if the light was on, sent again. Sends are local UDP and raise an error in the action if the socket
  fails.
- **The Scenes light polls the controller's web UI every 30 seconds** and again right after each command it sends. If
  the controller does not answer, only the Scenes light becomes unavailable (logged once, and again when it is back);
  the DDP lights keep working.

## Effects

The effect list comes from aioddp: `breathe`, `chase`, `twinkle`, `rainbow`, `palette`. The effect `off` means a solid
color. The light's RGB color drives each effect's main color where it has one (for example the chase color or the
breathe color); brightness scales the whole effect.

## NDB-Pro built-in scenes

A controller of type Minleon NDB-Pro (detected from its status model `NDBPRO`, or chosen by hand) gets one more light
on the same device: **Scenes**. It drives the
controller's own built-in effects over its HTTP UI instead of sending DDP. Entity IDs below assume the device is named
"NDBPRO" (the title discovery gives it unless the controller reports a name), so the lights are
`light.ndbpro_scenes`, `light.ndbpro_output_1`, and so on; check yours on the device page.

- Its effect list is the controller's: `Show1` and `Show2` play the shows stored on the controller; the rest (`Test`,
  `Bands`, `Glow`, ...) are its built-in patterns. Brightness maps to the controller's intensity (0-100%).
- `light.turn_off` selects the controller's `Off` and also blanks any segment light that is on, so the house goes dark.
- A bare `light.turn_on` brings back the last scene, also after a Home Assistant restart. Startup never commands the
  controller; the light polls the controller every 30 s and shows what it is actually running.
- `ddp.recall_preset` plays one of the 20 stored presets (1-10 are slots of Show 1, 11-20 of Show 2).

**Segment lights vs. scenes.** DDP data overrides the controller's built-in effect immediately, and about 20 s after DDP
stops the controller falls back to it. So the two kinds of light take turns:

- Turning on a scene (or recalling a preset) first turns every segment light off without sending black and stops DDP,
  so the scene shows at once.
- Turning a segment light on takes the pixels over again. While any segment light is on, the keepalive (default 1 s)
  keeps re-sending its frame so the controller does not fall back mid-color; with keepalive 0 a solid color reverts to
  the built-in effect after about 20 s.
- When the last segment light turns off, DDP stops; about 20 s later the controller's own scene (or `Off`) is back.

## Examples: scenes on a schedule

Schedule the controller's stored scenes with plain automations against the Scenes light:

```yaml
automation:
  - alias: Show 1 at sunset
    triggers:
      - trigger: sun
        event: sunset
        offset: "-00:15:00"
    actions:
      - action: light.turn_on
        target:
          entity_id: light.ndbpro_scenes
        data:
          effect: Show1
          brightness_pct: 80

  - alias: Holiday preset on weekends
    triggers:
      - trigger: time
        at: "18:00:00"
    conditions:
      - condition: time
        weekday: [sat, sun]
    actions:
      - action: ddp.recall_preset
        target:
          entity_id: light.ndbpro_scenes
        data:
          preset: 3

  - alias: Lights off at 23:00
    triggers:
      - trigger: time
        at: "23:00:00"
    actions:
      - action: light.turn_off
        target:
          entity_id: light.ndbpro_scenes
```

Turning the Scenes light off blanks the DDP segment lights too, so the 23:00 automation is all you need for "off".

Home Assistant scenes still work for the DDP segment lights; a scene captures each light's state (on/off, brightness,
color, effect):

```yaml
scene:
  - name: Roofline warm chase
    entities:
      light.ndbpro_output_1:
        state: "on"
        brightness: 200
        rgb_color: [255, 140, 40]
        effect: chase
      light.ndbpro_output_2:
        state: "on"
        brightness: 180
        rgb_color: [255, 0, 0]
        effect: "off" # solid color; quote it, a bare off is a YAML boolean
```

Activate it from an automation with `scene.turn_on`. After a Home Assistant restart, each segment light restores its
last state and, if it was on, sends it again.

## Use cases

- Run a roofline in a solid warm white most nights and a chase or rainbow effect for holidays, from dashboards or
  scenes.
- Let the NDB-Pro play the shows stored on it on a schedule (sunset to 23:00) while Home Assistant stays in charge of
  the on/off times.
- Take a section of the strip over for a one-off alert color (for example a doorbell flash) and hand it back after.

## Actions

| Action | Target | Fields |
|---|---|---|
| `ddp.recall_preset` | A Scenes light | `preset` (1-20; 1-10 are slots of Show 1, 11-20 of Show 2) |
| `light.turn_on` / `light.turn_off` | Any light of this integration | Segment lights: `brightness`, `rgb_color`, `effect`. Scenes light: `brightness`, `effect` |

`ddp.recall_preset` on a segment light is refused. Recalling a preset overwrites the controller's current colors with
the preset's. Actions raise an error when a send fails, the effect is unknown, or the controller does not answer.

## Diagnostics

**Settings > Devices & services > DDP Pixel Controller > Download diagnostics** includes the entry, the DDP sender's
counters, and, for an NDB-Pro, the controller's config and status documents. The host, IP addresses, MAC address, and
Wi-Fi name are redacted.

## Known limitations

- **One DDP sender per controller.** DDP has no priority or arbitration. If FPP, xLights, or anything else is driving
  the controller, stop it before using these lights, or the two fight frame by frame.
- **DDP takes over the NDB-Pro, and it gives up after about 20 s.** DDP data overrides the controller's built-in
  effect at once; about 20 s after DDP stops it falls back to it, even mid solid color. The keepalive (default 1 s)
  prevents this; with keepalive 0 a solid color reverts after about 20 s.
- **Recalling a preset overwrites the controller's current colors** with the preset's. The integration never writes
  the controller's saved settings.
- **Segment light state is assumed.** Changes made elsewhere (the controller's web UI, another sender) are not seen.
- **Setup cannot verify the controller over DDP.** It only checks that a UDP socket opens; a wrong host looks fine
  until a frame is sent and nothing lights up.
- **The Scenes light needs the controller's HTTP UI on port 80** and only exists for vendor controller types.
- **The layout is read once, when the entry is added.** If you re-patch outputs on the controller, remove and re-add
  the entry.
- **No discovery.** DDP has no discovery protocol, so controllers are added by host.
- **No brand images yet,** so the integration shows Home Assistant's placeholder icon.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Lights do not react | Host is right; UDP 4048 is not blocked between Home Assistant and the controller; nothing else is sending DDP to it; the output is patched on the controller. The brightness cap (default 60%) may also make colors dimmer than expected |
| Colors are wrong (red and green swapped, ...) | Set **Color order** in the options to match the pixels |
| A solid color turns into the controller's own effect after about 20 s | The keepalive is off or too long; set it to 1 s |
| Scenes light is unavailable | The controller's web UI (TCP 80) is not answering. The DDP lights are unaffected; the Scenes light recovers on the next 30 s poll |
| The controller has a new IP address | **Reconfigure** the entry and enter the new host (give the controller a DHCP reservation to avoid this) |
| `ddp.recall_preset` is refused | The target is a segment light, or the entry is Generic DDP, which has no Scenes light |

To report a problem, include the downloaded diagnostics and debug logs:

```yaml
logger:
  logs:
    custom_components.ddp: debug
    aioddp: debug
    pyminleon: debug
```

## Development

```bash
conda activate hass-ddp
python -m pip install -e ".[dev]"   # test, lint and type-check tools (pyproject.toml)
python -m pytest -q                 # coverage gate: fail_under in pyproject.toml
ruff check .
ruff format --check .
python -m mypy
```

The integration's Integration Quality Scale status, rule by rule, is in `custom_components/ddp/quality_scale.yaml`.

Tests run against a real loopback UDP listener and assert the DDP bytes on the wire, and against a loopback fake of
the NDB-Pro's HTTP UI serving captured pages.

## License

Apache-2.0
