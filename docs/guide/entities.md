# Entities

## Lights

| Entity | What it does |
|---|---|
| **Output N** light (one per output; **All** for a manually described strip) | On/off, brightness, RGB color, and the aioddp effects. Sends DDP; its state is assumed |
| **Scenes** light (vendor controller types only) | Selects the controller's built-in effects and shows, brightness maps to its intensity. Polled |

Action: `ddp.recall_preset` (NDB-Pro Scenes light only). Home Assistant's `light.turn_on` and `light.turn_off` work on
every light, and scenes capture the segment lights. The integration provides no triggers or conditions.

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

## Actions

| Action | Target | Fields |
|---|---|---|
| `ddp.recall_preset` | A Scenes light | `preset` (1-20; 1-10 are slots of Show 1, 11-20 of Show 2) |
| `light.turn_on` / `light.turn_off` | Any light of this integration | Segment lights: `brightness`, `rgb_color`, `effect`. Scenes light: `brightness`, `effect` |

`ddp.recall_preset` on a segment light is refused. Recalling a preset overwrites the controller's current colors with
the preset's. Actions raise an error when a send fails, the effect is unknown, or the controller does not answer.

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

