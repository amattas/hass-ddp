"""Commands to a vendor controller, shared by the controller device's entities (main light, Scene, Color 1-5).

Every command runs under the coordinator's lock (so a stale poll cannot undo it) and publishes what it changed.

Scene mode and DDP mode are exclusive (the controller cannot report a scene's pixels, so DDP pixels over a running
scene could never be shown truthfully). A command that starts a scene hands the pixels back from DDP once the
controller took it: every DDP light goes off without sending, and keepalive stops resending, so the controller's
scene shows. A DDP light turned on while a scene runs first turns the controller off (``leave_scene``). Turning the
controller off is its "Off" alone (in DDP mode it already is). The palette and intensity are the controller's own
settings, so their commands change no mode.
``follow_poll`` is the coordinator's ``on_poll``: a scene started outside HA turns the DDP lights off the same way.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import COLOR_COOL_WHITE, COLOR_WARM_WHITE, CONTROLLER_OFF, DOMAIN, RGB
from .controller import (
    CONTROLLER_ERRORS,
    EMPTY_COLOR,
    NAMED_COLORS,
    PALETTE_SLOTS,
    WARM_WHITE_SHOWN,
)
from .coordinator import DDPScenesCoordinator, SceneState

if TYPE_CHECKING:
    from . import DDPRuntime

_LOGGER = logging.getLogger(__name__)

# A palette slot holds a custom color or one of the controller's whites (sent as its exact value).
MODE_COLOR = "color"
MODE_WHITES: dict[str, RGB] = {
    COLOR_WARM_WHITE: NAMED_COLORS[COLOR_WARM_WHITE],
    COLOR_COOL_WHITE: NAMED_COLORS[COLOR_COOL_WHITE],
}
# What a slot reads back as for each white (Warm White shows as #c69740 once the controller set it).
_WHITE_READBACK: dict[RGB, str] = {
    **{color: mode for mode, color in MODE_WHITES.items()},
    WARM_WHITE_SHOWN: COLOR_WARM_WHITE,
}


def slot_mode(color: RGB) -> str:
    """The mode of a slot holding ``color``: a white, else ``MODE_COLOR`` (an empty slot included)."""
    return _WHITE_READBACK.get(color, MODE_COLOR)


class ControllerCommands:
    """The commands; ``entity_id`` (the entity the action was called on) names it in errors."""

    def __init__(self, runtime: DDPRuntime, coordinator: DDPScenesCoordinator) -> None:
        self._runtime = runtime
        self.coordinator = coordinator

    @property
    def effects(self) -> list[str]:
        """The controller's effects, without "Off" (the controller's off, not an effect)."""
        data = self.coordinator.data
        return [effect for effect in data.effects if effect != CONTROLLER_OFF] if data is not None else []

    async def turn_on(self, entity_id: str | None, effect: str | None = None) -> None:
        """Run ``effect`` (default: the last scene, else the first), then hand the pixels back."""
        effects = self.effects
        effect = effect or self.coordinator.last_effect or (effects[0] if effects else None)
        if effect not in effects:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_effect",
                translation_placeholders={
                    "effect": str(effect),
                    "effects": ", ".join(effects),
                },
            )
        async with self.coordinator.lock:
            try:
                await self.coordinator.controller.set_effect(effect)
            except CONTROLLER_ERRORS as err:
                raise controller_error(entity_id, err) from err
            await self._hand_back()  # only once the controller took the command: a failure keeps the DDP look
            self._publish(effect=effect)

    async def turn_off(self, entity_id: str | None) -> None:
        """Select the controller's "Off". The DDP lights are off in scene mode, and in DDP mode it already is."""
        async with self.coordinator.lock:
            await self._controller_off(entity_id)

    async def leave_scene(self, entity_id: str | None) -> None:
        """Before a DDP light drives pixels: turn the controller off if it runs a scene (raises ``controller_failed``
        if it cannot, so nothing is sent). Caller holds the coordinator's lock."""
        if self._runtime.scene_running:
            await self._controller_off(entity_id)

    async def _controller_off(self, entity_id: str | None) -> None:
        try:
            await self.coordinator.controller.set_effect(CONTROLLER_OFF)
        except CONTROLLER_ERRORS as err:
            raise controller_error(entity_id, err) from err
        self._publish(effect=CONTROLLER_OFF)

    async def set_intensity(self, entity_id: str | None, percent: int) -> None:
        """Set the controller's intensity (1-100 %) for its scenes; it starts no scene."""
        async with self.coordinator.lock:
            try:
                await self.coordinator.controller.set_intensity(percent)
            except CONTROLLER_ERRORS as err:
                raise controller_error(entity_id, err) from err
            self._publish(intensity=percent)

    async def recall_preset(self, entity_id: str | None, preset: int) -> None:
        """Recall stored show ``preset`` (1-20), hand the pixels back, then read back what the controller runs."""
        async with self.coordinator.lock:
            try:
                await self.coordinator.controller.recall_preset(preset - 1)
            except CONTROLLER_ERRORS as err:
                raise controller_error(entity_id, err) from err
            await self._hand_back()
            await self.coordinator.async_read_after_command()

    async def set_palette(
        self,
        entity_id: str | None,
        colors: Sequence[RGB],
        background: RGB | None = None,
    ) -> None:
        """Make the palette exactly ``colors`` (the other slots empty; none: the controller picks random colors)."""
        async with self.coordinator.lock:
            try:
                await self.coordinator.controller.set_palette(colors, background=background)
            except CONTROLLER_ERRORS as err:
                raise controller_error(entity_id, err) from err
            slots = (*colors, *[EMPTY_COLOR] * (PALETTE_SLOTS - len(colors)))
            if background is None:
                self._publish(colors=slots)
            else:
                self._publish(colors=slots, background=background)

    async def set_color(self, entity_id: str | None, slot: int, color: RGB) -> None:
        """Set palette ``slot`` (1-based) to ``color`` (``EMPTY_COLOR`` empties it).

        Before the first successful read the other slots are unknown, so the palette is read back instead.
        """
        async with self.coordinator.lock:
            try:
                await self.coordinator.controller.set_color(slot, color)
            except CONTROLLER_ERRORS as err:
                raise controller_error(entity_id, err) from err
            data = self.coordinator.data
            if data is None or len(data.colors) != PALETTE_SLOTS:
                await self.coordinator.async_read_after_command()
                return
            colors = list(data.colors)
            colors[slot - 1] = color
            self._publish(colors=tuple(colors))

    async def follow_poll(self, previous: SceneState | None, state: SceneState) -> None:
        """A poll found the controller running a scene (started outside HA: its own UI, app or schedule): every DDP
        light goes off, as when HA starts one. "Off" changes nothing: DDP lights keep running.

        The coordinator's ``on_poll``: it runs under the coordinator's lock before the poll publishes, so commands
        never trigger it.
        """
        if state.effect is not None and state.effect != CONTROLLER_OFF:
            await self._hand_back()

    def _publish(self, **changes: Any) -> None:
        """Publish a commanded change as the coordinator's data, keeping everything the command did not change."""
        self.coordinator.publish(replace(self.coordinator.data or SceneState(None, None, ()), **changes))

    async def _hand_back(self) -> None:
        """Scene mode: every DDP light off without sending, and nothing held, so DDP stops overriding the scene."""
        runtime = self._runtime
        async with runtime.ownership:
            for light in runtime.segment_lights:
                await light.async_hand_back()
            runtime.sender.release()


def controller_error(entity_id: str | None, err: Exception) -> HomeAssistantError:
    """A controller command failed (``controller_failed``)."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="controller_failed",
        translation_placeholders={"entity": str(entity_id), "error": str(err)},
    )
