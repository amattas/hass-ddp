"""Shared fixtures: custom integration enablement, a real loopback DDP listener, and config entries."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ddp.const import (
    CONF_DATA_TYPE,
    CONF_FIRMWARE,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_PIXEL_COUNT,
    CONF_SEGMENTS,
    DOMAIN,
)

HEADER = struct.Struct(">BBBBIH")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ddp in every test."""


@dataclass(frozen=True)
class Packet:
    """One received DDP datagram, decoded."""

    flags: int
    sequence: int
    data_type: int
    destination: int
    offset: int
    data: bytes

    @property
    def push(self) -> bool:
        return bool(self.flags & 0x01)


@dataclass
class Listener(asyncio.DatagramProtocol):
    """Collects every DDP datagram sent to it on 127.0.0.1."""

    packets: list[Packet] = field(default_factory=list)
    port: int = 0
    _arrived: asyncio.Event = field(default_factory=asyncio.Event)

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        flags, seq, dtype, dest, offset, length = HEADER.unpack_from(data)
        self.packets.append(Packet(flags, seq & 0x0F, dtype, dest, offset, data[10 : 10 + length]))
        self._arrived.set()

    async def wait_for(self, predicate: Callable[[Packet], bool], timeout: float = 2.0) -> Packet:
        """Return the first packet (already received or arriving) that matches ``predicate``."""
        async with asyncio.timeout(timeout):
            seen = 0
            while True:
                for packet in self.packets[seen:]:
                    if predicate(packet):
                        return packet
                seen = len(self.packets)
                self._arrived.clear()
                await self._arrived.wait()

    async def settle(self, seconds: float = 0.1) -> None:
        """Give in-flight datagrams time to land."""
        await asyncio.sleep(seconds)


@pytest.fixture
async def udp_listener(socket_enabled: None) -> AsyncIterator[Listener]:
    """A real UDP socket on loopback standing in for the controller."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(Listener, local_addr=("127.0.0.1", 0))
    protocol.port = transport.get_extra_info("sockname")[1]
    try:
        yield protocol
    finally:
        transport.close()


# NDB-Pro-like layout: two outputs, 80 px at slot 1 and 42 px at slot 241, conv 13 (3-channel input).
SEGMENTS = [
    {"key": "output1", "name": "Output 1", "start": 0, "length": 80},
    {"key": "output2", "name": "Output 2", "start": 80, "length": 42},
]


@pytest.fixture
def entry_factory(hass: HomeAssistant, udp_listener: Listener) -> Callable[..., MockConfigEntry]:
    """Build and register an entry pointing at the loopback listener (``port`` is a test-only data key)."""

    def make(*, options: dict[str, Any] | None = None) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="NDB-Pro",
            unique_id="127.0.0.1",
            data={
                "host": "127.0.0.1",
                "port": udp_listener.port,
                CONF_PIXEL_COUNT: 122,
                CONF_DATA_TYPE: "rgb",
                CONF_SEGMENTS: SEGMENTS,
                CONF_MANUFACTURER: "Minleon",
                CONF_MODEL: "NDB-Pro",
                CONF_FIRMWARE: "1.2.3",
            },
            options=options or {},
        )
        entry.add_to_hass(hass)
        return entry

    return make


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, entry_factory: Callable[..., MockConfigEntry]
) -> AsyncIterator[MockConfigEntry]:
    """A loaded entry; unloaded at teardown so no runner outlives the test."""
    entry = entry_factory()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id)
