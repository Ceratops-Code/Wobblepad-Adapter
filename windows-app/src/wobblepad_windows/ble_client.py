"""Bleak-based discovery, notification streaming, and bounded reconnects."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner

from wobblepad_windows.model import (
    BATTERY_LEVEL_UUID,
    CHARACTERISTIC_UUID,
    SERVICE_UUID,
)

BATTERY_REFRESH_SECONDS = 60.0


@dataclass(frozen=True)
class Board:
    address: str
    name: str


class BleBridge:
    """Own one BLE connection and reconnect a stalled stream up to three times."""

    def __init__(
        self,
        packet_callback: Callable[[bytes], None],
        status_callback: Callable[[str, bool], None],
        battery_callback: Callable[[int | None], None],
    ) -> None:
        self._packet_callback = packet_callback
        self._status_callback = status_callback
        self._battery_callback = battery_callback
        self._address: str | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._client: BleakClient | None = None
        self._last_packet = 0.0
        self._packets = 0

    async def scan(self, timeout: float = 8.0) -> list[Board]:
        """Return compatible advertisements without requiring classic pairing."""

        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        boards: dict[str, Board] = {}
        for device, advertisement in discovered.values():
            name = device.name or advertisement.local_name or ""
            services = {value.lower() for value in (advertisement.service_uuids or [])}
            if "bobo" in name.lower() or SERVICE_UUID in services:
                boards[device.address] = Board(device.address, name or "Compatible balance board")
        return sorted(boards.values(), key=lambda item: (item.name.lower(), item.address))

    async def start(self, address: str) -> None:
        await self.stop()
        self._address = address
        self._connection_task = asyncio.create_task(self._connection_loop(address))

    async def stop(self) -> None:
        self._address = None
        task = self._connection_task
        self._connection_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._disconnect_client()
        self._status_callback("Disconnected", False)
        self._battery_callback(None)

    def _notification(self, _characteristic: object, data: bytearray) -> None:
        loop = asyncio.get_running_loop()
        self._last_packet = loop.time()
        self._packets += 1
        self._packet_callback(bytes(data))

    async def _disconnect_client(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception:
            pass

    async def _read_battery(self, client: BleakClient) -> None:
        try:
            value = await client.read_gatt_char(BATTERY_LEVEL_UUID)
            percent = int(value[0]) if value else -1
            self._battery_callback(percent if 0 <= percent <= 100 else None)
        except Exception:
            self._battery_callback(None)

    async def _connection_loop(self, address: str) -> None:
        attempts = 0
        try:
            while self._address == address and attempts <= 3:
                disconnected = asyncio.Event()

                def on_disconnect(
                    _client: BleakClient,
                    event: asyncio.Event = disconnected,
                ) -> None:
                    event.set()

                reason = "Connection lost"
                try:
                    self._status_callback(
                        "Connecting to the balance board…" if attempts == 0 else f"Reconnecting ({attempts}/3)…",
                        False,
                    )
                    client = BleakClient(address, disconnected_callback=on_disconnect, timeout=12.0)
                    self._client = client
                    await client.connect()
                    await client.start_notify(CHARACTERISTIC_UUID, self._notification)
                    loop = asyncio.get_running_loop()
                    self._last_packet = loop.time()
                    self._packets = 0
                    await self._read_battery(client)
                    next_battery_read = loop.time() + BATTERY_REFRESH_SECONDS
                    self._status_callback("Connected — waiting for tilt packets", True)
                    while self._address == address and client.is_connected:
                        try:
                            await asyncio.wait_for(disconnected.wait(), timeout=0.2)
                            reason = "Balance board disconnected"
                            break
                        except TimeoutError:
                            if loop.time() >= next_battery_read:
                                await self._read_battery(client)
                                next_battery_read = loop.time() + BATTERY_REFRESH_SECONDS
                            timeout = 1.0 if self._packets else 3.0
                            if loop.time() - self._last_packet > timeout:
                                reason = "Tilt stream stalled"
                                break
                    if self._address != address:
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    reason = f"Bluetooth error: {error}"
                finally:
                    await self._disconnect_client()
                    self._battery_callback(None)
                attempts = 0 if self._packets >= 100 else attempts + 1
                if attempts > 3:
                    self._status_callback(f"{reason}. Reconnect stopped; scan again.", False)
                    return
                self._status_callback(f"{reason}. Input paused.", False)
                await asyncio.sleep(0.3)
        finally:
            await self._disconnect_client()
