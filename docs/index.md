# DDP for Home Assistant

A Home Assistant custom integration that drives DDP (Distributed Display Protocol) pixel controllers, such as the
Minleon NDB-Pro, as native light entities. Each controller is one device with one light per output. Every light
supports on/off, brightness, RGB color, and an effect list, so Home Assistant scenes can capture and replay it.

All protocol code lives in [aioddp](https://github.com/amattas/aioddp), and Minleon NDB-Pro control in
[pyminleon](https://github.com/amattas/pyminleon); this integration only maps them onto Home Assistant.

## Guides

- [Installation](guide/installation.md) — install with HACS or by hand, add a controller, reconfigure or remove it.
- [Configuration](guide/configuration.md) — controller types, options, and how state is kept up to date.
- [Entities](guide/entities.md) — the lights, effects, NDB-Pro scenes, actions, and automation examples.

## Supported devices

| Device | Support |
|---|---|
| Minleon NDB-Pro with Rainmin RGB+ pixels | The reference device. DDP lights plus the Scenes light and `ddp.recall_preset` |
| Any other DDP v1 controller (UDP port 4048) | **Generic DDP**: one light per output, or one light for the whole strip. Not tested beyond the DDP spec |

RGB and RGBW pixels are supported. DDP is send-only, so Home Assistant cannot read a controller's state back over DDP;
see [Known limitations](#known-limitations). See [aioddp's compatibility notes](https://github.com/amattas/aioddp/blob/main/docs/compatibility.md)
for which controllers have been exercised.

## Use cases

- Run a roofline in a solid warm white most nights and a chase or rainbow effect for holidays, from dashboards or
  scenes.
- Let the NDB-Pro play the shows stored on it on a schedule (sunset to 23:00) while Home Assistant stays in charge of
  the on/off times.
- Take a section of the strip over for a one-off alert color (for example a doorbell flash) and hand it back after.

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

