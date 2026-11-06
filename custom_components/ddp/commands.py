"""Commands to a vendor controller, shared by the controller device's entities (light, Power, Scene, Color 1-5).

Every command runs under the coordinator's lock (so a stale poll cannot undo it) and publishes what it changed. A
command that starts a scene hands the pixels back from DDP once the controller took it: every DDP light shows the
scene, sending nothing, and the held pixels follow (``DDPRuntime.sync_holds``). Turning the controller off blanks
the DDP-driven segments first. The palette is the controller's own setting, so palette commands leave DDP alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from aioddp import DDPError
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import CONTROLLER_OFF, DOMAIN, RGB, LightSource
from .controller import CONTROLLER_ERRORS, EMPTY_COLOR, PALETTE_SLOTS
from .coordinator import DDPScenesCoordinator, SceneState

if TYPE_CHECKING:
    from . import DDPRuntime

ALL_SOURCES = tuple(LightSource)


def to_brightness(intensity: int) -> int:
    """A controller intensity (percent) as an HA brightness."""
    return round(intensity * 255 / 100)


def to_intensity(brightness: int) -> int:
    """An HA brightness as a controller intensity (percent, at least 1)."""
    return max(1, round(brightness * 100 / 255))


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

    async def turn_on(
        self,
        entity_id: str | None,
        effect: str | None = None,
        brightness: int | None = None,
    ) -> None:
        """Run ``effect`` (default: the last scene, else the first) at ``brightness``, then hand the pixels back."""
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
        controller = self.coordinator.controller
        async with self.coordinator.lock:
            try:
                if brightness is not None:
                    await controller.set_intensity(to_intensity(brightness))
                await controller.set_effect(effect)
            except CONTROLLER_ERRORS as err:
                raise controller_error(entity_id, err) from err
            await self._hand_back(entity_id)  # only once the controller took the command: a failure keeps the DDP look
            if brightness is None:
                self._publish(effect=effect)
            else:
                self._publish(effect=effect, intensity=to_intensity(brightness))

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
