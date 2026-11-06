# Entities

## Lights

| Entity | What it does |
|---|---|
| **Output N** light (one per output; **All** for a manually described strip) | On/off, brightness, RGB color (RGBW color for an RGBW entry), the aioddp effects, and multi-color patterns (`ddp.set_pattern`). Sends DDP; its state is assumed. While a controller scene runs it shows on (the scene), and turning it off disables just that output |
| **All outputs** light (only when the controller has more than one output) | The same as an output light, over the whole strip: a pattern or effect runs on across the outputs in one DDP frame |
| **Power** switch (vendor controller types only; the controller device's main entity, named like the device) | On while the controller runs a scene. On brings back the last scene (else the first), also after a restart; off is the controller's `Off` and blanks the DDP lights too. Its `colors` and `background` attributes are the controller's palette (the colors its effects use), set with `ddp.set_colors`; `ddp.recall_preset` plays a stored preset. Polled |
| **Color 1** to **Color 5** lights (controller device) | One palette slot each; off empties the slot. A Color tab for a custom color (brightness shows 255 and scales the color a turn_on sends) and a Temperature tab for the controller's white LEDs: warm (2700 K) or cool (6500 K) |
| **Mode 1** to **Mode 5** selects (controller device) | What each palette slot holds: `Color`, `Warm White`, or `Cool White`, read from the slot. A white is set at once; `Color` brings back the slot's last custom color (red by default) |
| **Scene** select (controller device) | The controller's built-in effects and shows; the running one is selected, none while it is `Off`. Selecting one runs it and hands the pixels back from DDP |
| **Intensity** number (controller device, under Configuration) | The controller's own brightness for its scenes, 1-100%. A setting: it starts no scene and leaves DDP alone |

**Devices.** The output lights and All outputs are on the DDP device, named after the entry. A vendor controller type
adds a controller device named after the entry and the type (for example "Front Yard Minleon NDB-Pro"), connected
through the DDP device, with Power, Color 1 to 5, Mode 1 to 5, Scene, and Intensity on it; the DDP device is then named
after the entry and Generic DDP ("Front Yard Generic DDP").

Actions: `ddp.set_pattern` (DDP lights: the outputs and All outputs), and `ddp.recall_preset` and `ddp.set_colors`
(the NDB-Pro Power switch only). Home Assistant's `light.turn_on` and `light.turn_off` work on every light, and scenes
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

A controller of type Minleon NDB-Pro (detected from its status model `NDBPRO`, or chosen by hand) gets a second
device, "<entry> Minleon NDB-Pro", which drives the controller's own built-in effects over its HTTP UI instead of
sending DDP. Its main entity is the Power switch, named like the device; Color 1 to 5, Mode 1 to 5, and Scene follow,
and Intensity sits under Configuration. Entity IDs below assume the entry is named "NDBPRO" (the title discovery gives it unless the
controller reports a name), so the entities are `switch.ndbpro_minleon_ndb_pro`, `light.ndbpro_minleon_ndb_pro_color_1`,
`select.ndbpro_minleon_ndb_pro_mode_1`, `select.ndbpro_minleon_ndb_pro_scene`, `number.ndbpro_minleon_ndb_pro_intensity`, and the DDP lights
`light.ndbpro_generic_ddp_output_1` and so on; check yours on the device pages. Entities created by an earlier release
keep their entity IDs (for example `switch.ndbpro_minleon_ndb_pro_power` and `light.ndbpro_output_1`).

**Upgrading from 0.3:** the controller light (`light.ndbpro_minleon_ndb_pro`, or an older ID such as
`light.ndbpro_scenes`) is removed at setup. Move automations that targeted it to the Power switch (on and off,
`ddp.recall_preset`, `ddp.set_colors`), the Scene select (`select.select_option` instead of an `effect`), or Intensity
(`number.set_value` instead of a `brightness`).

- Scene's options are the controller's effects: `Show1` and `Show2` play the shows stored on the controller; the rest
  (`Test`, `Bands`, `Glow`, ...) are its built-in patterns. Intensity sets the controller's brightness (1-100%).
- Power off selects the controller's `Off` and also blanks any segment light that is on, so the house goes dark.
- Power on brings back the last scene, also after a Home Assistant restart. Startup never commands the controller; the
  controller device polls the controller every 30 s and shows what it is actually running.
- `ddp.recall_preset` (on Power) plays one of the 20 stored presets (1-10 are slots of Show 1, 11-20 of Show 2).
- `ddp.set_colors` (on Power) sets the palette the built-in effects draw from: up to 5 custom colors and a background. The
  `colors` and `background` attributes show the palette as the controller reports it (`[0, 0, 0]` is an empty slot).
  `cool_white` and `warm_white` select the controller's white LEDs (it maps `#feffff` to cool white and pure white to
  warm white); an empty list clears the palette, so the controller picks random colors. Setting colors does not turn
  Power on or take the pixels from a DDP light, and it is not saved to the controller's flash.
- **Mode 1** to **Mode 5** and **Color 1** to **Color 5** set one palette slot each, read back by the same 30 s poll.
  Mode is read from the slot: `#c69740` (how the controller reports Warm White once set) or `#ffffff` is Warm White,
  `#feffff` is Cool White, anything else (an empty slot included) is Color. Choosing Warm White or Cool White sends
  that white value (`#ffffff` or `#feffff`) at once; choosing Color while the slot holds a white sends the slot's last
  custom color (the last one seen or set that is neither black nor a white, also after a restart; red by default), and
  leaves a slot that already holds a color, or is empty, as it is. So an HA scene that restores Mode and Color together
  never sees a cleared slot.
- Color N has a Color tab and a Temperature tab, and shows the one its Mode reads. A color sets the slot to it (scaled
  by a brightness); a temperature sets a white, sent exact: up to 4600 K Warm White (shown as 2700 K), above it Cool
  White (shown as 6500 K). Off empties the slot (and Mode reads Color again). A bare turn_on re-sends the slot's last
  color, a white included, also after a restart, red by default. A color of pure white `#ffffff` reads back as Warm
  White, so the mode follows. Like `ddp.set_colors`, a slot change leaves DDP alone. Upgrading from 0.3: the
  `Cool White` and `Warm White` effects of Color N are gone; use Mode N or the Temperature tab.

### Presets

Presets are Home Assistant scenes: a scene that captures Power, Scene, Mode 1 to 5, Color 1 to 5, Intensity, and the
DDP lights restores the whole look (Mode Color brings the slot's custom color back before Color N sets it, so the
order the scene applies them in does not matter). The NDB-Pro's 20 stored presets are recall-only, with
`ddp.recall_preset`; the integration never writes the controller's saved settings.

**Segment lights vs. scenes.** DDP data overrides the controller's built-in effect immediately, and about 20 s after DDP
stops on its pixels the controller falls back to it. Each DDP light (the outputs and All outputs) shows what lights its
pixels:

- Turning on a scene (Power or Scene, or recalling a preset) stops DDP without sending black and shows every DDP light on: they show
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
- Turning Power off turns every DDP light off. When the poll sees the controller turned `Off` elsewhere,
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
| `ddp.recall_preset` | A controller's Power switch | `preset` (1-20; 1-10 are slots of Show 1, 11-20 of Show 2) |
| `ddp.set_colors` | A controller's Power switch | `colors` (required: 0-5 colors for palette slots 1-5, each `[r, g, b]` 0-255 or `cool_white` / `warm_white`; the remaining slots are emptied, and an empty list lets the controller pick random colors), `background` (`[r, g, b]`; left as it is when omitted) |
| `light.turn_on` / `light.turn_off` | Any light of this integration | Segment lights: `brightness`, `rgb_color` (`rgbw_color` on an RGBW entry), `effect`. Color 1-5: `rgb_color` and `brightness` (scales the color), or `color_temp_kelvin` (up to 4600 Warm White, above Cool White) |
| `switch.turn_on` / `switch.turn_off`, `select.select_option`, `number.set_value` | Power, Scene, Mode 1-5, Intensity | Power: none (on is the last scene). Scene: `option` (one of the controller's effects). Mode N: `option` (`color`, `warm_white`, `cool_white`). Intensity: `value` (1-100) |

`ddp.set_pattern` on a Color light is refused. `ddp.recall_preset` and `ddp.set_colors` act only on a Power switch;
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
      - action: number.set_value
        target:
          entity_id: number.ndbpro_minleon_ndb_pro_intensity
        data:
          value: 80
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
          entity_id: switch.ndbpro_minleon_ndb_pro
        data:
          preset: 3

  - alias: Lights off at 23:00
    triggers:
      - trigger: time
        at: "23:00:00"
    actions:
      - action: switch.turn_off
        target:
          entity_id: switch.ndbpro_minleon_ndb_pro
```

Turning Power off blanks the DDP segment lights too, so the 23:00 automation is all you need for "off".

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
    entity_id: switch.ndbpro_minleon_ndb_pro
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
  entity_id: switch.ndbpro_minleon_ndb_pro
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
