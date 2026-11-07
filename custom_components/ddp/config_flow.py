"""Config and options flows: discover the controller's layout, pick its controller type, else ask for the outputs."""

from __future__ import annotations

import logging
from typing import Any

import probatio
from aioddp import MAX_FPS, ConfigError, ControllerLayout, DataType, DDPError, DDPSender, DDPSendError, discover
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
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
from .entity import device_identifiers

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = probatio.Schema({probatio.Required(CONF_HOST): TextSelector()})
CONF_OUTPUTS = "outputs"  # manual step: how many outputs the output step asks for
CONF_FIRST_PIXEL = "first_pixel"  # output step: 1-based, inclusive
CONF_LAST_PIXEL = "last_pixel"
MAX_OUTPUTS = 16
DEFAULT_OUTPUT_LENGTH = 50  # pixels a new output's range covers by default

CONTROLLER_PLACEHOLDER = "detected"
DATA_TYPE_PLACEHOLDER = "data_type"
LAYOUT_PLACEHOLDER = "layout"
NO_LAYOUT = "none"  # the layout placeholder when the controller reports no usable layout
OUTPUT_PLACEHOLDER = "output"
OUTPUTS_PLACEHOLDER = "outputs"

PIXEL_SELECTOR = NumberSelector(NumberSelectorConfig(min=1, step=1, mode=NumberSelectorMode.BOX))


def _manual_schema(data_type: str, outputs: int) -> probatio.Schema:
    """The manual step's schema: pixel type and number of outputs, these defaults preselected."""
    return probatio.Schema(
        {
            probatio.Required(CONF_DATA_TYPE, default=data_type): SelectSelector(
                SelectSelectorConfig(options=DATA_TYPES, translation_key=CONF_DATA_TYPE, mode=SelectSelectorMode.LIST)
            ),
            probatio.Required(CONF_OUTPUTS, default=outputs): NumberSelector(
                NumberSelectorConfig(min=1, max=MAX_OUTPUTS, step=1, mode=NumberSelectorMode.BOX)
            ),
        }
    )


def _output_schema(name: str, first: int, last: int) -> probatio.Schema:
    """The output step's schema: name and 1-based inclusive pixel range, these defaults prefilled."""
    return probatio.Schema(
        {
            probatio.Required(CONF_NAME, default=name): TextSelector(),
            probatio.Required(CONF_FIRST_PIXEL, default=first): PIXEL_SELECTOR,
            probatio.Required(CONF_LAST_PIXEL, default=last): PIXEL_SELECTOR,
        }
    )


def _range_error(first: int, last: int, segments: list[dict[str, Any]]) -> str | None:
    """Error key for a 1-based inclusive range, or ``None``; gaps between outputs are fine, overlaps are not."""
    if last < first:
        return "invalid_range"
    if any(first - 1 < s[SEGMENT_START] + s[SEGMENT_LENGTH] and last > s[SEGMENT_START] for s in segments):
        return "output_overlap"
    return None


def _controller_selector() -> SelectSelector:
    """A select of every registered controller type."""
    options = [SelectOptionDict(value=t.key, label=t.label) for t in CONTROLLER_TYPES.values()]
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))


def _controller_schema(default: str) -> probatio.Schema:
    """The controller type step's schema, ``default`` preselected."""
    return probatio.Schema({probatio.Required(CONF_CONTROLLER_TYPE, default=default): _controller_selector()})


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


async def _read_layout(host: str, controller_type: str, http_port: int) -> ControllerLayout | None:
    """The layout over DDP, else over the controller type's own API; ``None`` if neither gives one."""
    try:
        layout = await discover(host)
    except DDPError as err:
        _LOGGER.debug("discovery of %s failed: %s", host, err)
        layout = None
    if layout is None:  # the controller may serve its layout over its own API instead
        layout = await fetch_layout(controller_type, host, http_port)
    return layout


def _layout_data(layout: ControllerLayout | None) -> dict[str, Any] | None:
    """Entry data for a usable layout (a drivable data type and active outputs), else ``None``.

    Segment keys are the layout's segment names, so an output that stays keeps its entity's unique ID.
    """
    if layout is None or layout.data_type is None or not layout.active_outputs:
        return None
    segments = [
        {
            SEGMENT_KEY: segment.name,
            SEGMENT_NAME: f"Output {output.index + 1}",
            SEGMENT_START: segment.start,
            SEGMENT_LENGTH: segment.length,
        }
        for output, segment in zip(layout.active_outputs, layout.segments(), strict=True)
    ]
    return {
        CONF_PIXEL_COUNT: layout.pixel_count,
        CONF_DATA_TYPE: DATA_TYPE_RGBW if layout.data_type is DataType.RGBW else DATA_TYPE_RGB,
        CONF_SEGMENTS: segments,
        CONF_MANUFACTURER: layout.manufacturer,
        CONF_MODEL: layout.model,
        CONF_FIRMWARE: layout.firmware,
    }


def _describe(data: dict[str, Any] | None) -> str:
    """The reconfigure form's summary of a layout read from the controller."""
    if data is None:
        return NO_LAYOUT
    outputs = len(data[CONF_SEGMENTS])
    return f"{data[CONF_DATA_TYPE]}, {outputs} output{'s' if outputs != 1 else ''}, {data[CONF_PIXEL_COUNT]} pixels"


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
        # The manual and output steps: the pixel type, how many outputs, those entered so far, and the stored
        # segments a reconfigure prefills the forms from.
        self._data_type = DATA_TYPE_RGB
        self._outputs = 1
        self._segments: list[dict[str, Any]] = []
        self._prefill: list[dict[str, Any]] = []
        # A Generic DDP entry built over a detected layout keeps that layout's title and device info.
        self._title = ""
        self._device_info: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()
            self._host = host
            self._detected = await detect(host, DEFAULT_HTTP_PORT)
            self._layout = await _read_layout(host, self._detected, DEFAULT_HTTP_PORT)
            return await self.async_step_controller()
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA)

    async def async_step_controller(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the controller type, the detected one preselected; Generic DDP is always offered.

        A vendor type with a usable layout creates the entry from it. Generic DDP, or no usable layout, continues to
        the manual and output steps, prefilled from the detected layout when there is one.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            self._controller_type = user_input[CONF_CONTROLLER_TYPE]
            layout = self._layout
            data = _layout_data(layout)
            if layout is None or data is None or self._controller_type == TYPE_GENERIC:
                if layout is not None and data is not None:
                    self._prefill_from(data[CONF_DATA_TYPE], data[CONF_SEGMENTS])
                    self._title = _title(layout, self._host)
                    self._device_info = {key: data[key] for key in (CONF_MANUFACTURER, CONF_MODEL, CONF_FIRMWARE)}
                return await self.async_step_manual()
            if not (error := await _check_socket(self._host, data[CONF_PIXEL_COUNT], data[CONF_DATA_TYPE])):
                return self.async_create_entry(
                    title=_title(layout, self._host),
                    data={CONF_HOST: self._host, **data, CONF_CONTROLLER_TYPE: self._controller_type},
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="controller",
            data_schema=_controller_schema(self._controller_type if user_input is not None else self._detected),
            description_placeholders={CONTROLLER_PLACEHOLDER: CONTROLLER_TYPES[self._detected].label},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the host or the controller type later, and re-read the layout; the entry reloads.

        A usable layout replaces the stored one (data type, outputs, pixel count, device info), so switching the
        controller between 3- and 4-channel input takes effect. Generic DDP, or a type that gives no layout, walks the
        manual and output steps instead, prefilled from the stored outputs.
        """
        entry = self._get_reconfigure_entry()
        http_port = entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            moved = host != entry.data[CONF_HOST]
            if moved:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
            controller_type = user_input[CONF_CONTROLLER_TYPE]
            layout = None
            if controller_type != TYPE_GENERIC:
                layout = _layout_data(await _read_layout(host, controller_type, http_port))
            if layout is None:
                self._host, self._controller_type = host, controller_type
                self._prefill_from(entry.data[CONF_DATA_TYPE], entry.data[CONF_SEGMENTS])
                return await self.async_step_manual()
            result = await self._reconfigure({CONF_HOST: host, CONF_CONTROLLER_TYPE: controller_type, **layout})
            if not isinstance(result, str):
                return result
            errors["base"] = result
        host = user_input[CONF_HOST].strip() if user_input is not None else entry.data[CONF_HOST]
        chosen = user_input[CONF_CONTROLLER_TYPE] if user_input is not None else entry.data[CONF_CONTROLLER_TYPE]
        detected = await detect(host, http_port)
        layout = _layout_data(await _read_layout(host, chosen, http_port))
        schema = probatio.Schema(
            {
                probatio.Required(CONF_HOST, default=host): TextSelector(),
                probatio.Required(CONF_CONTROLLER_TYPE, default=chosen): _controller_selector(),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            description_placeholders={
                CONTROLLER_PLACEHOLDER: CONTROLLER_TYPES[detected].label,
                DATA_TYPE_PLACEHOLDER: entry.data[CONF_DATA_TYPE],
                LAYOUT_PLACEHOLDER: _describe(layout),
            },
            errors=errors,
        )

    def _prefill_from(self, data_type: str, segments: list[dict[str, Any]]) -> None:
        """Default the manual and output steps to this pixel type and these segments."""
        self._data_type = data_type
        self._prefill = list(segments)
        self._outputs = min(max(len(segments), 1), MAX_OUTPUTS)

    async def _reconfigure(self, updates: dict[str, Any]) -> ConfigFlowResult | str:
        """Apply ``updates`` to the reconfigured entry and reload it, or the socket check's error key."""
        entry = self._get_reconfigure_entry()
        host = updates[CONF_HOST]
        data = {**entry.data, **updates}
        if error := await _check_socket(host, data[CONF_PIXEL_COUNT], data[CONF_DATA_TYPE]):
            return error
        if host != entry.data[CONF_HOST]:
            # Unique IDs of the entities are keyed by the entry, so only the devices' identities follow the host: the
            # DDP device and, for a vendor controller type, the controller device.
            registry = dr.async_get(self.hass)
            for old, new in zip(device_identifiers(entry.data[CONF_HOST]), device_identifiers(host), strict=True):
                if device := registry.async_get_device_by_identifier(old, entry.entry_id):
                    registry.async_update_device(device.id, new_identifiers={new})
        # A color order of the old data type would stop the strip from building.
        options = {
            key: value
            for key, value in entry.options.items()
            if key != CONF_COLOR_ORDER or value in COLOR_ORDERS[data[CONF_DATA_TYPE]]
        }
        return self.async_update_reload_and_abort(
            entry,
            unique_id=host,
            title=host if entry.title == entry.data[CONF_HOST] else entry.title,
            data_updates=updates,
            options=options,
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """No usable layout (or Generic DDP on reconfigure): ask for the pixel type and the number of outputs."""
        if user_input is not None:
            self._data_type = user_input[CONF_DATA_TYPE]
            self._outputs = int(user_input[CONF_OUTPUTS])
            self._segments = []
            return await self.async_step_output()
        return self.async_show_form(step_id="manual", data_schema=_manual_schema(self._data_type, self._outputs))

    async def async_step_output(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """One output's name and pixel range, once per output; after the last one, create or update the entry."""
        errors: dict[str, str] = {}
        index = len(self._segments)
        if user_input is not None:
            first, last = int(user_input[CONF_FIRST_PIXEL]), int(user_input[CONF_LAST_PIXEL])
            if error := _range_error(first, last, self._segments):
                errors["base"] = error
            else:
                segment = {
                    SEGMENT_KEY: f"output{index + 1}",
                    SEGMENT_NAME: user_input[CONF_NAME].strip() or f"Output {index + 1}",
                    SEGMENT_START: first - 1,
                    SEGMENT_LENGTH: last - first + 1,
                }
                if index + 1 < self._outputs:
                    self._segments.append(segment)
                    return await self.async_step_output()
                segments = [*self._segments, segment]
                data = {
                    CONF_PIXEL_COUNT: max(s[SEGMENT_START] + s[SEGMENT_LENGTH] for s in segments),
                    CONF_DATA_TYPE: self._data_type,
                    CONF_SEGMENTS: segments,
                }
                if self.source == SOURCE_RECONFIGURE:
                    updates = {CONF_HOST: self._host, CONF_CONTROLLER_TYPE: self._controller_type, **data}
                    if not isinstance(result := await self._reconfigure(updates), str):
                        return result
                    errors["base"] = result
                elif error := await _check_socket(self._host, data[CONF_PIXEL_COUNT], self._data_type):
                    errors["base"] = error
                else:
                    return self.async_create_entry(
                        title=self._title or self._host,
                        data={
                            CONF_HOST: self._host,
                            **data,
                            **self._device_info,
                            CONF_CONTROLLER_TYPE: self._controller_type,
                        },
                    )
        if user_input is not None:
            defaults = (user_input[CONF_NAME], int(user_input[CONF_FIRST_PIXEL]), int(user_input[CONF_LAST_PIXEL]))
        elif index < len(self._prefill):
            stored = self._prefill[index]
            start = stored[SEGMENT_START]
            defaults = (stored[SEGMENT_NAME], start + 1, start + stored[SEGMENT_LENGTH])
        else:
            first = max((s[SEGMENT_START] + s[SEGMENT_LENGTH] for s in self._segments), default=0) + 1
            defaults = (f"Output {index + 1}", first, first + DEFAULT_OUTPUT_LENGTH - 1)
        return self.async_show_form(
            step_id="output",
            data_schema=_output_schema(*defaults),
            description_placeholders={OUTPUT_PLACEHOLDER: str(index + 1), OUTPUTS_PLACEHOLDER: str(self._outputs)},
            errors=errors,
        )

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
