"""Commands to a vendor controller, shared by the controller device's entities (Power, Scene, Color 1-5,
Intensity).

Every command runs under the coordinator's lock (so a stale poll cannot undo it) and publishes what it changed. A
command that starts a scene hands the pixels back from DDP once the controller took it: every DDP light shows the
scene, sending nothing, and the held pixels follow (``DDPRuntime.sync_holds``). Turning the controller off blanks
the DDP-driven segments first. The palette and intensity are the controller's own settings, so their commands leave
DDP alone.
``follow_poll`` is the coordinator's ``on_poll``: a scene started or stopped outside HA reaches the DDP lights.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from aioddp import DDPError
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import COLOR_COOL_WHITE, COLOR_WARM_WHITE, CONTROLLER_OFF, DOMAIN, RGB, LightSource
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
ALL_SOURCES = tuple(LightSource)

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
            await self._hand_back(entity_id)  # only once the controller took the command: a failure keeps the DDP look
            self._publish(effect=effect)

    async def turn_off(self, entity_id: str | None) -> None:
        """Fully dark: blank every DDP segment HA drives, show every DDP light off, then select the controller's
        "Off". A segment that cannot be blanked does not stop the "Off"; its error is raised after.
        """
        runtime = self._runtime
        segment_error: HomeAssistantError | None = None
        async with self.coordinator.lock:
            for light in runtime.segment_lights:
                try:
                    await light.async_controller_off()
                except HomeAssistantError as err:  # keep going: the controller's "Off" still darkens the rest
                    segment_error = segment_error or err
            try:
                async with runtime.ownership:
                    await self._sync_holds(entity_id)
            except HomeAssistantError as err:
                segment_error = segment_error or err
            try:
                await self.coordinator.controller.set_effect(CONTROLLER_OFF)
            except CONTROLLER_ERRORS as err:
                raise controller_error(entity_id, err) from err
            self._publish(effect=CONTROLLER_OFF)
        if segment_error is not None:
            raise segment_error

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
            await self._hand_back(entity_id)
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
        """Show on the DDP lights a scene started or stopped outside HA (the controller's own UI or schedule).

        The coordinator's ``on_poll``: it runs under the coordinator's lock before the poll publishes, so commands
        never trigger it. A new scene takes every ``off`` and ``ddp`` light (blanked ones keep their black). Any poll
        of a running scene also shows it on every ``off`` light that holds no pixels (released, so the controller
        lights them again); ``ddp`` lights are then left alone, since DDP may be overriding that scene. "Off" turns
        the lights showing or blanking a scene off and leaves ``ddp`` lights running.
        """
        if state.effect is None:
            return
        before = previous.effect if previous is not None else None
        runtime = self._runtime
        lights = runtime.segment_lights
        sources: tuple[LightSource, ...] = (LightSource.OFF, LightSource.DDP)
        target = LightSource.SCENE
        if state.effect == CONTROLLER_OFF:
            sources, target = (LightSource.SCENE, LightSource.BLANKED), LightSource.OFF
        elif before is None or before == state.effect:
            # Decided before any light changes: one light showing the scene would make the others stop holding.
            lights = [light for light in lights if light.source is LightSource.OFF and not light.holding]
        async with runtime.ownership:
            for light in lights:
                await light.async_follow_scene(target, sources)
            try:
                await runtime.sync_holds()
            except DDPError as err:  # the poll itself succeeded; the next change syncs again
                _LOGGER.warning("Could not send black to the off DDP lights of %s: %s", self.coordinator.name, err)

    def _publish(self, **changes: Any) -> None:
        """Publish a commanded change as the coordinator's data, keeping everything the command did not change."""
        self.coordinator.publish(replace(self.coordinator.data or SceneState(None, None, ()), **changes))

    async def _hand_back(self, entity_id: str | None) -> None:
        """Show the scene on every DDP light without sending, and stop keepalive, so DDP stops overriding it."""
        for light in self._runtime.segment_lights:
            await light.async_follow_scene(LightSource.SCENE, ALL_SOURCES)
        # A segment turned on mid-way (an HA scene applies lights concurrently) keeps its pixels and keepalive.
        async with self._runtime.ownership:
            await self._sync_holds(entity_id)

    async def _sync_holds(self, entity_id: str | None) -> None:
        """``DDPRuntime.sync_holds`` after a command; a failed black frame is the commanding entity's error."""
        try:
            await self._runtime.sync_holds()
        except DDPError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="send_failed",
                translation_placeholders={"entity": str(entity_id), "error": str(err)},
            ) from err


def controller_error(entity_id: str | None, err: Exception) -> HomeAssistantError:
    """A controller command failed (``controller_failed``)."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="controller_failed",
        translation_placeholders={"entity": str(entity_id), "error": str(err)},
    )
