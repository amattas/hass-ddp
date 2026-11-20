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

Superseded by 2026-11-19 (scene mode and DDP mode are exclusive).


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

## 2026-11-18: One light per pixel, numbered in physical order

- Field request: address single pixels of the NDB-Pro (122 RGB pixels: output 1 = 80, output 2 = 42, the controller
  reverses one port itself). Decided: an opt-in option "One light per pixel" (`pixel_lights`, default off: 122 more
  entities would swamp everyone else). On, every output gets "<output name> Light 1".."Light N" (translation key
  `pixel`, name "{output} Light {number}"), on the DDP device; off, the reload's stale-light pruning removes them.
- Pixel lights are DDP lights over a one-pixel segment (`PixelLight`, a `DDPSegmentLight`), so they share the
  source, hand-off, hold, keepalive and restore machinery: on/off, brightness, RGB or RGBW color. No effects, and
  `ddp.set_pattern` on one is refused (`pixel_light_pattern`; `not_a_ddp_light` would read wrong, it is a DDP light).
- (Superseded 2026-11-19, "Pixel lights follow DDP order": no Reversed flag.) Physical order: Light N is the N-th pixel along the installed string. Each segment stores `reversed` (the flow
  always writes it; an entry from before has none, read as false, so it loads unchanged with no migration): when true
  Light N is DDP pixel `start + length - N`, else `start + N - 1`. What DDP sends is unchanged. The flow prefills it
  from the layout (`OutputInfo.reversed_by_controller`, the NDB-Pro's per-port `rev`) and the output step has a
  "Reversed" box (Generic DDP users set it by hand). Reconfigure keeps the stored flag of a matching key, like its
  name, so a user who unticked it after a hardware test keeps that; a key with no stored flag (an entry from before
  it) or a new output takes the layout's. Prefilled from the stored segments, the stored flag is kept.
- Unique IDs are `<entry>_<segment key>_pixel<N>` with N the physical number, so flipping the flag keeps the
  entities (they map to the other end). Not verified on hardware: whether the NDB-Pro's `rev` puts DDP pixel
  `start + length - 1` at the string's start. If Light 1 lights the far end, the user unticks Reversed; the README
  says so.
- Interaction rule (ownership, generalized): last write wins per pixel. A pixel light taking its pixel turns no
  overlapping light off: its output and All outputs keep lighting their other pixels and stay on, whatever they show.
  A solid color stays; a light showing the controller's scene stays `scene` (the pixel is overridden over DDP and
  held by keepalive); one running an effect or pattern has its runner stopped (it would repaint the pixel) and stays
  `ddp` on its last frame, frozen, with effect off and no pattern, so it is solid for later hand-offs
  (`DDPSegmentLight.async_freeze`). A runner stopped before its first frame (the pixel light and an effect turned on
  at once) left nothing to freeze, and nothing held its pixels; then the light sends its solid color instead
  (`DDPRuntime.sent` checks the held ranges). Repainting an output (any turn_on) takes its pixels from its pixel lights (they
  show off, as All outputs does to an output). Turning a light off blanks all its pixels, the pixel lights' included:
  over a running scene they are blanked with it (held black), otherwise the `ddp` lights inside it show off, since
  its black covers them (before, an output inside All outputs could never be `ddp` while All outputs was, so nothing
  changes for them). Why not the plain rule: an output that showed off after one of its pixels changed, while still
  lighting its other pixels, could not be turned off without turning it on first (turn_off of an off light sends
  nothing). The first cut kept only a solid output on; a verifier found the scene and effect cases left the output
  showing off with lit pixels, so all three now stay on.
- (Superseded 2026-11-19: no light is blanked any more.) Blanking generalized from All outputs: a light with lights inside it (All outputs over the outputs and pixels, an
  output over its pixel lights) is blanked only while one of them still is, and does not hold its black itself; the
  lights inside hold theirs. So un-blanking one pixel during a scene releases just that pixel.
- Cost: overlap lists are computed once at setup (`DDPSegmentLight.link`), and `sync_holds` evaluates "off lights
  hold" once per sync (`DDPRuntime.off_lights_hold`, `DDPSegmentLight.holds`) instead of once per light. One frame is
  a segment-sized packet, so a pixel change sends 3 or 4 bytes, never a full strip; turning on all 122 pixel lights in
  one call takes about 50 ms in the test suite (122 packets plus the off outputs' black).

## 2026-11-19: Scene mode and DDP mode are exclusive

- Decided (user, after hardware checks): on a vendor controller entry (NDB-Pro) the controller's scenes and the DDP
  lights never run together. Why: the controller cannot report the pixels of its own scenes (it answers no DDP query;
  its HTTP API has no pixel readback), so DDP pixels over a running scene could never be shown truthfully, and that
  mixed state caused most of the complexity and the bugs found (the `blanked` source, holds over a scene, freezing and
  hand-offs over a scene). Losing "a few DDP pixels over a running scene" is accepted.
- Scene mode: starting a scene from HA (controller light turn_on, Scene select, `ddp.recall_preset`) turns every DDP
  light off (outputs, All outputs, pixel lights) once the controller took the command: runners stop, nothing is
  sent, nothing is held (`ControllerCommands._hand_back`). A poll that finds a scene running does the same
  (`follow_poll`), so a scene started in the controller's UI or app ends DDP mode within 30 s.
- DDP mode: turning on any DDP light, or `ddp.set_pattern`, while the coordinator's state is a running scene first
  selects the controller's "Off" (`ControllerCommands.leave_scene`, under the coordinator lock), then drives the
  pixels. If that command fails, the action raises `controller_failed` and sends nothing. While the controller is Off
  the DDP lights behave as on Generic DDP; a poll reading "Off" changes nothing.
- Every DDP light change runs under the coordinator lock (then `ownership`, then the light's lock: the commands'
  order), so a scene command or poll never interleaves with it (`DDPSegmentLight._exclusive`). Generic DDP entries
  take only `ownership`, as before.
- The controller light's turn_off is the controller's "Off" alone: in scene mode the DDP lights are already off, and
  in DDP mode the controller already is, so blanking the DDP-driven segments was dead and is removed. Palette
  changes (Color 1-5, `ddp.set_colors`) and the intensity change no mode.
- Startup: setup still only reads the controller (never commands or probes it). If the read state is a running scene,
  the DDP lights restore off and send nothing; otherwise they restore and re-send as before. A stored source of
  `scene` or `blanked` (earlier versions) reads as not driven by DDP, so it is never re-sent.
- Keepalive resends only while some DDP light is on: then every DDP light's pixels are held (black for the off
  ones); with none on, nothing is held. `sync_holds` loses its partial release (with every light holding and All
  outputs spanning the strip, nothing held was ever outside a holding light).
- Removed: the `scene` and `blanked` sources (`LightSource` is `off`/`ddp`; kept as an enum because restore data
  stores it), un-blanking by a bare turn_on, a light deriving its source from the lights inside it, holds over a
  running scene, `async_follow_scene`, `async_controller_off`, `idle`, `_normalize`, and per-light state writes of
  the overlapping lights (no light derives its state from another now; hand-back and freeze write their own).
- Kept, as they apply within DDP mode: the sharing rules between outputs, All outputs and pixel lights (2026-11-18:
  a pixel light never turns its output off, an effect or pattern freezes on a pixel take, a repaint takes the pixel
  lights' pixels, turning a light off turns the lights inside it off).
- Supersedes: 2026-11-06 "DDP lights reflect a running controller scene" (all of it, including the held-pixels
  notes about blanked lights and the hardware question about per-output disable, which no longer arises), the
  controller-off blanking in 2026-11-06 "controller device entities" ("off = controller Off plus blanking the
  DDP-driven segments"), and in 2026-11-18 the scene cases of the interaction rule (a light showing the scene stays
  `scene`; blanking over a running scene; the generalized blanked-source paragraph).
- Implementation notes (2026-11-19, after a verifier refuted the first cut): the DDP lights keep their own model of
  the controller, `DDPRuntime.scene_mode` (scene / DDP / unknown), set by setup's read, every poll, and the
  controller commands; every switch into scene mode happens under `ownership`. A scene command the controller took counts as a running scene even if the
  read-back fails (a recalled preset whose read failed left the model at Off, so the next DDP turn_on skipped the
  Off); the next poll corrects it. In DDP mode a DDP change takes only `ownership` (it never waits on a poll's HTTP:
  with a 2 s page delay a turn_on took about 6 s), and scene commands and polls flip the mode and hand the lights
  back in one `ownership` section, so the change either ran before (and is turned off) or sees the scene. Outside DDP
  mode the change takes the coordinator lock, then `leave_scene`, then `ownership`. Unknown mode (never read): the Off
  is best effort; if the controller does not answer, DDP goes ahead, the mode is assumed DDP until a poll reads the
  controller (so not every DDP change waits on a dead host), and the first good poll turns the DDP lights off if a
  scene runs. Restore runs under `ownership` and syncs the holds, so the off lights next to a restored one are black
  and held (each once; later restores find them held).
- Assumption, not measured on hardware: after HA stops sending (a scene command), the NDB-Pro shows its scene again
  once it notices DDP stopped, possibly only after its DDP timeout (about 20 s). The code sends nothing to speed this
  up; until then the last DDP frame may linger. The README says so.

## 2026-11-19: Pixel lights follow DDP order; the controller applies rev

- Hardware result: the NDB-Pro applies its per-port `rev` itself, so DDP order already is the string's order. With
  the Reversed flag ticked (prefilled from `rev`) the pixel lights came out backwards; unticked, they were right.
- Decided: the Reversed feature is removed. Light N is always DDP pixel `start + N - 1`. Gone: the output step's
  Reversed field, the prefill from `OutputInfo.reversed_by_controller`, the reconfigure rule keeping a stored flag,
  `SegmentConfig.reversed`, `SEGMENT_REVERSED`, and its strings and docs.
- A 0.6.0 entry may have `reversed` stored in its segments: it is ignored at load, and since every flow path builds
  new segment dicts (from the forms, or from the layout plus the stored names), the next reconfigure stops writing
  it. No migration or version bump. Unique IDs `<entry>_<segment key>_pixel<N>` are unchanged.
- Supersedes the Reversed parts of 2026-11-18: the physical-order bullet (including its reconfigure rule for a
  stored flag) and the "flipping the flag keeps the entities" note.

## 2026-11-19: One device per output

- For navigability (an output with 80 pixel lights buried 122 entities in one device): with more than one output,
  each output gets its own device, identifier `(DOMAIN, f"{host}:{segment key}")`, `via_device_id` the DDP device,
  the same manufacturer, model and firmware. The output light is its main entity (`name = None`, so it takes the
  device's name), and its pixel lights are "Light N" on it (translation key `pixel`, placeholder `number`). All
  outputs stays on the DDP device.
- Name: "<entry title> <output name>" for every controller type, e.g. "NDB-Pro Output 1". The DDP device's
  "Generic DDP" suffix on a vendor entry exists only to tell it from the controller device; an output device is
  already told apart by its output name, so "NDB-Pro Generic DDP Output 1" would be noise. The friendly names of a
  Generic entry's lights stay what they were ("NDB-Pro Output 1", "NDB-Pro Output 2 Light 5"); a vendor entry's
  lose "Generic DDP" ("NDB-Pro Output 1" instead of "NDB-Pro Generic DDP Output 1").
- One output: no output device. The DDP device already is that output, so a child device would be one more click
  for nothing. Its light keeps the output's name on the DDP device, and its pixel lights are "<output> Light N"
  there (`OutputPixelLight`, translation key `output_pixel`).
- Upgrade: entities keep their unique IDs and their registry entity IDs (a vendor entry keeps
  `light.<title>_generic_ddp_output_1`); only their device changes, which HA applies when the entity is added. New
  entities get IDs from the new device name (`light.<title>_output_1`; Home Assistant also prefixes the area of a
  device that is in one).
- What the user set on the DDP device carries over (verifier finding: an upgraded DDP device in area "Garden" named
  "My Strip" left 124 of 125 lights outside the area and renamed them "NDB-Pro Output 2 ..."): when the registry
  creates an output device afresh (`register_output_device`, at light setup), it gets the DDP device's area and, if
  the DDP device has a user-set name, the user-set name "<that name> <output name>", so the lights' friendly names
  stay ("My Strip Output 2 Light 5"). Without a user-set name it keeps its default name. A device that already
  exists, or that the registry restores (HA keeps a removed device's name and area, so 2 -> 1 -> 2 outputs brings
  back the user's settings; a re-added entry's orphans too), is never changed: what the user sets on an output
  device later is theirs. Lights with their own area or name are untouched.
  The copy happens only at creation: later changes to the DDP device's area or name are not followed, and a
  `name_by_user` written this way hides later output renames (HA treats it as the user's; verifier found it, left as
  documented: the user renames the device by hand).
- Stale devices: light setup removes every device of the entry it no longer wants (output devices of dropped
  outputs, all of them once a single output is left, the controller device after a switch to Generic DDP). A light
  setup still creates is detached from the device first (`device_id=None`), so the device's removal does not take
  its registry entry (and entity ID) with it; it lands on its new device when added. This replaces the
  controller-device-only removal.
- Reconfigure with a new host moves the output devices' identifiers with the DDP and controller devices (one per
  stored segment key); output devices whose output is then dropped are removed at setup.
- Segment keys are `output<N>`, a layout's segment names or a pre-0.5 `all`, so `host:<key>` never collides with
  `host:controller`.

## 2026-11-19: Every frame is sent as 3 frames 100 ms apart

Hardware test on the NDB-Pro (firmware 2.0):

- With its built-in effect set to Off, a single DDP frame is never shown, not even 70 s after Off.
- A stream of frames 0.5 s or 1 s apart is shown, and the pixels hold about 20 s after the last frame (timed roughly
  by eye).
- A single frame over a running scene is dropped at once and the scene resumes.

So the controller drops a lone frame after a gap. hass-ddp sent one frame per solid-color change (keepalive followed
later), so a change after a gap, for example right after HA turned the controller's scene Off, showed nothing until
the next keepalive.

- Decision: every frame goes out as 3 frames 100 ms apart. The burst lives in aioddp (`DDPSender(repeat_frames=3,
  repeat_interval=0.1)`), not here: the sender resends the current shadow of its held ranges from a background task,
  `send_frame` does not wait (it runs under the coordinator lock), a newer frame restarts the countdown, `release()`
  stops repeats of released pixels, and `close()` cancels them. hass-ddp sets it from `FRAME_REPEATS` /
  `FRAME_REPEAT_INTERVAL` in const.py; it is not a user option.
- Keepalive (default 1 s) and the hold/hand-back logic are unchanged, with one exception (verifier finding: turning
  the last light off released the sender at once, so its black went out once, the lone frame the NDB-Pro drops):
  `sync_holds` releases with `release(drain=True)`, which lets the pending burst finish before the pixels stop being
  resent (keepalive and `held` drop them at once). The scene hand-back keeps the immediate `release()`: no DDP, a
  scheduled repeat included, is sent once a scene takes over.
- aioddp also guarantees no repeat task outlives `close()` (a frame racing `close()` started one; verifier finding).
- Tests: an autouse fixture sends each frame once, because most tests assert the exact packets of a change after a
  0.1 s settle; one test (marker `frame_repeats`) asserts the real burst.

## 2026-11-19: A taken-over light's leftover pixels go black

Bug (reproduced, approved to fix): a DDP light that takes pixels from an overlapping one (`_take_pixels` ->
`async_hand_back`) showed the other off and sent nothing, so the pixels the taker does not cover kept their old
color. All outputs red, then Output 1 green: All outputs and Output 2 showed off, Output 2's 42 pixels stayed red, and
keepalive kept resending it. `sync_holds` counted `sender.held` as covered, and the held range still carried All
outputs' stale red. A later scene all-off skipped the lights already off (HA's reproduce_state), so nothing blanked
Output 2; whether it stuck depended on the gather order of [All off, Output 1 on, Output 2 off].

- Decision: while a DDP light drives, `sync_holds` blacks every off DDP light's pixels that no on (`ddp`) light
  covers; `sender.held` no longer counts as coverage. The off lights' segments are merged into runs and the on ones
  subtracted, one `sender.off` per run, so the pixel-lights option does not send a frame per pixel light.
- A run the sender already holds black (its shadow, `stats.last_frame`) is skipped (deviation from the brief, which
  allowed re-sending: it keeps turn_off from sending its black twice and restore from re-blacking at every light). A
  stale frame is never black, so it is still blanked.
- An on light is never blackened: a `ddp` light covers its segment before its runner's first frame, and a pixel light
  on inside an on output is covered by both. Restore blacks the same pixels as before (off lights not yet held black).
- Unchanged: `sent()` (used by `async_freeze`), the scene hand-back (`_enter_scene_mode`: no black, immediate
  release), and the "nothing driving" branch (`release(drain=True)`).
