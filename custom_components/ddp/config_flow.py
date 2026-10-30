"""Config and options flows: discover the controller's layout, pick its controller type, else ask for the layout."""

from __future__ import annotations

import logging
from typing import Any

import probatio
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from pyddp import MAX_FPS, ConfigError, ControllerLayout, DataType, DDPSender, DDPSendError, PyDDPError, discover

from . import CONFIG_VERSION, make_strip
from .const import (
    COLOR_ORDERS,
    CONF_BRIGHTNESS_CAP,
    CONF_COLOR_ORDER,
    CONF_CONTROLLER_TYPE,
    CONF_DATA_TYPE,
    CONF_FIRMWARE,
    CONF_FPS,
    CONF_HTTP_PORT,
    CONF_KEEPALIVE,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_PIXEL_COUNT,
    CONF_SEGMENTS,
    DATA_TYPE_RGB,
    DATA_TYPE_RGBW,
    DATA_TYPES,
    DEFAULT_BRIGHTNESS_CAP,
    DEFAULT_COLOR_ORDER,
    DEFAULT_FPS,
    DEFAULT_HTTP_PORT,
    DEFAULT_KEEPALIVE,
    DOMAIN,
    MAX_KEEPALIVE,
    MIN_KEEPALIVE,
    SEGMENT_KEY,
    SEGMENT_LENGTH,
    SEGMENT_NAME,
    SEGMENT_START,
)
from .controller import CONTROLLER_TYPES, TYPE_GENERIC, detect, fetch_layout

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = probatio.Schema({probatio.Required(CONF_HOST): TextSelector()})
MANUAL_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_PIXEL_COUNT): NumberSelector(
            NumberSelectorConfig(min=1, step=1, mode=NumberSelectorMode.BOX)
        ),
        probatio.Required(CONF_DATA_TYPE, default=DATA_TYPE_RGB): SelectSelector(
            SelectSelectorConfig(options=DATA_TYPES, translation_key=CONF_DATA_TYPE, mode=SelectSelectorMode.LIST)
        ),
    }
)

CONTROLLER_PLACEHOLDER = "detected"


def _controller_schema(default: str) -> probatio.Schema:
    """A select of every registered controller type, ``default`` preselected."""
    options = [SelectOptionDict(value=t.key, label=t.label) for t in CONTROLLER_TYPES.values()]
    return probatio.Schema(
        {
            probatio.Required(CONF_CONTROLLER_TYPE, default=default): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
            )
        }
    )


async def _check_socket(host: str, pixel_count: int, data_type: str) -> str | None:
    """Error key, or ``None`` if a UDP socket to ``host`` opens. Sends nothing, so setup never flashes the lights."""
    try:
        strip = make_strip(host, pixel_count, data_type)
    except ConfigError as err:
        _LOGGER.debug("invalid strip config for %s: %s", host, err)
        return "invalid_config"
    sender = DDPSender(strip)
    try:
        await sender.open()
    except DDPSendError as err:
        _LOGGER.debug("cannot open DDP socket to %s: %s", host, err)
        return "cannot_connect"
    await sender.close()
    return None


def _title(layout: ControllerLayout, host: str) -> str:
    status = layout.status or {}
    name = status.get("name")
    if isinstance(name, str) and name:
        return name
    return layout.model or host


class DDPConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one DDP controller, keyed by host."""

    VERSION = CONFIG_VERSION

    def __init__(self) -> None:
        self._host = ""
        self._detected = TYPE_GENERIC
        self._layout: ControllerLayout | None = None
        self._controller_type = TYPE_GENERIC

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()
            self._host = host
            try:
                self._layout = await discover(host)
            except PyDDPError as err:
                _LOGGER.debug("discovery of %s failed: %s", host, err)
                self._layout = None
            self._detected = await detect(host, DEFAULT_HTTP_PORT)
            if self._layout is None:  # the controller may serve its layout over its own API instead
                self._layout = await fetch_layout(self._detected, host, DEFAULT_HTTP_PORT)
            return await self.async_step_controller()
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA)

    async def async_step_controller(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the controller type, the detected one preselected; Generic DDP is always offered."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._controller_type = user_input[CONF_CONTROLLER_TYPE]
            layout = self._layout
            if layout is None or layout.data_type is None or not layout.active_outputs:
                return await self.async_step_manual()
            data_type = DATA_TYPE_RGBW if layout.data_type is DataType.RGBW else DATA_TYPE_RGB
            if not (error := await _check_socket(self._host, layout.pixel_count, data_type)):
                segments = [
                    {
                        SEGMENT_KEY: segment.name,
                        SEGMENT_NAME: f"Output {output.index + 1}",
                        SEGMENT_START: segment.start,
                        SEGMENT_LENGTH: segment.length,
                    }
                    for output, segment in zip(layout.active_outputs, layout.segments(), strict=True)
                ]
                return self.async_create_entry(
                    title=_title(layout, self._host),
                    data={
                        CONF_HOST: self._host,
                        CONF_PIXEL_COUNT: layout.pixel_count,
                        CONF_DATA_TYPE: data_type,
                        CONF_SEGMENTS: segments,
                        CONF_MANUFACTURER: layout.manufacturer,
                        CONF_MODEL: layout.model,
                        CONF_FIRMWARE: layout.firmware,
                        CONF_CONTROLLER_TYPE: self._controller_type,
                    },
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="controller",
            data_schema=_controller_schema(self._controller_type if user_input is not None else self._detected),
            description_placeholders={CONTROLLER_PLACEHOLDER: CONTROLLER_TYPES[self._detected].label},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the controller type later; the entry reloads so the Scenes light appears or goes."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(
                entry, data_updates={CONF_CONTROLLER_TYPE: user_input[CONF_CONTROLLER_TYPE]}
            )
        detected = await detect(entry.data[CONF_HOST], entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT))
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_controller_schema(entry.data[CONF_CONTROLLER_TYPE]),
            description_placeholders={CONTROLLER_PLACEHOLDER: CONTROLLER_TYPES[detected].label},
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Discovery found nothing usable: ask for the pixel count and data type."""
        errors: dict[str, str] = {}
        if user_input is not None:
            pixel_count = int(user_input[CONF_PIXEL_COUNT])
            data_type = user_input[CONF_DATA_TYPE]
            if not (error := await _check_socket(self._host, pixel_count, data_type)):
                return self.async_create_entry(
                    title=self._host,
                    data={
                        CONF_HOST: self._host,
                        CONF_PIXEL_COUNT: pixel_count,
                        CONF_DATA_TYPE: data_type,
                        CONF_SEGMENTS: [
                            {SEGMENT_KEY: "all", SEGMENT_NAME: "All", SEGMENT_START: 0, SEGMENT_LENGTH: pixel_count}
                        ],
                        CONF_CONTROLLER_TYPE: self._controller_type,
                    },
                )
            errors["base"] = error
        schema = MANUAL_SCHEMA if user_input is None else self.add_suggested_values_to_schema(MANUAL_SCHEMA, user_input)
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DDPOptionsFlow:
        return DDPOptionsFlow()


class DDPOptionsFlow(OptionsFlowWithReload):
    """Brightness cap, color order, effect fps, keepalive. Saving reloads the entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            keepalive = float(user_input[CONF_KEEPALIVE])
            if 0 < keepalive < MIN_KEEPALIVE:
                errors[CONF_KEEPALIVE] = "invalid_keepalive"
            else:
                return self.async_create_entry(
                    data={
                        CONF_BRIGHTNESS_CAP: int(user_input[CONF_BRIGHTNESS_CAP]),
                        CONF_COLOR_ORDER: user_input[CONF_COLOR_ORDER],
                        CONF_FPS: int(user_input[CONF_FPS]),
                        CONF_KEEPALIVE: keepalive,
                    }
                )
        data_type = self.config_entry.data[CONF_DATA_TYPE]
        current = {
            CONF_BRIGHTNESS_CAP: DEFAULT_BRIGHTNESS_CAP,
            CONF_COLOR_ORDER: DEFAULT_COLOR_ORDER[data_type],
            CONF_FPS: DEFAULT_FPS,
            CONF_KEEPALIVE: DEFAULT_KEEPALIVE,
            **self.config_entry.options,
            **(user_input or {}),
        }
        schema = probatio.Schema(
            {
                probatio.Required(CONF_BRIGHTNESS_CAP): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=100, step=1, unit_of_measurement="%", mode=NumberSelectorMode.SLIDER
                    )
                ),
                probatio.Required(CONF_COLOR_ORDER): SelectSelector(
                    SelectSelectorConfig(options=COLOR_ORDERS[data_type], mode=SelectSelectorMode.DROPDOWN)
                ),
                probatio.Required(CONF_FPS): NumberSelector(
                    NumberSelectorConfig(min=1, max=MAX_FPS, step=1, mode=NumberSelectorMode.SLIDER)
                ),
                probatio.Required(CONF_KEEPALIVE): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=MAX_KEEPALIVE, step=0.1, unit_of_measurement="s", mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(schema, current), errors=errors
        )
