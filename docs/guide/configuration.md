# Configuration

## Controller types

| Type | Lights |
|---|---|
| Generic DDP | One DDP light per output. Works with any DDP controller; nothing else is created |
| Minleon NDB-Pro | The DDP lights, plus the **Scenes** light and the `ddp.recall_preset` action (see [NDB-Pro built-in scenes](entities.md#ndb-pro-built-in-scenes)) |

The stored type decides: nothing probes the controller at startup. Adding another vendor's controller means one entry
in the controller type registry (`custom_components/ddp/controller.py`) backed by that vendor's library.

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
- **The Scenes light polls the controller's web UI every 30 seconds** and again right after each command it sends. If
  the controller does not answer, only the Scenes light becomes unavailable (logged once, and again when it is back);
  the DDP lights keep working.

## Diagnostics

**Settings > Devices & services > DDP Pixel Controller > Download diagnostics** includes the entry, the DDP sender's
counters, and, for an NDB-Pro, the controller's config and status documents. The host, IP addresses, MAC address, and
Wi-Fi name are redacted.

