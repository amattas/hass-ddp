# Entities

## Lights

| Entity | What it does |
|---|---|
| **Output N** light (one per output defined at setup; an entry from before 0.5.0 has a single **All** light until it is reconfigured) | On/off, brightness, RGB color (RGBW color for an RGBW entry), the aioddp effects, and multi-color patterns (`ddp.set_pattern`). Sends DDP; its state is assumed. On a vendor controller, turning it on stops a running scene (the controller goes `Off`), and starting a scene turns it off |
| **All outputs** light (only when the controller has more than one output) | The same as an output light, over the whole strip: a pattern or effect runs on across the outputs in one DDP frame |
| **Output N Light 1** to **Light N** (with the **One light per pixel** option; one per pixel of every output) | One pixel each: on/off, brightness, and color (RGBW for an RGBW entry); no effects or patterns. Numbered along the installed string: Light 1 is the output's first pixel, or its last one on an output marked **Reversed**. Shares its pixel with its output light and All outputs (see below) |
| **Controller** light (vendor controller types only; the controller device's main entity, named like the device) | On while the controller runs a scene. On brings back the last scene (else the first), also after a restart; brightness is the controller's own intensity (1-100%), and turn_on with a brightness sets it. Off is the controller's `Off`; starting a scene turns the DDP lights off. Its `colors` and `background` attributes are the controller's palette (the colors its effects use), set with `ddp.set_colors`; `ddp.recall_preset` plays a stored preset. Polled |
| **Color 1** to **Color 5** lights (controller device) | One palette slot each; off empties the slot. A Color tab for a custom color (brightness shows 255 and scales the color a turn_on sends) and a Temperature tab, a continuous 2700-6500 K range: the ends are the controller's white LEDs, between them is a color |
| **Scene** select (controller device) | The controller's built-in effects and shows; the running one is selected, none while it is `Off`. Selecting one runs it and turns the DDP lights off |

**Devices.** The output lights, All outputs and the pixel lights are on the DDP device, named after the entry. A vendor controller type
adds a controller device named after the entry and the type (for example "Front Yard Minleon NDB-Pro"), connected
through the DDP device, with the main light, Color 1 to 5, and Scene on it; the DDP device is then named
after the entry and Generic DDP ("Front Yard Generic DDP").

Actions: `ddp.set_pattern` (DDP lights: the outputs and All outputs, not the pixel lights), and `ddp.recall_preset` and `ddp.set_colors`
(the NDB-Pro controller light only). Home Assistant's `light.turn_on` and `light.turn_off` work on every light, and scenes
capture the segment lights. The integration provides no triggers or conditions.

**All outputs and the output lights share pixels**, so only one of them owns a pixel at a time. Turning All outputs on
stops the output lights' effects and shows them off, without sending anything; turning an output light on does the same
to All outputs. Nothing is blanked: the newly turned-on light simply draws over its own pixels, and an output that All
outputs had lit keeps that frame until something else sends to it.

**Pixel lights share their pixel** with their output light and All outputs, and the last write wins per pixel.
Coloring a pixel light changes only that pixel, and its output (and All outputs) stays on, lighting its other pixels:
a solid color stays, and an effect or pattern stops on its last frame (it would paint over the pixel), shown with
effect off. Turning the output light on again, or giving it a color or an effect,
repaints all its pixels, and the pixel lights on it show off. Turning the output light off blanks all its pixels, so
its pixel lights go off too. Turning a pixel light off sends black for that pixel only.

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

A controller of type Minleon NDB-Pro (detected from its status model `NDBPRO`, or chosen by hand) gets a second
device, "<entry> Minleon NDB-Pro", which drives the controller's own built-in effects over its HTTP UI instead of
sending DDP. Its main entity is the controller light, named like the device; Color 1 to 5 and Scene follow. Entity IDs below assume the entry is named "NDBPRO" (the title discovery gives it unless the
controller reports a name), so the entities are `light.ndbpro_minleon_ndb_pro`, `light.ndbpro_minleon_ndb_pro_color_1`,
`select.ndbpro_minleon_ndb_pro_scene`, and the DDP lights
`light.ndbpro_generic_ddp_output_1` and so on; check yours on the device pages. Entities created by an earlier release
keep their entity IDs (for example `light.ndbpro_minleon_ndb_pro` and `light.ndbpro_output_1`).

**Upgrading from 0.4.1:** the Power switch and the Intensity number are replaced by the controller light
(`light.ndbpro_minleon_ndb_pro`): on and off are the same, brightness is the intensity, and `ddp.recall_preset` and
`ddp.set_colors` target it. Home Assistant removes the old switch and number at setup; point automations at the light.

- Scene's options are the controller's effects: `Show1` and `Show2` play the shows stored on the controller; the rest
  (`Test`, `Bands`, `Glow`, ...) are its built-in patterns. The controller light's brightness sets the controller's intensity (1-100%).
- Turning the controller light off selects the controller's `Off`. A scene and the DDP lights never run together: starting a scene turns the DDP lights off, and turning a DDP light on turns the controller `Off`.
- Turning the controller light on brings back the last scene, also after a Home Assistant restart. Startup never commands the controller; the
  controller device polls the controller every 30 s and shows what it is actually running.
- `ddp.recall_preset` (on the controller light) plays one of the 20 stored presets (1-10 are slots of Show 1, 11-20 of Show 2).
- `ddp.set_colors` (on the controller light) sets the palette the built-in effects draw from: up to 5 custom colors and a background. The
  `colors` and `background` attributes show the palette as the controller reports it (`[0, 0, 0]` is an empty slot).
  `cool_white` and `warm_white` select the controller's white LEDs (it maps `#feffff` to cool white and pure white to
  warm white); an empty list clears the palette, so the controller picks random colors. Setting colors does not turn
  the controller on or take the pixels from a DDP light, and it is not saved to the controller's flash.
- **Color 1** to **Color 5** set one palette slot each, read back by the same 30 s poll. A Color N light has a Color
  tab and a Temperature tab, the white picker; it shows the tab the slot reads: `#c69740` (how the controller reports
  Warm White once set) or `#ffffff` is Warm White, `#feffff` is Cool White, anything else is a color. A color sets the
  slot to it (scaled by a brightness). The Temperature tab is a continuous 2700-6500 K range: 2700 K sends the
  controller's Warm White and 6500 K its Cool White, both exact (values outside the range are clamped); anything
  between sends Home Assistant's kelvin-to-RGB color scaled by brightness, and such a slot reads back as an RGB color
  (only the two presets read back as a temperature). Off empties the slot. A bare turn_on re-sends the slot's last color,
  a white included, also after a restart, red by default. A color of pure white `#ffffff` reads back as Warm White.
  Like `ddp.set_colors`, a slot change leaves DDP alone. Upgrading from 0.3: the `Cool White` and `Warm White`
  effects of Color N are gone; use the Temperature tab. Upgrading from 0.4.2: temperatures between the ends are now sent as colors instead of snapping to a white. Upgrading from 0.4.1: the Mode 1 to 5 selects are removed
  (Home Assistant drops their entities at setup); the Temperature tab replaces them.

### Presets

Presets are Home Assistant scenes: a scene that captures the controller light, Scene, Color 1 to 5, and the
DDP lights restores the whole look (a Color N light sets its slot outright, so the order the scene applies them in does not matter). The NDB-Pro's 20 stored presets are recall-only, with
`ddp.recall_preset`; the integration never writes the controller's saved settings.

**Segment lights vs. scenes: two exclusive modes.** The controller cannot report the pixels of its own scenes (it
answers no DDP query and its web UI has no pixel readback), so DDP pixels over a running scene could never be shown
truthfully. A vendor controller is therefore either in **scene mode** (it runs a scene, every DDP light is off) or in
**DDP mode** (the controller is `Off`, the DDP lights drive the pixels):

- Turning on a scene (the controller light or Scene, or recalling a preset) turns every DDP light off (outputs, All
  outputs, pixel lights) without sending black: effects stop and the keepalive stops re-sending, so the controller's
  scene shows. A scene started from the controller's own UI or app does the same once the 30 s poll sees it.
- Turning on any DDP light, or `ddp.set_pattern`, while a scene runs first turns the controller `Off`, then drives the
  pixels. If the controller does not answer, the action fails and nothing is sent.
- In DDP mode the DDP lights work as on a Generic DDP controller: an off output stays black (held by the keepalive)
  while another DDP light is on; when the last one turns off, DDP stops and about 20 s later the controller shows its
  own `Off`. Turning the controller light off selects `Off` and leaves DDP mode alone.
- Palette changes (Color 1 to 5, `ddp.set_colors`) change no mode.
- After a Home Assistant restart the DDP lights restore and re-send their last look only if the controller is not
  running a scene; setup reads the controller's state and never commands it.

## Actions

| Action | Target | Fields |
|---|---|---|
| `ddp.set_pattern` | An output light or All outputs | `colors` (required: 1-16 colors, each `[r, g, b]` 0-255, or `[r, g, b, w]` on an RGBW entry with `palette`), `block_size` (pixels per color block, default 1; palette only), `effect` (`palette` by default, or `chase`, `breathe`, `twinkle`), `params` (the effect's other parameters, for example `{"speed": 4}`), `brightness` (1-255) |
| `ddp.recall_preset` | The controller light | `preset` (1-20; 1-10 are slots of Show 1, 11-20 of Show 2) |
| `ddp.set_colors` | The controller light | `colors` (required: 0-5 colors for palette slots 1-5, each `[r, g, b]` 0-255 or `cool_white` / `warm_white`; the remaining slots are emptied, and an empty list lets the controller pick random colors), `background` (`[r, g, b]`; left as it is when omitted) |
| `light.turn_on` / `light.turn_off` | Any light of this integration | Segment lights: `brightness`, `rgb_color` (`rgbw_color` on an RGBW entry), `effect`. Color 1-5: `rgb_color` and `brightness` (scales the color), or `color_temp_kelvin` (2700-6500 K, clamped: 2700 Warm White, 6500 Cool White, between them a color) |
| `light.turn_on` / `light.turn_off`, `select.select_option` | Controller light, Scene | Controller light: `brightness` (sets the intensity, 1-100%; none: on is the last scene). Scene: `option` (one of the controller's effects) |

`ddp.set_pattern` on a Color light is refused. `ddp.recall_preset` and `ddp.set_colors` act only on the controller light;
Home Assistant skips any other target (with a warning in the log).
Recalling a preset overwrites the controller's current colors with the preset's. Actions raise an error when a send
fails, the effect is unknown or takes no colors, a pattern parameter is invalid, a pattern color has a white value the
entry or effect cannot show, or the controller does not answer.

## Examples: scenes on a schedule

Schedule the controller's stored scenes with plain automations against the controller device's entities:

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
          entity_id: light.ndbpro_minleon_ndb_pro
        data:
          brightness_pct: 80
      - action: select.select_option
        target:
          entity_id: select.ndbpro_minleon_ndb_pro_scene
        data:
          option: Show1

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
          entity_id: light.ndbpro_minleon_ndb_pro
        data:
          preset: 3

  - alias: Lights off at 23:00
    triggers:
      - trigger: time
        at: "23:00:00"
    actions:
      - action: light.turn_off
        target:
          entity_id: light.ndbpro_minleon_ndb_pro
```

Starting the scene at sunset turns the DDP lights off, so the 23:00 automation is all you need for "off" (if a DDP light was turned on in between, the controller is already `Off`; turn that light off instead).

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
    entity_id: light.ndbpro_minleon_ndb_pro
  data:
    colors: ["warm_white", [255, 0, 0], "warm_white", [255, 0, 0]]
- action: select.select_option
  target:
    entity_id: select.ndbpro_minleon_ndb_pro_scene
  data:
    option: Fixed Colors
```

Clear the palette so the controller picks random colors:

```yaml
action: ddp.set_colors
target:
  entity_id: light.ndbpro_minleon_ndb_pro
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
