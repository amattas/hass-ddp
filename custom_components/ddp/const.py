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
CONF_CONTROLLER_TYPE: Final = "controller_type"  # key into controller.CONTROLLER_TYPES; vendor types get a Scenes light
CONF_HTTP_PORT: Final = "http_port"  # test-only, like "port": where the controller's HTTP UI listens
DEFAULT_HTTP_PORT: Final = 80
# Segment dict keys: "key" is the stable aioddp segment name used in unique IDs, "name" is for display.
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
# On by default: the NDB-Pro falls back to its built-in effect about 20 s after DDP stops, even mid solid color.
DEFAULT_KEEPALIVE: Final = 1.0
MIN_KEEPALIVE: Final = 0.5
MAX_KEEPALIVE: Final = 300.0

COLOR_ORDERS: Final = {
    DATA_TYPE_RGB: ["".join(p) for p in permutations("RGB")],
    DATA_TYPE_RGBW: ["".join(p) for p in permutations("RGBW")],
}
DEFAULT_COLOR_ORDER: Final = {DATA_TYPE_RGB: "RGB", DATA_TYPE_RGBW: "RGBW"}

# Effect name aioddp renders for a plain solid color; hidden from the effect list (HA's EFFECT_OFF means solid).
SOLID_EFFECT: Final = "solid"

# Actions
SERVICE_RECALL_PRESET: Final = "recall_preset"
PRESET_COUNT: Final = 20
