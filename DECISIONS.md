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

- `aioddp` and `pyminleon` stay pinned by git commit (`pkg@git+https://...@<sha>`, no spaces: hassfest rejects requirements containing a space) until their first PyPI releases, then switch to `==` PyPI pins in `manifest.json` and `pyproject.toml`.

## 2026-11-05: first field test feedback (patterns, controller effects, both outputs, white)

Findings from the live NDB-Pro (diagnostics): entry is `rgb`, controller input conversion `conv=13` (3-channel RGB
in, controller derives white for its RGBW+ pixels); the Scenes light already lists the controller's 17 effects, but
the controller's 6-slot color palette (`/pcolors.html`, with Cool White / Warm White presets) is not exposed.

- Multi-color DDP patterns: a `ddp.set_pattern` entity action on segment lights (list of colors + block size, rendered
  by aioddp's `palette` effect, kept in restore state). No fixed "alternate"-style effects.
- aioddp effects: expose their parameters through the same action; new effects are a later phase, chosen by name.
- Both outputs together: a built-in "All outputs" light spanning the whole strip (one DDP frame, patterns continue
  across outputs), not HA's Light Group helper.
- Controller effects and palette: two HA devices under one entry, the controller (Scenes light + its 6 palette colors,
  via a `ddp.set_colors` action, current palette as attributes; `via_device` the DDP device) and the DDP output device
  (segment lights, All outputs). Palette read/write is added to pyminleon (it still never saves to flash).
- White channel: the user switches the NDB-Pro to a 4-channel input mode in its UI and reconfigures the entry;
  segment lights then get HA's RGBW color mode. Solid colors and patterns carry W; animated aioddp effects stay RGB.
  Reconfigure re-reads the layout so the data type and segment slots follow the controller.
- Implementation notes (2026-11-05): the controller device links to the DDP device with `via_device_id` (HA 2026.10
  deprecates `via_device`), so the Scenes light of a new entry is `light.<title>_<type label>_scenes`; existing
  entities keep their IDs. The NDB-Pro's Warm White preset sends `#ffffff` and Cool White `#feffff` (the gold swatch
  is UI only); pyminleon exposes both as constants and hass-ddp as the named colors `warm_white` / `cool_white`.
  Light setup removes any light of the entry it did not create (dropped output, All outputs with one segment left,
  Scenes on Generic). A pattern color with W is refused before aioddp sees it on an RGB entry or a non-RGBW effect.

## 2026-11-06: DDP lights reflect a running controller scene

- Field test: while a controller scene ran, the DDP lights (Output 1/2, All outputs) showed off because they only
  ever showed what HA sent over DDP. Decided: they reflect the scene. Each DDP light has a source: `off`, `ddp`
  (HA drives it), `scene` (the controller's effect lights it; nothing is sent) or `blanked` (a scene runs but this
  light was turned off: black is sent and held for its pixels). `is_on` is `ddp` or `scene`.
- Scene on (HA command or poll): `off` and `ddp` lights become `scene` (DDP stops, as before). Scene off from HA: every
  DDP light goes off (as before). Scene off seen by a poll: `scene`/`blanked` lights go off, `ddp` lights keep running.
- Turning a `scene` light off blanks just its pixels and holds them (per-output disable); a bare turn_on hands the
  pixels back to the scene by releasing them, so the controller resumes there after its ~20 s DDP timeout. Any
  turn_on with a color, effect or pattern takes the light to `ddp` as before.
- aioddp keepalive now resends only held byte ranges and `release(segment)` drops one range, so blanking one output
  never overrides the scene on another. `release_if_idle` is replaced by per-segment release.
- Not verified on hardware yet: whether the NDB-Pro keeps its effect running on outputs DDP is not writing to. If it
  does not, per-output disable freezes the other output; the README says so until the field test settles it.
- Restart: `blanked` is not restored; after the first poll a running scene shows every idle DDP light as `scene`.
- Implementation notes (2026-11-06): ownership hand-over applies to `ddp` and `scene` lights (both go `off`, sending
  nothing; a `blanked` light keeps its black), so All outputs goes off when an output takes its pixels over a running
  scene, and the outputs go off when All outputs does. A poll runs the DDP-light changes under the
  coordinator's lock before it publishes (`DDPScenesCoordinator.on_poll`, set by the Scenes light; since moved to the
  commands, see below), so commands never
  trigger them; "scene start" (a change to a different non-Off effect) takes `off` and `ddp` lights. Every other poll
  of a running scene moves the `off` lights that hold no range to `scene` (released, so the controller lights them
  again ~20 s later) and never touches `ddp` lights, since DDP may be overriding that effect. Setup moves only idle
  lights (off, no overlapping `ddp` light). Restore stores the source: a light that was on showing a scene is not re-sent. All outputs is
  blanked only while an output it covers is; its outputs hold their own black, so un-blanking one releases just that
  output, and a bare turn_on of All outputs un-blanks every output it covers.
- Held pixels (2026-11-06, after a verifier found them drifting): which ranges keepalive resends is a pure function of
  the lights' state, not of events. `DDPRuntime.sync_holds()` runs under the ownership lock at the end of every change
  (turn_on, turn_off, un-blank, a poll's changes, the Scenes light's commands): the wanted set is the segments of the
  lights whose `holding` is true (`ddp`, `blanked`, or `off` while no scene runs and another light is `ddp`).
  The held pixels are read from the sender (`DDPSender.held`, byte ranges divided by the strip's channels), never
  from bookkeeping: a runner stopped before its first frame (concurrent turn_ons of All outputs and an output) never
  held its range, and a `runtime.held` dict that assumed it did left the other output unblanked and unheld. A held
  range nothing wants is released; an `off` or `blanked` light that should hold gets black wherever neither the
  sender holds the pixels nor a `ddp` light covers them (a `ddp` light whose runner has not sent yet still counts as
  covered, so it never gets black). Consequence: with no scene running, turning one output on over DDP sends black once to the other
  off outputs and holds it (before, an output that had never been sent stayed unheld), and a poll that sees the
  controller go `Off` holds black for the outputs that showed the scene while another light is `ddp`. Setup and
  unload still send nothing (restore re-sends a `ddp` light but does not sync).

## 2026-11-06: controller device entities

- Names: the controller device's light is the device's main entity (no entity name), so it reads as the controller
  (e.g. "Front Yard Minleon NDB-Pro") instead of "… Scenes". The DDP device of a vendor entry is named
  "<title> Generic DDP" (the Generic DDP type label); a Generic entry keeps the title alone.
- The controller device also gets: a `switch` "Power" (on = last scene, off = the controller's Off), a `select` "Scene"
  listing the controller's effects, and five `light` entities "Color 1".."Color 5" for the palette slots: RGB color
  picker, effect list ["Cool White", "Warm White"] for the controller's named whites, off = slot cleared (#000000).
  All read through the one coordinator poll; a command publishes its result and reads back after it.
- Warm White reads back from the controller as #c69740 and Cool White as #feffff; the slot lights show those as the
  effect, not as a color. The light entity, `ddp.set_colors` and `ddp.recall_preset` stay for automations.
- Implementation notes (2026-11-06): the controller light keeps its unique ID (`<entry>_controller`) and gets
  `_attr_name = None`; its translation key is now `controller` (icon and the `colors`/`background` attribute names,
  no entity name), replacing `scenes`. Existing entities keep their entity IDs; new ones read
  `light.<entry>_minleon_ndb_pro`, `switch.<entry>_minleon_ndb_pro_power`, `select.<entry>_minleon_ndb_pro_scene`,
  `light.<entry>_minleon_ndb_pro_color_N`, and the DDP lights `light.<entry>_generic_ddp_output_N`. The command logic
  (turn on with an effect, off = controller Off plus blanking the DDP-driven segments, recall a preset, set the
  palette or one slot) lives in `commands.py` (`ControllerCommands`, `runtime.commands`), shared by the light, Power,
  Scene and the Color lights; the hand-back to the scene and `sync_holds` are unchanged. The last scene for a bare
  turn_on is the coordinator's `last_effect` (any poll or command that saw a non-Off effect), restored by the
  controller light's extra data, so Power on finds it too. Polls reach the DDP lights through
  `ControllerCommands.follow_poll`, which setup registers as the coordinator's `on_poll` (cleared on unload), so
  disabling the controller light no longer stops polls from moving the DDP lights. A slot command publishes the new palette (no read-back), except before the first
  read, when the other slots are unknown and the palette is read back. A Color light's bare turn_on sends the slot's
  last non-black color sent or read (restore extra data `last_color`; a white is remembered too, so clearing a white
  and turning on again brings it back), red `(255, 0, 0)` by default; a brightness scales the color sent, a named white is always sent exact.
  Warm White reads back as `#c69740` = `(198, 151, 64)`. Switch and select are removed on a switch to Generic with the
  controller device (the device registry removes its entities); the light platform drops stale controller lights.

## 2026-11-06: the Power switch is the controller device's main entity

- Field test of 0.3.1: the controller device should read Power (top, the device's name), then Color 1..5, then
  Scene. The controller light is dropped: the `switch` "Power" is the main entity (no entity name), the five Color
  lights and the Scene `select` follow (HA sorts the rest by name), and the controller's intensity becomes a `number`
  "Intensity" (percent, entity category config) so it sits in Configuration, not Controls.
- `ddp.recall_preset` and `ddp.set_colors` target the Power switch now (switch domain); the palette and background
  attributes move to it. The Scene select keeps leaving the controller's "Off" out so Power on restores the last scene.
- The old controller light entity (`<entry>_controller`) is removed at setup like other stale entities; automations
  that targeted it must move to the Power switch or the Scene select.
- Palette slots get a `select` "Mode N" (Color / Warm White / Cool White) next to "Color N". Mode is read from the
  slot: #c69740 or #ffffff = Warm White, #feffff = Cool White, anything else (black included) = Color. Choosing a
  white sends it at once; choosing Color clears the slot (black: the light shows off, pick a color to set it). While
  the mode is a white, Color N is an on/off light (no color picker; off clears the slot, on re-sends the white);
  while Color, it is an RGB light. The Warm/Cool White effects on Color N are gone; `ddp.set_colors` keeps the names.
- Follow-up (0.4.1): Color N supports RGB and color temperature (2700-6500 K) instead of turning into an on/off
  light: a white slot reports color temp (Warm White 2700 K, Cool White 6500 K), a custom color RGB, black off; a
  temperature turn_on sends Warm White up to 4600 K, else Cool White, exact. Selecting Mode = Color re-sends the slot's last custom color (default red) instead of clearing it,
  so an HA scene that restores Mode and Color together cannot race a cleared slot; that decision is taken under the
  coordinator lock from the current palette, so a concurrent Color N command is never overwritten by a stale snapshot. Presets are HA scenes: they
  capture Power, Scene, Mode/Color 1-5, Intensity and the DDP lights; the NDB-Pro's 20 stored presets stay
  recall-only (pyminleon never writes saved settings).
- 0.4.2: the Mode 1..5 selects are removed. Color N's Temperature tab (Warm White 2700 K, Cool White 6500 K) is
  the white picker, and its Color tab a custom color, so a separate mode is redundant. The select platform removes
  the stale `mode_N` entities at setup. `last_custom` and the Mode = Color restore logic go with them; a bare
  turn_on still restores the slot's last value.
- 0.4.2 (cont.): the Intensity number goes too. The controller device's main entity is a brightness-only `light`
  (on/off = the controller's Power, brightness = its intensity, no effects), replacing the Power switch; the
  controller entities are that light, Color 1..5 and Scene. `ddp.recall_preset` / `ddp.set_colors` target the light
  again. Stale switch/number entities are removed at setup.
- 2026-11-07: Color N's Temperature tab is a continuous range. 2700 K sends the controller's Warm White and 6500 K
  its Cool White, exact (temperatures past either end are clamped to it); anything in between sends Home Assistant's
  kelvin -> RGB (`color_temperature_to_rgb`), scaled by a brightness. The controller runs in a 3-channel RGB input
  mode and converts any RGB to its pixels itself, so snapping to the two presets threw away the in-between
  temperatures. An in-between slot is a custom color and reads back as RGB; only the two preset whites read back as
  a temperature. No in-range kelvin at any brightness yields a value the readback takes for a white (checked over
  2701-6499 K, brightness 1-255), so no guard is needed.

## 2026-11-07: Generic DDP manual setup defines outputs explicitly

- Generic DDP users must be able to address specific pixels, so the manual setup no longer asks for a pixel count and
  makes one "All" light. It asks for the pixel type (RGB = 3 channels per pixel, RGBW = 4) and the number of outputs
  (1-16), then one form per output: name, first pixel, last pixel (1-based, inclusive, across the whole controller).
- Stored as today's segments (`output<N>` keys, 0-based start, length), so the entry shape and version (2) are
  unchanged and no migration is needed. The pixel count is derived: the highest last pixel.
- An output prefilled from the stored or detected layout keeps that segment's key, even when renamed or resized, so
  its light keeps its unique ID, customizations and history (layout keys follow the port, e.g. output1/output3 with
  port 2 empty). An output beyond the prefilled ones gets the first `output<N>` from its position that no kept output
  uses.
- Gaps between outputs are allowed (those pixels are never driven); overlaps are refused (`output_overlap`), as is a
  last pixel before the first (`invalid_range`).
- Adding: a vendor type with a usable layout still creates the entry directly. Generic DDP always walks the manual
  and output steps, prefilled from the detected layout when there is one (pixel type, outputs, names, ranges; the
  layout's title and device info are kept); without a layout the forms show defaults (1 output, pixels 1-50, next
  outputs continuing after the previous one).
- Reconfigure walks the same steps for Generic DDP (always, even if DDP discovery would answer) and for a vendor type
  that gives no layout, prefilled from the stored data type and segments (stored data wins over a detected layout; every entry has stored
  segments, so a reconfigure never needs the layout tier); then the usual reconfigure update (device
  identifiers follow the host, a color order of the other data type is dropped). An output whose key disappears loses
  its light through the existing stale-entity pruning (an output removed by lowering the count, for example); an old
  manual entry's "all" segment stays as output 1's key.

## 2026-11-09: Every controller type defines its outputs in the flow

- Decision: adding and reconfiguring walk the manual (pixel type + number of outputs) and output (name, first and
  last pixel) steps for every controller type, not only Generic DDP or a vendor type without a layout. A vendor
  entry's "<title> Generic DDP" device is configured the same way as a Generic entry.
- Why: the DDP lights are the same for every type, and users want to address specific pixels (split a port into
  several lights, leave pixels undriven) on a vendor controller too. Creating the entry straight from the layout gave
  them no way to do that.
- Adding: the steps are prefilled from the detected layout when there is one (pixel type, outputs, "Output N" names,
  ranges); the layout's title and device info are kept, so submitting the forms unchanged stores what the old direct
  path stored. Without a layout the forms show the defaults, as before.
- Reconfigure: a vendor type re-reads the layout and prefills the steps from it (its data type and segments), but an
  output whose key matches a stored segment keeps the stored name (the user may have renamed it); the layout's
  manufacturer, model and firmware are stored on the final update. Generic DDP, or a vendor type that reports no
  layout, prefills from the stored data type and segments, as before. The socket check now runs on the last output
  step for every path, so an unreachable host re-shows that form.
- Keys: unchanged rules. A prefilled output keeps its segment key (the layout's segment names, e.g. `output1`), so its
  light keeps its unique ID; an output beyond the prefilled ones gets the first free `output<N>` from its position.
- For a vendor type the ranges should normally match the controller's own port setup, since the controller maps the
  DDP channels to its ports; the forms say so, but the flow does not enforce it.
