"""Constants for the DDP Pixel Controller integration."""

from __future__ import annotations

from enum import StrEnum
from itertools import permutations
from typing import Final

DOMAIN: Final = "ddp"

# Entry data
CONF_PIXEL_COUNT: Final = "pixel_count"
CONF_DATA_TYPE: Final = "data_type"
CONF_SEGMENTS: Final = "segments"
CONF_MANUFACTURER: Final = "manufacturer"
CONF_MODEL: Final = "model"
CONF_FIRMWARE: Final = "firmware"
CONF_CONTROLLER_TYPE: Final = "controller_type"  # key into controller.CONTROLLER_TYPES; vendor types add a device
CONF_HTTP_PORT: Final = "http_port"  # test-only, like "port": where the controller's HTTP UI listens
DEFAULT_HTTP_PORT: Final = 80
# Segment dict keys: "key" is the stable aioddp segment name used in unique IDs, "name" is for display.
SEGMENT_KEY: Final = "key"
SEGMENT_NAME: Final = "name"
SEGMENT_START: Final = "start"
SEGMENT_LENGTH: Final = "length"
# 0.6.0 stored a per-output "reversed" flag; it is ignored (the controller applies its port reverse itself) and the
# flow no longer writes it.

DATA_TYPE_RGB: Final = "rgb"
DATA_TYPE_RGBW: Final = "rgbw"
DATA_TYPES: Final = [DATA_TYPE_RGB, DATA_TYPE_RGBW]

# Options
CONF_BRIGHTNESS_CAP: Final = "brightness_cap"  # percent
CONF_COLOR_ORDER: Final = "color_order"
CONF_FPS: Final = "fps"
CONF_KEEPALIVE: Final = "keepalive"  # seconds, 0 = off
CONF_PIXEL_LIGHTS: Final = "pixel_lights"  # one light per pixel of every output, next to the output lights

DEFAULT_BRIGHTNESS_CAP: Final = 60
DEFAULT_FPS: Final = 30
# On by default: the NDB-Pro falls back to its built-in effect about 20 s after DDP stops, even mid solid color.
DEFAULT_KEEPALIVE: Final = 1.0
MIN_KEEPALIVE: Final = 0.5
MAX_KEEPALIVE: Final = 300.0
DEFAULT_PIXEL_LIGHTS: Final = False  # opt-in: a 122-pixel strip would add 122 entities

COLOR_ORDERS: Final = {
    DATA_TYPE_RGB: ["".join(p) for p in permutations("RGB")],
    DATA_TYPE_RGBW: ["".join(p) for p in permutations("RGBW")],
}
DEFAULT_COLOR_ORDER: Final = {DATA_TYPE_RGB: "RGB", DATA_TYPE_RGBW: "RGBW"}

# Effect name aioddp renders for a plain solid color; hidden from the effect list (HA's EFFECT_OFF means solid).
SOLID_EFFECT: Final = "solid"


class LightSource(StrEnum):
    """What lights a DDP light's pixels: nothing, or HA over DDP (the light is on). A controller scene never shares
    the pixels with DDP: scene mode and DDP mode are exclusive, and in scene mode every DDP light is off. Stored in
    restore data; an earlier version's ``scene`` and ``blanked`` read as not driven by DDP.
    """

    OFF = "off"
    DDP = "ddp"


# Unique-ID suffix of the "All outputs" light, which spans the whole strip (entries with more than one segment).
STRIP_KEY: Final = "strip"
# Unique-ID suffix of a pixel light after its output's key, with the pixel's 1-based physical number: output2_pixel1.
PIXEL_KEY: Final = "pixel"

# Actions
SERVICE_RECALL_PRESET: Final = "recall_preset"
PRESET_COUNT: Final = 20
SERVICE_SET_PATTERN: Final = "set_pattern"
PATTERN_EFFECT: Final = "palette"  # set_pattern's default: repeating blocks of the given colors
MAX_PATTERN_COLORS: Final = 16
ATTR_COLORS: Final = "colors"
ATTR_BLOCK_SIZE: Final = "block_size"
ATTR_PARAMS: Final = "params"
SERVICE_SET_COLORS: Final = "set_colors"  # the controller's palette, on the controller light
ATTR_BACKGROUND: Final = "background"
# set_colors names for the controller's own whites (exact RGB values the vendor maps to its white LEDs)
COLOR_COOL_WHITE: Final = "cool_white"
COLOR_WARM_WHITE: Final = "warm_white"

# The controller's built-in effect that blanks the lights: the controller device's off, not an effect.
CONTROLLER_OFF: Final = "Off"

# Device identifier suffix of a vendor controller's own device (its entities), next to the DDP device's bare host.
CONTROLLER_DEVICE_SUFFIX: Final = ":controller"

type RGB = tuple[int, int, int]
type RGBW = tuple[int, int, int, int]
type Color = RGB | RGBW  # RGBW only on an entry whose data type is rgbw
