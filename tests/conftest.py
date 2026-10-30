"""Shared fixtures: custom integration enablement, a real loopback DDP listener, a fake NDB-Pro HTTP UI, and entries."""

from __future__ import annotations

import asyncio
import json
import re
import struct
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
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


FIXTURES = Path(__file__).parent / "fixtures" / "ndbpro"
_EFFECT_BUTTON = re.compile(r"""class='(?:psel|pnsel) button' onclick='fx2b\("([^"]+)",""")
_INTENSITY_VALUE = re.compile(r"(class='slider Cint' value=')\d+(')")


@dataclass
class FakeController:
    """The NDB-Pro's HTTP UI on loopback: chunked pages like the firmware, and ``/api/control`` that changes them.

    ``requests`` records (method, target, body). With ``offline`` set, connections are dropped without an answer.
    """

    pages: dict[str, str] = field(
        default_factory=lambda: {
            f"/{name}.html": (FIXTURES / f"{name}.html").read_text() for name in ("pfx2", "pcolors", "pshows")
        }
    )
    requests: list[tuple[str, str, bytes]] = field(default_factory=list)
    offline: bool = False
    page_delay: float = 0.0  # seconds a page read takes to arrive, its content fixed when the request came in
    port: int = 0

    def select(self, effect: str) -> None:
        """Mark ``effect`` selected on the effects page, as the firmware does after a ``{"fx": ...}`` command."""
        self.pages["/pfx2.html"] = _EFFECT_BUTTON.sub(
            lambda m: f"class='{'psel' if m.group(1) == effect else 'pnsel'} button' onclick='fx2b(\"{m.group(1)}\",",
            self.pages["/pfx2.html"],
        )

    def commands(self) -> list[tuple[str, str, bytes]]:
        """Every request except the page reads a poll makes."""
        return [r for r in self.requests if not (r[0] == "GET" and r[1] in ("/pfx2.html", "/pcolors.html"))]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        except asyncio.IncompleteReadError:  # the client gave up (e.g. offline: it saw EOF and hung up first)
            writer.close()
            return
        try:
            if self.offline:
                return
            method, target, _ = head.split("\r\n", 1)[0].split(" ", 2)
            length = re.search(r"(?im)^content-length:\s*(\d+)", head)
            body = await reader.readexactly(int(length.group(1))) if length else b""
            self.requests.append((method, target, body))
            path = target.split("?", 1)[0]
            if method == "POST" and path == "/api/control":
                command = json.loads(body)
                if "fx" in command:
                    self.select(command["fx"])
                if "int" in command:
                    self.pages["/pcolors.html"] = _INTENSITY_VALUE.sub(
                        rf"\g<1>{int(command['int'])}\g<2>", self.pages["/pcolors.html"]
                    )
                reply = b"OK"
            elif method == "GET" and path in self.pages:
                reply = self.pages[path].encode()
                await asyncio.sleep(self.page_delay)
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                return
            # What the firmware sends, even to HTTP/1.0 requests: HTTP/1.1 with a chunked body.
            chunks = b"".join(f"{len(c):x}\r\n".encode() + c + b"\r\n" for c in _split(reply, 1024))
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunks + b"0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()


def _split(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


@pytest.fixture
async def fake_controller(socket_enabled: None) -> AsyncIterator[FakeController]:
    """A loopback NDB-Pro HTTP UI serving the fixture pages."""
    controller = FakeController()
    server = await asyncio.start_server(controller.handle, "127.0.0.1", 0)
    controller.port = server.sockets[0].getsockname()[1]
    try:
        yield controller
    finally:
        server.close()
        await server.wait_closed()


# NDB-Pro-like layout: two outputs, 80 px at slot 1 and 42 px at slot 241, conv 13 (3-channel input).
SEGMENTS = [
    {"key": "output1", "name": "Output 1", "start": 0, "length": 80},
    {"key": "output2", "name": "Output 2", "start": 80, "length": 42},
]


@pytest.fixture
def entry_factory(hass: HomeAssistant, udp_listener: Listener) -> Callable[..., MockConfigEntry]:
    """Build and register an entry pointing at the loopback listener (``port`` is a test-only data key).

    With ``controller``, the entry is a discovered NDB-Pro (model ``NDBPRO``) whose HTTP UI is that fake
    (``http_port`` is test-only too); without, its model is something else, so it gets no controller light.
    """

    def make(*, options: dict[str, Any] | None = None, controller: FakeController | None = None) -> MockConfigEntry:
        extra: dict[str, Any] = {CONF_MODEL: "NDB-Pro"}
        if controller is not None:
            extra = {CONF_MODEL: "NDBPRO", "http_port": controller.port}
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
                CONF_FIRMWARE: "1.2.3",
                **extra,
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
