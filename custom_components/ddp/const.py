"""Constants for the DDP Pixel Controller integration."""

from __future__ import annotations

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
# Segment dict keys: "key" is the stable pyddp segment name used in unique IDs, "name" is for display.
SEGMENT_KEY: Final = "key"
SEGMENT_NAME: Final = "name"
SEGMENT_START: Final = "start"
SEGMENT_LENGTH: Final = "length"

DATA_TYPE_RGB: Final = "rgb"
DATA_TYPE_RGBW: Final = "rgbw"
DATA_TYPES: Final = [DATA_TYPE_RGB, DATA_TYPE_RGBW]

# Options
CONF_BRIGHTNESS_CAP: Final = "brightness_cap"  # percent
CONF_COLOR_ORDER: Final = "color_order"
CONF_FPS: Final = "fps"
CONF_KEEPALIVE: Final = "keepalive"  # seconds, 0 = off

DEFAULT_BRIGHTNESS_CAP: Final = 60
DEFAULT_FPS: Final = 30
DEFAULT_KEEPALIVE: Final = 0.0
MIN_KEEPALIVE: Final = 0.5
MAX_KEEPALIVE: Final = 300.0

COLOR_ORDERS: Final = {
    DATA_TYPE_RGB: ["".join(p) for p in permutations("RGB")],
    DATA_TYPE_RGBW: ["".join(p) for p in permutations("RGBW")],
}
DEFAULT_COLOR_ORDER: Final = {DATA_TYPE_RGB: "RGB", DATA_TYPE_RGBW: "RGBW"}

# Effect name pyddp renders for a plain solid color; hidden from the effect list (HA's EFFECT_OFF means solid).
SOLID_EFFECT: Final = "solid"
