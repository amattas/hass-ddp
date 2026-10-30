"""Polling of a vendor controller's built-in effect engine, shared by the Scenes light."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .controller import CONTROLLER_ERRORS, Controller, ControllerState

if TYPE_CHECKING:
    from . import DDPConfigEntry

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)  # one cheap local HTTP read; the controller can also be changed from its web UI


@dataclass(frozen=True, slots=True)
class SceneState:
    """What the controller's built-in effect engine is doing: ``effect`` is ``None`` when unknown."""

    effect: str | None
    intensity: int | None  # percent
    effects: tuple[str, ...]


class DDPScenesCoordinator(DataUpdateCoordinator[SceneState]):
    """Polls the controller; commands share ``lock`` with polls so a stale read cannot undo a command.

    A failed read raises ``UpdateFailed``, which makes the Scenes light unavailable and logs once on the way down
    and once on recovery (the coordinator's own behaviour).
    """

    def __init__(self, hass: HomeAssistant, entry: DDPConfigEntry, controller: Controller) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=entry.title, update_interval=SCAN_INTERVAL)
        self.controller = controller
        self._host: str = entry.data[CONF_HOST]
        self.lock = asyncio.Lock()

    async def _async_update_data(self) -> SceneState:
        async with self.lock:
            return await self.read()

    async def read(self) -> SceneState:
        """Read the controller's state. Callers sending commands hold ``lock``."""
        try:
            state: ControllerState = await self.controller.state()
        except CONTROLLER_ERRORS as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="controller_unreachable",
                translation_placeholders={"host": self._host, "error": str(err)},
            ) from err
        return SceneState(state.effect, state.intensity, tuple(state.effects))

    def publish(self, data: SceneState) -> None:
        """Set the data from a command; if the last poll had failed, log the recovery a successful poll would."""
        if not self.last_update_success:
            _LOGGER.info("Fetching %s data recovered", self.name)
        self.async_set_updated_data(data)

    async def async_read_after_command(self) -> None:
        """Publish what the controller runs now; a failed read marks the light unavailable. Caller holds ``lock``."""
        try:
            self.publish(await self.read())
        except UpdateFailed as err:
            self.async_set_update_error(err)
