# Installation

## Prerequisites

- Home Assistant 2026.10.0 or later.
- The controller reachable from Home Assistant over UDP port 4048 and, for the Scenes light, TCP port 80.
- `git` available to Home Assistant: the two libraries are installed from pinned git commits (`aioddp` is not yet
  published to PyPI).

## Install

### HACS (custom repository)

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/amattas/hass-ddp` with type **Integration**.
3. Install **DDP Pixel Controller** and restart Home Assistant.

### Manual

1. Copy the `custom_components/ddp` folder of [the repository](https://github.com/amattas/hass-ddp) into `<config>/custom_components/`.
2. Restart Home Assistant.

## Add a controller

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
| Controller type | Add, Reconfigure | Generic DDP or a vendor type (see [Controller types](configuration.md#controller-types)) |
| Pixel count | Add (manual only) | Total pixels the controller drives |
| Pixel type | Add (manual only) | RGB or RGBW (channels per pixel) |

DDP is send-only UDP with no acknowledgement, so setup can only check that a socket opens. Setup never sends a frame,
and reloading or removing the integration never blanks the lights.

## Reconfigure

**Reconfigure** on the integration entry changes the host (for example after a new DHCP lease) or the controller type;
the entry reloads and keeps its device, entities, and history. Choosing Generic DDP removes the Scenes light. The
pixel layout is not re-read: to change it, remove and re-add the entry.

## Removal

1. **Settings > Devices & services > DDP Pixel Controller**, open the entry's menu and choose **Delete**.
2. If you installed with HACS, remove **DDP Pixel Controller** in HACS and restart Home Assistant; for a manual
   install, delete `<config>/custom_components/ddp` and restart.

Removing the entry stops the integration's frames but does not blank the lights; the controller falls back to its own
effect (about 20 s on an NDB-Pro). Turn the lights off first if you want them dark.

