# DDP Pixel Controller for Home Assistant

A Home Assistant custom integration that drives DDP (Distributed Display Protocol) pixel controllers, such as the
Minleon NDB-Pro, as native light entities. Each controller is one device with one light per output (a Minleon
NDB-Pro adds a second device for its built-in effects and palette). Every light
supports on/off, brightness, RGB color (RGBW on 4-channel pixels), and an effect list, so Home Assistant scenes can
capture and replay it.

All protocol code lives in [aioddp](https://github.com/amattas/aioddp), and Minleon NDB-Pro control in
[pyminleon](https://github.com/amattas/pyminleon); this repository only maps them onto Home Assistant.

## Supported devices

| Device | Support |
|---|---|
| Minleon NDB-Pro with Rainmin RGB+ pixels | The reference device. DDP lights plus the controller device (Power, Color 1-5, Mode 1-5, Scene, Intensity), `ddp.recall_preset`, and `ddp.set_colors` |
| Any other DDP v1 controller (UDP port 4048) | **Generic DDP**: one light per output, or one light for the whole strip. Not tested beyond the DDP spec |

RGB and RGBW pixels are supported. DDP is send-only, so Home Assistant cannot read a controller's state back over DDP;
see Known limitations. See [aioddp's compatibility notes](https://github.com/amattas/aioddp/blob/main/docs/compatibility.md)
for which controllers have been exercised.

## Supported functions

| Entity | What it does |
|---|---|
| **Output N** light (one per output; **All** for a manually described strip) | On/off, brightness, RGB color (RGBW color for an RGBW entry), the aioddp effects, and multi-color patterns (`ddp.set_pattern`). Sends DDP; its state is assumed. While a controller scene runs it shows on (the scene), and turning it off disables just that output |
| **All outputs** light (only when the controller has more than one output) | The same as an output light, over the whole strip: a pattern or effect runs on across the outputs in one DDP frame |
| **Power** switch (vendor controller types only; the controller device's main entity, named like the device) | On while the controller runs a scene. On brings back the last scene (else the first), also after a restart; off is the controller's `Off` and blanks the DDP lights too. Its `colors` and `background` attributes are the controller's palette (the colors its effects use), set with `ddp.set_colors`; `ddp.recall_preset` plays a stored preset. Polled |
| **Color 1** to **Color 5** lights (controller device) | One palette slot each; off empties the slot. In Color mode an RGB light (brightness shows 255 and scales the color a turn_on sends); in a white mode an on/off light for the controller's white LEDs |
| **Mode 1** to **Mode 5** selects (controller device) | What each palette slot holds: `Color`, `Warm White`, or `Cool White`, read from the slot. A white is set at once; `Color` empties the slot until Color N sets a color |
| **Scene** select (controller device) | The controller's built-in effects and shows; the running one is selected, none while it is `Off`. Selecting one runs it and hands the pixels back from DDP |
| **Intensity** number (controller device, under Configuration) | The controller's own brightness for its scenes, 1-100%. A setting: it starts no scene and leaves DDP alone |

**RGBW.** When the entry's pixel type is RGBW (the controller reports 4-channel input, or you chose RGBW when
describing the strip), every DDP light uses Home Assistant's RGBW color mode: `rgbw_color` sets red, green, blue,
and white, and white reaches the pixels' white LEDs. The default look is the white channel alone, `[0, 0, 0, 255]`.
Solid colors and `palette` patterns carry white; the animated effects use red, green, and blue only (see Known
limitations).

Devices: the output lights and All outputs are on the DDP device, named after the entry. A vendor controller type adds
a controller device named after the entry and the type (for example "Front Yard Minleon NDB-Pro"), connected through
the DDP device, with Power, Color 1 to 5, Mode 1 to 5, Scene, and Intensity on it; the DDP device is then named after
the entry and Generic DDP ("Front Yard Generic DDP").

Actions: `ddp.set_pattern` (DDP lights: the outputs and All outputs), and `ddp.recall_preset` and `ddp.set_colors`
(the NDB-Pro Power switch only). Home Assistant's `light.turn_on` and `light.turn_off` work on every light, and scenes capture the segment lights.
The integration provides no triggers or conditions.

**All outputs and the output lights share pixels**, so only one of them owns a pixel at a time. Turning All outputs on
stops the output lights' effects and shows them off, without sending anything; turning an output light on does the same
to All outputs. Nothing is blanked: the newly turned-on light simply draws over its own pixels, and an output that All
outputs had lit keeps that frame until something else sends to it.

## Installation

Prerequisites: Home Assistant 2026.10.0 or later, the controller reachable from Home Assistant over UDP (port 4048) and,
for the controller device's entities, TCP port 80, and `git` available to Home Assistant, because the two libraries are installed from
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
| Minleon NDB-Pro | The DDP lights, plus a controller device with **Power** (its main entity), **Color 1** to **Color 5**, **Mode 1** to **Mode 5**, **Scene**, and **Intensity**, and the `ddp.recall_preset` and `ddp.set_colors` actions (see below) |

The stored type decides: nothing probes the controller at startup. Adding another vendor's controller means one entry
in the controller type registry (`custom_components/ddp/controller.py`) backed by that vendor's library.

### Reconfigure

**Reconfigure** on the integration entry changes the host (for example after a new DHCP lease) or the controller type,
and re-reads the output layout the same way adding does (a DDP query, then the controller type's own API). The form
shows the stored pixel type and the layout the controller reports now. When it reports one, the entry stores its
pixel type (RGB or RGBW), outputs, and pixel count; so after switching an NDB-Pro between a 3-channel and a 4-channel
input mode, reconfigure the entry to follow it. Outputs that are still there keep their lights; an output that is gone
loses its light (and with a single output left, All outputs goes too). A color order option that does not fit the new
pixel type is reset to the default. When the controller reports no layout, the stored one is kept.

The entry reloads and keeps its devices, entities, and history. Choosing Generic DDP removes the controller device and
its entities.

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
- **The controller device's entities share one poll of the controller's web UI every 30 seconds**; a command publishes
  what it changed (a preset is read back right after). If the controller does not answer, only those entities become unavailable (logged once, and again when it is back);
  the DDP lights keep working.

## Effects

The effect list comes from aioddp: `breathe`, `chase`, `twinkle`, `rainbow`, `palette`. The effect `off` means a solid
color. The light's RGB color drives each effect's main color where it has one (for example the chase color, the
breathe color, or the one palette color); brightness scales the whole effect.

**Patterns.** `ddp.set_pattern` gives a DDP light several colors at once: by default the `palette` effect repeats them
in fixed blocks (`block_size` pixels each, so two colors with block size 1 alternate pixel by pixel). It can also hand
the colors to `chase` (its lit groups cycle through them), or to `breathe` and `twinkle`, which use the first color.
`params` sets the effect's other parameters, such as the chase `speed`; a bad value is refused with the parameter
named. The light's effect shows the rendering effect, and the pattern is kept across restarts and a bare
`light.turn_on`. A later `light.turn_on` with a color replaces the pattern with that one color; choosing another
effect drops it.

On an RGBW entry a pattern color may be `[r, g, b, w]` (an `[r, g, b]` color gets white 0), with `palette` only;
the other effects render RGB and refuse a white value, as does an RGB entry.

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
  that white value (`#ffffff` or `#feffff`) at once; choosing Color empties the slot (`#000000`), so Color N shows off
  until you pick a color.
- In Color mode, Color N is an RGB light: a color sets the slot to it (scaled by a brightness), off empties the slot,
  and a bare turn_on of an empty slot brings back its last color, also after a restart, red by default. A color of pure
  white `#ffffff` reads back as Warm White, so the mode follows. In a white mode Color N is an on/off light: on re-sends
  the white, off empties the slot (and the mode reads Color again). Like `ddp.set_colors`, a slot change leaves DDP
  alone. Upgrading from 0.3: the `Cool White` and `Warm White` effects of Color N are gone; use Mode N.

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

## Use cases

- Run a roofline in a solid warm white most nights and a chase or rainbow effect for holidays, from dashboards or
  scenes.
- Let the NDB-Pro play the shows stored on it on a schedule (sunset to 23:00) while Home Assistant stays in charge of
  the on/off times.
- Take a section of the strip over for a one-off alert color (for example a doorbell flash) and hand it back after.

## Actions

| Action | Target | Fields |
|---|---|---|
| `ddp.set_pattern` | An output light or All outputs | `colors` (required: 1-16 colors, each `[r, g, b]` 0-255, or `[r, g, b, w]` on an RGBW entry with `palette`), `block_size` (pixels per color block, default 1; palette only), `effect` (`palette` by default, or `chase`, `breathe`, `twinkle`), `params` (the effect's other parameters, for example `{"speed": 4}`), `brightness` (1-255) |
| `ddp.recall_preset` | A controller's Power switch | `preset` (1-20; 1-10 are slots of Show 1, 11-20 of Show 2) |
| `ddp.set_colors` | A controller's Power switch | `colors` (required: 0-5 colors for palette slots 1-5, each `[r, g, b]` 0-255 or `cool_white` / `warm_white`; the remaining slots are emptied, and an empty list lets the controller pick random colors), `background` (`[r, g, b]`; left as it is when omitted) |
| `light.turn_on` / `light.turn_off` | Any light of this integration | Segment lights: `brightness`, `rgb_color` (`rgbw_color` on an RGBW entry), `effect`. Color 1-5: in Color mode `rgb_color` and `brightness` (scales the color); in a white mode on/off only |
| `switch.turn_on` / `switch.turn_off`, `select.select_option`, `number.set_value` | Power, Scene, Mode 1-5, Intensity | Power: none (on is the last scene). Scene: `option` (one of the controller's effects). Mode N: `option` (`color`, `warm_white`, `cool_white`). Intensity: `value` (1-100) |

`ddp.set_pattern` on a Color light is refused. `ddp.recall_preset` and `ddp.set_colors` act only on a Power switch;
Home Assistant skips any other target (with a warning in the log). Recalling a preset overwrites the controller's current colors with
the preset's. Actions raise an error when a send fails, the effect is unknown or takes no colors, a pattern parameter
is invalid, a pattern color has a white value the entry or effect cannot show, or the controller does not answer.

## Diagnostics

**Settings > Devices & services > DDP Pixel Controller > Download diagnostics** includes the entry, the DDP sender's
counters, and, for an NDB-Pro, the controller's polled state and palette and the controller's config and status documents. The host, IP addresses, MAC address, and
Wi-Fi name are redacted.

## Known limitations

- **One DDP sender per controller.** DDP has no priority or arbitration. If FPP, xLights, or anything else is driving
  the controller, stop it before using these lights, or the two fight frame by frame.
- **DDP takes over the NDB-Pro, and it gives up after about 20 s.** DDP data overrides the controller's built-in
  effect at once; about 20 s after DDP stops it falls back to it, even mid solid color. The keepalive (default 1 s)
  prevents this; with keepalive 0 a solid color reverts after about 20 s.
- **Recalling a preset overwrites the controller's current colors** with the preset's. The integration never writes
  the controller's saved settings.
- **Segment light state is assumed.** Changes made elsewhere (another sender) are not seen; the controller's own scene
  starting or stopping is, at the next 30 s poll.
- **Per-output disable during a scene is not verified on hardware.** If the NDB-Pro stops its effect on every output
  while DDP writes to one, disabling one output freezes the scene on the others.
- **Setup cannot verify the controller over DDP.** It only checks that a UDP socket opens; a wrong host looks fine
  until a frame is sent and nothing lights up.
- **The controller device needs the controller's HTTP UI on port 80** and only exists for vendor controller types.
- **The layout is read when the entry is added and on Reconfigure,** never at startup. If you re-patch outputs or
  change the controller's input mode, reconfigure the entry.
- **White needs a 4-channel input mode.** The NDB-Pro must be set to a 4-channel (RGBW) input mode in its own UI for
  the entry to be RGBW; reconfigure the entry after changing it. In a 3-channel mode the controller derives white
  itself and the lights stay RGB.
- **Animated effects ignore white.** `breathe`, `chase`, `twinkle`, and `rainbow` render RGB: on an RGBW light they
  use the color's red, green, and blue and leave the white LEDs off. Solid colors and `palette` carry white.
- **No discovery.** DDP has no discovery protocol, so controllers are added by host.
- **No brand images yet,** so the integration shows Home Assistant's placeholder icon.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Lights do not react | Host is right; UDP 4048 is not blocked between Home Assistant and the controller; nothing else is sending DDP to it; the output is patched on the controller. The brightness cap (default 60%) may also make colors dimmer than expected |
| Colors are wrong (red and green swapped, ...) | Set **Color order** in the options to match the pixels |
| A solid color turns into the controller's own effect after about 20 s | The keepalive is off or too long; set it to 1 s |
| Power, Color 1-5, Mode 1-5, Scene, or Intensity are unavailable | The controller's web UI (TCP 80) is not answering. The DDP lights are unaffected; the controller device's entities recover on the next 30 s poll |
| The controller has a new IP address | **Reconfigure** the entry and enter the new host (give the controller a DHCP reservation to avoid this) |
| `ddp.recall_preset` or `ddp.set_colors` does nothing or is refused | Nothing happens (and the log warns about the target) when the target is not a Power switch, for example a light or a Generic DDP entry, which has none. `ddp.set_colors` refuses more than 5 colors, a component outside 0-255, or a name other than `cool_white` / `warm_white` |
| `ddp.set_pattern` is refused | The target is a Color light, the effect takes no colors (`rainbow`), or a `params` value is out of range or not a parameter of that effect (the message names it) |
| An output light turned off by itself | All outputs was turned on; it owns the pixels of every output until an output light is turned on again |

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
