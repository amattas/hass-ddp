"""Controller types: which vendor controller (if any) sits behind the DDP output.

Device communication lives in vendor libraries (pyminleon); this module is the glue. ``Controller`` is the
vendor-neutral surface the Scenes light uses. Adding a vendor is one entry in ``CONTROLLER_TYPES``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from homeassistant.exceptions import ConfigEntryError
from pyddp import ControllerLayout, PyDDPError, parse_config
from pyminleon import MinleonError, NDBController, identify

from .const import DEFAULT_HTTP_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

TYPE_GENERIC: Final = "generic"
TYPE_NDBPRO: Final = "ndbpro"

# What vendor controller libraries raise for any runtime failure (unreachable, timeout, unreadable answer).
CONTROLLER_ERRORS: Final = (MinleonError,)


class ControllerState(Protocol):
    """What a controller's built-in effect engine is doing."""

    @property
    def effect(self) -> str | None: ...
    @property
    def intensity(self) -> int | None: ...  # percent
    @property
    def effects(self) -> Sequence[str]: ...


class ControllerPreset(Protocol):
    """One stored preset slot."""

    @property
    def slot(self) -> int: ...
    @property
    def effect(self) -> str: ...
    @property
    def intensity(self) -> int: ...


class Controller(Protocol):
    """A controller's built-in effects and presets, as the Scenes light drives them. Raises ``CONTROLLER_ERRORS``."""

    async def state(self) -> ControllerState: ...
    async def presets(self) -> Sequence[ControllerPreset]: ...
    async def set_effect(self, name: str) -> None: ...
    async def set_intensity(self, percent: int) -> None: ...
    async def recall_preset(self, slot: int) -> None: ...


type ControllerFactory = Callable[[str, int], Controller]
type LayoutFetcher = Callable[[str, int], Awaitable[ControllerLayout | None]]


@dataclass(frozen=True, slots=True)
class ControllerType:
    """One selectable controller type. ``factory`` is ``None`` for a type with no controller (Generic DDP)."""

    key: str
    label: str
    model: str | None = None  # id the vendor library's ``identify`` reports for this type
    factory: ControllerFactory | None = None
    fetch_layout: LayoutFetcher | None = None  # output layout over the vendor's own API, when DDP can't give it


async def _ndbpro_layout(host: str, http_port: int) -> ControllerLayout | None:
    """The NDB-Pro serves the DDP config and status documents over HTTP; it ignores the DDP query."""
    ctl = NDBController(host, port=http_port)
    try:
        return parse_config(await ctl.config(), host=host, source="http", status=await ctl.status())
    except (*CONTROLLER_ERRORS, PyDDPError) as err:
        _LOGGER.debug("no HTTP layout from %s: %s", host, err)
        return None


CONTROLLER_TYPES: Final[dict[str, ControllerType]] = {
    t.key: t
    for t in (
        ControllerType(TYPE_GENERIC, "Generic DDP"),
        ControllerType(
            TYPE_NDBPRO,
            "Minleon NDB-Pro",
            model="NDBPRO",
            factory=lambda host, port: NDBController(host, port=port),
            fetch_layout=_ndbpro_layout,
        ),
    )
}


def type_for_model(model: str | None) -> str:
    """The type key whose vendor library reports ``model``, else Generic."""
    return next((t.key for t in CONTROLLER_TYPES.values() if model is not None and t.model == model), TYPE_GENERIC)


def create_controller(type_key: str, host: str, http_port: int = DEFAULT_HTTP_PORT) -> Controller | None:
    """The controller client for a stored type; ``None`` for Generic. Opens no connection.

    ``ConfigEntryError`` for a type this release doesn't know (e.g. the entry was written by a newer one).
    """
    if (controller_type := CONTROLLER_TYPES.get(type_key)) is None:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="unknown_controller_type",
            translation_placeholders={"type": type_key},
        )
    factory = controller_type.factory
    return factory(host, http_port) if factory is not None else None


async def detect(host: str, http_port: int = DEFAULT_HTTP_PORT) -> str:
    """The type key of the controller at ``host``; Generic if nothing is recognized or the vendor API fails."""
    try:
        model = await identify(host, port=http_port)
    except CONTROLLER_ERRORS as err:  # includes a host the library rejects
        _LOGGER.debug("controller detection at %s failed: %s", host, err)
        return TYPE_GENERIC
    return type_for_model(model)


async def fetch_layout(type_key: str, host: str, http_port: int = DEFAULT_HTTP_PORT) -> ControllerLayout | None:
    """The layout via the type's vendor API, or ``None`` if the type has none or it didn't answer."""
    controller_type = CONTROLLER_TYPES.get(type_key)
    fetcher = controller_type.fetch_layout if controller_type is not None else None
    return await fetcher(host, http_port) if fetcher is not None else None
