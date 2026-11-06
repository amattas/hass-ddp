# Entities

## Lights

| Entity | What it does |
|---|---|
| **Output N** light (one per output; **All** for a manually described strip) | On/off, brightness, RGB color (RGBW color for an RGBW entry), the aioddp effects, and multi-color patterns (`ddp.set_pattern`). Sends DDP; its state is assumed. While a controller scene runs it shows on (the scene), and turning it off disables just that output |
| **All outputs** light (only when the controller has more than one output) | The same as an output light, over the whole strip: a pattern or effect runs on across the outputs in one DDP frame |
| **Scenes** light (vendor controller types only, on the controller device) | Selects the controller's built-in effects and shows, brightness maps to its intensity. Its palette is the `colors` and `background` attributes, set with `ddp.set_colors`. Polled |

**Devices.** The output lights and All outputs are on the DDP device, named after the entry. A vendor controller type
adds a controller device named after the entry and the type (for example "Front Yard Minleon NDB-Pro"), connected
through the DDP device, with the Scenes light on it.

Actions: `ddp.set_pattern` (DDP lights: the outputs and All outputs), and `ddp.recall_preset` and `ddp.set_colors`
(NDB-Pro Scenes light only). Home Assistant's `light.turn_on` and `light.turn_off` work on every light, and scenes
capture the segment lights. The integration provides no triggers or conditions.

**All outputs and the output lights share pixels**, so only one of them owns a pixel at a time. Turning All outputs on
stops the output lights' effects and shows them off, without sending anything; turning an output light on does the same
to All outputs. Nothing is blanked: the newly turned-on light simply draws over its own pixels, and an output that All
outputs had lit keeps that frame until something else sends to it.

## RGBW

When the entry's pixel type is RGBW (the controller reports 4-channel input, or you chose RGBW when describing the
strip), every DDP light uses Home Assistant's RGBW color mode: `rgbw_color` sets red, green, blue, and white, and white
reaches the pixels' white LEDs. The default look is the white channel alone, `[0, 0, 0, 255]`. Solid colors and
`palette` patterns carry white; the animated effects use red, green, and blue only. An NDB-Pro must be in a 4-channel
input mode for this (see [Pixel type](configuration.md#pixel-type)); an RGB entry refuses a color with a white value.

## Effects

The effect list comes from aioddp: `breathe`, `chase`, `twinkle`, `rainbow`, `palette`. The effect `off` means a solid
color. The light's RGB color drives each effect's main color where it has one (for example the chase color or the
breathe color, or the one palette color); brightness scales the whole effect. `breathe`, `chase`, `twinkle`, and
`rainbow` ignore white on an RGBW light.

## Patterns

`ddp.set_pattern` gives a DDP light several colors at once: by default the `palette` effect repeats them in fixed
blocks (`block_size` pixels each, so two colors with block size 1 alternate pixel by pixel). It can also hand the colors
to `chase` (its lit groups cycle through them), or to `breathe` and `twinkle`, which use the first color. `params` sets
the effect's other parameters, such as the chase `speed`; a bad value is refused with the parameter named. The light's
effect shows the rendering effect, and the pattern is kept across restarts and a bare `light.turn_on`. A later
`light.turn_on` with a color replaces the pattern with that one color; choosing another effect drops it.

On an RGBW entry a pattern color may be `[r, g, b, w]` (an `[r, g, b]` color gets white 0), with `palette` only; the
other effects render RGB and refuse a white value, as does an RGB entry.

## NDB-Pro built-in scenes

A controller of type Minleon NDB-Pro (detected from its status model `NDBPRO`, or chosen by hand) gets one more light
on a second device, "<entry> Minleon NDB-Pro": **Scenes**. It drives the controller's own built-in effects over its
HTTP UI instead of sending DDP. Entity IDs below assume the entry is named "NDBPRO" (the title discovery gives it
unless the controller reports a name), so the lights are `light.ndbpro_minleon_ndb_pro_scenes`,
`light.ndbpro_output_1`, and so on; check yours on the device pages. (A Scenes light created before the controller
device existed keeps its entity ID, for example `light.ndbpro_scenes`.)

- Its effect list is the controller's: `Show1` and `Show2` play the shows stored on the controller; the rest (`Test`,
  `Bands`, `Glow`, ...) are its built-in patterns. Brightness maps to the controller's intensity (0-100%).
- `light.turn_off` selects the controller's `Off` and also blanks any segment light that is on, so the house goes dark.
- A bare `light.turn_on` brings back the last scene, also after a Home Assistant restart. Startup never commands the
  controller; the light polls the controller every 30 s and shows what it is actually running.
- `ddp.recall_preset` plays one of the 20 stored presets (1-10 are slots of Show 1, 11-20 of Show 2).
- `ddp.set_colors` sets the palette the built-in effects draw from: up to 5 custom colors and a background. The
  `colors` and `background` attributes show the palette as the controller reports it (`[0, 0, 0]` is an empty slot).
  `cool_white` and `warm_white` select the controller's white LEDs (it maps `#feffff` to cool white and pure white to
  warm white); an empty list clears the palette, so the controller picks random colors. Setting colors does not turn
  the Scenes light on or take the pixels from a DDP light, and it is not saved to the controller's flash.

**Segment lights vs. scenes.** DDP data overrides the controller's built-in effect immediately, and about 20 s after DDP
stops on its pixels the controller falls back to it. Each DDP light (the outputs and All outputs) shows what lights its
pixels:

- Turning on a scene (or recalling a preset) stops DDP without sending black and shows every DDP light on: they show
  the scene. A scene started from the controller's own UI does the same once the 30 s poll sees it.
- Turning a DDP light off while a scene runs disables that output: black is sent for its pixels only and kept up by the
  keepalive, and the scene keeps running on the other outputs. All outputs off blanks the whole strip and shows every
  output off.
- A bare `light.turn_on` (no brightness, color, or effect) of a disabled light hands its pixels back to the scene:
  nothing is sent and the keepalive stops for them, and the controller resumes the scene there after its DDP timeout,
  about 20 s later. A bare turn_on of All outputs brings back every output; brought back one at a time, All outputs
  shows on again once no output is disabled.
- Turning a DDP light on with a brightness, color, effect, or pattern takes its pixels over DDP again; a light that
  shares those pixels (All outputs for an output, the outputs for All outputs) shows off, and the others keep showing
  the scene. While a light holds pixels, the keepalive (default 1 s) keeps re-sending them so the
  controller does not fall back mid-color; with keepalive 0 a solid color reverts to the built-in effect after about
  20 s.
- Turning the Scenes light off turns every DDP light off. When the poll sees the controller turned `Off` elsewhere,
  the lights that showed or disabled the scene go off; lights driven over DDP keep running.
- With no scene running, an off output stays black (held by the keepalive) while another DDP light is on; when the
  last DDP light turns off, DDP stops; about 20 s later the controller's own effect
  (or `Off`) is back.
- After a Home Assistant restart a disabled output is not restored (no black is sent, so the scene comes back on it),
  and a light that showed the scene is not re-sent; with a scene running, the idle DDP lights show it again.

Not verified on hardware yet: whether the NDB-Pro keeps its effect running on outputs DDP is not writing to. If it does
not, disabling one output freezes the scene on the others.

## Actions

| Action | Target | Fields |
|---|---|---|
| `ddp.set_pattern` | An output light or All outputs | `colors` (required: 1-16 colors, each `[r, g, b]` 0-255, or `[r, g, b, w]` on an RGBW entry with `palette`), `block_size` (pixels per color block, default 1; palette only), `effect` (`palette` by default, or `chase`, `breathe`, `twinkle`), `params` (the effect's other parameters, for example `{"speed": 4}`), `brightness` (1-255) |
| `ddp.recall_preset` | A Scenes light | `preset` (1-20; 1-10 are slots of Show 1, 11-20 of Show 2) |
| `ddp.set_colors` | A Scenes light | `colors` (required: 0-5 colors for palette slots 1-5, each `[r, g, b]` 0-255 or `cool_white` / `warm_white`; the remaining slots are emptied, and an empty list lets the controller pick random colors), `background` (`[r, g, b]`; left as it is when omitted) |
| `light.turn_on` / `light.turn_off` | Any light of this integration | Segment lights: `brightness`, `rgb_color` (`rgbw_color` on an RGBW entry), `effect`. Scenes light: `brightness`, `effect` |

`ddp.set_pattern` on the Scenes light, and `ddp.recall_preset` or `ddp.set_colors` on a segment light, are refused.
Recalling a preset overwrites the controller's current colors with the preset's. Actions raise an error when a send
fails, the effect is unknown or takes no colors, a pattern parameter is invalid, a pattern color has a white value the
entry or effect cannot show, or the controller does not answer.

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
          entity_id: light.ndbpro_minleon_ndb_pro_scenes
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
          entity_id: light.ndbpro_minleon_ndb_pro_scenes
        data:
          preset: 3

  - alias: Lights off at 23:00
    triggers:
      - trigger: time
        at: "23:00:00"
    actions:
      - action: light.turn_off
        target:
          entity_id: light.ndbpro_minleon_ndb_pro_scenes
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

## Examples: controller colors

The controller's Fixed Colors effect in warm white and red, every other color (slots 1-4; slot 5 is emptied):

```yaml
- action: ddp.set_colors
  target:
    entity_id: light.ndbpro_minleon_ndb_pro_scenes
  data:
    colors: ["warm_white", [255, 0, 0], "warm_white", [255, 0, 0]]
- action: light.turn_on
  target:
    entity_id: light.ndbpro_minleon_ndb_pro_scenes
  data:
    effect: Fixed Colors
```

Clear the palette so the controller picks random colors:

```yaml
action: ddp.set_colors
target:
  entity_id: light.ndbpro_minleon_ndb_pro_scenes
data:
  colors: []
```

## Examples: patterns

Alternate red and green pixel by pixel across both outputs, as one continuous pattern:

```yaml
action: ddp.set_pattern
target:
  entity_id: light.ndbpro_all_outputs
data:
  colors: [[255, 0, 0], [0, 255, 0]]
  block_size: 1
  brightness: 200
```

Red, white, and blue blocks of five pixels on one output, then a moving version with chase:

```yaml
- action: ddp.set_pattern
  target:
    entity_id: light.ndbpro_output_1
  data:
    colors: [[255, 0, 0], [255, 255, 255], [0, 0, 255]]
    block_size: 5
- action: ddp.set_pattern
  target:
    entity_id: light.ndbpro_output_1
  data:
    colors: [[255, 0, 0], [255, 255, 255], [0, 0, 255]]
    effect: chase
    params:
      speed: 4
      spacing: 6
      width: 3
```
