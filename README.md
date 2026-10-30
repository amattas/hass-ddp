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
| Keepalive interval | 1 s | Re-sends the current frame so the controller doesn't fall back to its built-in effect; 0 turns it off, otherwise at least 0.5 s |

Saving options reloads the entry.

### One sender per controller

DDP has no priority or arbitration. If FPP, xLights, or anything else is driving the controller, stop it before using
these lights, or the two will fight frame by frame.

## Effects

The effect list comes from pyddp: `breathe`, `chase`, `twinkle`, `rainbow`, `palette`. The effect `off` means a solid
color. The light's RGB color drives each effect's main color where it has one (for example the chase color or the
breathe color); brightness scales the whole effect.

## NDB-Pro built-in scenes

A Minleon NDB-Pro (discovered as model `NDBPRO`) gets one more light on the same device: **Scenes**. It drives the
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

## Scenes on a schedule

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

## Development

```bash
conda activate hass-ddp
python -m pytest -q
ruff check .
black --check .
mypy
```

Tests run against a real loopback UDP listener and assert the DDP bytes on the wire, and against a loopback fake of
the NDB-Pro's HTTP UI serving captured pages.

## License

Apache-2.0
