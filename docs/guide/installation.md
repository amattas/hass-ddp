# Installation

## Prerequisites

- Home Assistant 2026.10.0 or later.
- The controller reachable from Home Assistant over UDP port 4048 and, for the controller device's entities, TCP port 80.
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
5. Whatever the type, define the outputs: the pixel type (RGB = 3 channels, RGBW = 4) and the number of outputs
   (1-16), then one form per output: a name, its first pixel and its last pixel (1-based, inclusive, counted across
   the whole controller). Outputs may not overlap; gaps are allowed. The pixel count is derived from the highest last
   pixel. When the controller reports a layout the forms are prefilled from it ("Output 1", "Output 2", ... over its
   active outputs), so you can submit them unchanged. With a vendor type, keep the ranges in line with the
   controller's own port setup: the controller maps the DDP channels to its ports. One light is created per output.

| Field | Step | Notes |
|---|---|---|
| Host | Add | IP address or hostname. One entry per host; adding the same host again is refused |
| Controller type | Add, Reconfigure | Generic DDP or a vendor type (see [Controller types](configuration.md#controller-types)) |
| Pixel type | Add, Reconfigure | RGB or RGBW (channels per pixel) |
| Outputs | Add, Reconfigure | Number of outputs, 1-16 |
| Output name | Add, Reconfigure | One per output; names the light |
| First pixel | Add, Reconfigure | 1-based, inclusive, across the whole controller. Outputs may not overlap; gaps are allowed |
| Last pixel | Add, Reconfigure | Inclusive; the highest one sets the pixel count |

DDP is send-only UDP with no acknowledgement, so setup can only check that a socket opens. Setup never sends a frame,
and reloading or removing the integration never blanks the lights.

## Reconfigure

**Reconfigure** on the integration entry changes the host (for example after a new DHCP lease) or the controller type,
and re-reads the output layout the same way adding does (a DDP query, then the controller type's own API). The form
shows the stored pixel type and the layout the controller reports now. Submitting always continues to the same pixel
type, outputs, and per-output steps as adding. When the controller reports a layout they are prefilled from it (its
pixel type, outputs and ranges, keeping the names you gave the outputs), and its device info is stored; so after
switching an NDB-Pro between a 3-channel and a 4-channel input mode, reconfigure the entry to follow it. For
**Generic DDP**, or when the controller reports no layout, they are prefilled with the stored outputs. Change the
ranges, add or remove outputs: outputs that are still there keep their lights; an output that is gone loses its light
(and with a single output left, All outputs goes too). A color order option that does not fit the new pixel type is
reset to the default.

The entry reloads and keeps its devices, entities, and history. Choosing Generic DDP removes the controller device and
its entities.

## Removal

1. **Settings > Devices & services > DDP Pixel Controller**, open the entry's menu and choose **Delete**.
2. If you installed with HACS, remove **DDP Pixel Controller** in HACS and restart Home Assistant; for a manual
   install, delete `<config>/custom_components/ddp` and restart.

Removing the entry stops the integration's frames but does not blank the lights; the controller falls back to its own
effect (about 20 s on an NDB-Pro). Turn the lights off first if you want them dark.

