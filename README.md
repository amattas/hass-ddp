# DDP Pixel Controller for Home Assistant

A Home Assistant custom integration that drives DDP (Distributed Display Protocol) pixel controllers, such as the
Minleon NDB-Pro, as native light entities. Each controller is one device with one light per output. Every light
supports on/off, brightness, RGB color, and an effect list, so Home Assistant scenes can capture and replay it.

All protocol code lives in [pyddp](https://github.com/amattas/pyddp); this repository only maps it onto Home Assistant.

## Installation (HACS custom repository)

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/amattas/hass-ddp` with type **Integration**.
3. Install **DDP Pixel Controller** and restart Home Assistant.

Home Assistant 2026.10.0 or later is required. pyddp is installed from a pinned git commit (the `pyddp` name on PyPI
belongs to an unrelated project).

## Setup

1. **Settings > Devices & services > Add integration > DDP Pixel Controller.**
2. Enter the controller's IP address or hostname.
3. Home Assistant asks the controller for its output layout (DDP query, then its HTTP API). If it answers, one light
   is created per active output ("Output 1", "Output 2", ...). If not, enter the total pixel count and pixel type
   (RGB or RGBW) and one light covering the whole strip is created.

DDP is send-only UDP with no acknowledgement, so setup can only check that a socket opens. Setup never sends a frame,
and reloading or removing the integration never blanks the lights.

### Options

| Option | Default | Notes |
|---|---|---|
| Brightness cap | 60% | Applied to every frame, effects included. Lower it to protect power supplies |
| Color order | RGB / RGBW | Channel order the pixels expect |
| Effect frame rate | 30 fps | 1 to 60 |
| Keepalive interval | 0 (off) | Re-sends the current frame for controllers that time out; at least 0.5 s when on |

Saving options reloads the entry.

### One sender per controller

DDP has no priority or arbitration. If FPP, xLights, or anything else is driving the controller, stop it before using
these lights, or the two will fight frame by frame.

## Effects

The effect list comes from pyddp: `breathe`, `chase`, `twinkle`, `rainbow`, `palette`. The effect `off` means a solid
color. The light's RGB color drives each effect's main color where it has one (for example the chase color or the
breathe color); brightness scales the whole effect.

## Scenes on a schedule

Scenes and schedules are plain Home Assistant: a scene captures each light's state (on/off, brightness, color, effect)
and automations activate it. Entity IDs below assume a device named "NDB-Pro" with two outputs; check yours under the
device page.

```yaml
scene:
  - name: Roofline warm chase
    entities:
      light.ndb_pro_output_1:
        state: "on"
        brightness: 200
        rgb_color: [255, 140, 40]
        effect: chase
      light.ndb_pro_output_2:
        state: "on"
        brightness: 180
        rgb_color: [255, 0, 0]
        effect: "off" # solid color; quote it, a bare off is a YAML boolean

automation:
  - alias: Roofline on at sunset
    triggers:
      - trigger: sun
        event: sunset
        offset: "-00:15:00"
    actions:
      - action: scene.turn_on
        target:
          entity_id: scene.roofline_warm_chase

  - alias: Roofline off at 23:00
    triggers:
      - trigger: time
        at: "23:00:00"
    actions:
      - action: light.turn_off
        target:
          entity_id:
            - light.ndb_pro_output_1
            - light.ndb_pro_output_2
```

You can also build the scene in the UI: set the lights the way you want them, then create a scene from the current
state. A `schedule` helper works as a trigger too, if you prefer on/off windows to fixed times.

After a Home Assistant restart, each light restores its last state and, if it was on, sends it again.

## Development

```bash
conda activate hass-ddp
python -m pytest -q
ruff check .
black --check .
mypy
```

Tests run against a real loopback UDP listener and assert the DDP bytes on the wire.

## License

Apache-2.0
