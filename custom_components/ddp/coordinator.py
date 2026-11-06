"""Polling of a vendor controller's built-in effect engine, shared by the controller device's entities."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONTROLLER_OFF, DOMAIN, RGB
from .controller import CONTROLLER_ERRORS, Controller, ControllerPalette, ControllerState

if TYPE_CHECKING:
    from . import DDPConfigEntry

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)  # one cheap local HTTP read; the controller can also be changed from its web UI


@dataclass(frozen=True, slots=True)
class SceneState:
    """What the controller's built-in effect engine is doing: ``effect`` is ``None`` when unknown.

    ``colors`` are the palette's slots (``EMPTY_COLOR`` for an empty one); the palette is unknown (``()`` and
    ``None``) only when a command lands before the first successful read.
    """

    effect: str | None
    intensity: int | None  # percent
    effects: tuple[str, ...]
    colors: tuple[RGB, ...] = ()
    background: RGB | None = None


class DDPScenesCoordinator(DataUpdateCoordinator[SceneState]):
    """Polls the controller; commands share ``lock`` with polls so a stale read cannot undo a command.

    A failed read raises ``UpdateFailed``, which makes the controller device's entities unavailable and logs once on
    the way down and once on recovery (the coordinator's own behaviour). ``last_effect`` is the last effect other
    than "Off" seen or commanded (a bare turn_on runs it); the Power switch restores it across restarts.
    """

    def __init__(self, hass: HomeAssistant, entry: DDPConfigEntry, controller: Controller) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=entry.title, update_interval=SCAN_INTERVAL)
        self.controller = controller
        self._host: str = entry.data[CONF_HOST]
        self.lock = asyncio.Lock()
        # Called with each polled state (and the one before it) under ``lock``, so a scene started or stopped
        # outside HA reaches the DDP lights before a command can interleave. Commands never call it.
        self.on_poll: Callable[[SceneState | None, SceneState], Awaitable[None]] | None = None
        self.last_effect: str | None = None

    async def _async_update_data(self) -> SceneState:
        async with self.lock:
            state = await self.read()
            if self.on_poll is not None:
                await self.on_poll(self.data, state)
            self._remember(state)
            return state

    async def read(self) -> SceneState:
        """Read the controller's state and palette. Callers sending commands hold ``lock``."""
        try:
            state: ControllerState = await self.controller.state()
            palette: ControllerPalette = await self.controller.palette()
        except CONTROLLER_ERRORS as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="controller_unreachable",
                translation_placeholders={"host": self._host, "error": str(err)},
            ) from err
        return SceneState(
            state.effect, state.intensity, tuple(state.effects), tuple(palette.colors), palette.background
        )

    def publish(self, data: SceneState) -> None:
        """Set the data from a command; if the last poll had failed, log the recovery a successful poll would."""
        if not self.last_update_success:
            _LOGGER.info("Fetching %s data recovered", self.name)
        self._remember(data)
        self.async_set_updated_data(data)

    def _remember(self, state: SceneState) -> None:
        if state.effect is not None and state.effect != CONTROLLER_OFF:
            self.last_effect = state.effect

    async def async_read_after_command(self) -> None:
        """Publish what the controller runs now; a failed read marks the entities unavailable. Caller holds ``lock``."""
        try:
            self.publish(await self.read())
        except UpdateFailed as err:
            self.async_set_update_error(err)
