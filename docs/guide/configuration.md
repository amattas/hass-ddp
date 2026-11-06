# Configuration

## Controller types

| Type | Lights |
|---|---|
| Generic DDP | One DDP light per output. Works with any DDP controller; nothing else is created |
| Minleon NDB-Pro | The DDP lights, plus a controller device with the controller light, **Power**, **Scene**, and **Color 1** to **Color 5**, and the `ddp.recall_preset` and `ddp.set_colors` actions (see [NDB-Pro built-in scenes](entities.md#ndb-pro-built-in-scenes)) |

The stored type decides: nothing probes the controller at startup. Adding another vendor's controller means one entry
in the controller type registry (`custom_components/ddp/controller.py`) backed by that vendor's library.

## Pixel type

The entry's pixel type (RGB or RGBW) decides every DDP light's color mode: RGBW entries use `rgbw_color`. An NDB-Pro
reports it from its input mode, so it must be set to a 4-channel mode in its own UI for the entry to be RGBW; then
**Reconfigure** the entry to re-read the layout (pixel count, type, and outputs). Details are in
[RGBW](entities.md#rgbw).

## Options

| Option | Default | Notes |
|---|---|---|
| Brightness cap | 60% | Applied to every frame, effects included. Lower it to protect power supplies |
| Color order | RGB / RGBW | Channel order the pixels expect |
| Effect frame rate | 30 fps | 1 to 60 |
| Keepalive interval | 1 s | Re-sends the current frame so the controller doesn't fall back to its built-in effect; 0 turns it off, otherwise at least 0.5 s |

Saving options reloads the entry.

## Data updates

- **Segment lights never poll.** DDP has no read path, so Home Assistant's state is the truth: it is restored after a
  restart and, if the light was on, sent again. Sends are local UDP and raise an error in the action if the socket
  fails.
- **The controller device's entities share one poll of the controller's web UI every 30 seconds**; a command publishes
  what it changed (a preset is read back right after). If the controller does not answer, only those entities become unavailable (logged once, and again when it is back);
  the DDP lights keep working.
- **Patterns are kept across restarts.** A DDP light restores its pattern with the rest of its state.

## Diagnostics

**Settings > Devices & services > DDP Pixel Controller > Download diagnostics** includes the entry, the DDP sender's
counters, and, for an NDB-Pro, the controller's polled state and palette and the controller's config and status
documents. The host, IP addresses, MAC address, and Wi-Fi name are redacted.

