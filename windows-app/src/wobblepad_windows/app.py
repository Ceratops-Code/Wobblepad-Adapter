"""Tkinter UI and lifecycle orchestration for the Windows bridge."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import pathlib
import queue
import threading
import time
import tkinter as tk
from collections.abc import Coroutine
from functools import partial
from tkinter import messagebox, ttk
from typing import Any

from wobblepad_windows.ble_client import BleBridge, Board
from wobblepad_windows.keyboard import ArrowKeyEmitter
from wobblepad_windows.model import (
    Calibration,
    ControlSettings,
    JoystickMapper,
    Pose,
    Vector,
    load_state,
    parse_packet,
    save_state,
)


def settings_path() -> pathlib.Path:
    """Keep private calibration in the current user's local application data."""

    root = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home()))
    return root / "WobblePad" / "settings.json"


class AsyncRunner:
    """Run Bleak's asyncio loop on one owned background thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="WobblePad-BLE", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def submit(self, operation: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        return asyncio.run_coroutine_threadsafe(operation, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=3.0)


class WobblePadApp:
    """Desktop controls for BLE connection, calibration, sensitivity, and arrows."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("WobblePad")
        self.root.geometry("690x820")
        self.root.minsize(620, 700)
        self.events: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self.runner = AsyncRunner()
        self.bridge = BleBridge(
            lambda packet: self.events.put(("packet", packet)),
            lambda text, connected: self.events.put(("status", (text, connected))),
            lambda level: self.events.put(("battery", level)),
        )
        self.state_file = settings_path()
        self.controls, self.saved_samples = load_state(self.state_file)
        self.keyboard = ArrowKeyEmitter(repeat_interval=self.controls.repeat_interval_ms / 1000)
        self.boards: dict[str, Board] = {}
        self.current_address: str | None = None
        self.samples: dict[Pose, list[Vector]] = {}
        self.mapper: JoystickMapper | None = None
        self.connected = False
        self.connecting = False
        self.watch_for_board = True
        self.scan_in_progress = False
        self.output_enabled = False
        self.next_output_attempt = 0.0
        self.capture_pose: Pose | None = None
        self.capture_samples: list[Vector] = []
        self.capture_until = 0.0
        self.packet_count = 0
        self.last_rate_count = 0
        self.last_rate_time = time.monotonic()
        self.live_x = 0.0
        self.live_y = 0.0
        self.packet_rate = 0
        self.battery_level: int | None = None
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(20, self._poll)
        self.root.after(0, self._scan_once)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="WobblePad", font=("Segoe UI", 24, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Unofficial BLE-to-arrow bridge for compatible balance boards. Not affiliated with BO&BO Ltd.",
            wraplength=640,
        ).pack(anchor="w", pady=(0, 12))
        self.status = tk.StringVar(value="Watching for a compatible balance board…")
        ttk.Label(frame, textvariable=self.status, wraplength=640).pack(anchor="w", pady=(0, 8))

        connection = ttk.Frame(frame)
        connection.pack(fill="x")
        self.board_choice = tk.StringVar()
        self.board_box = ttk.Combobox(connection, textvariable=self.board_choice, state="readonly")
        self.board_box.pack(side="left", fill="x", expand=True)
        ttk.Button(connection, text="Scan", command=self.scan).pack(side="left", padx=4)
        ttk.Button(connection, text="Connect", command=self.connect).pack(side="left", padx=4)
        ttk.Button(connection, text="Disconnect", command=self.disconnect).pack(side="left")

        self.live = tk.StringVar(value="X +0.00   Y +0.00   •   0 packets/sec   •   BoBo battery —")
        ttk.Label(frame, textvariable=self.live, font=("Consolas", 13)).pack(anchor="w", pady=12)

        output = ttk.LabelFrame(frame, text="Controller output", padding=10)
        output.pack(fill="x", pady=4)
        self.output_status = tk.StringVar(value="Starts automatically after connection and calibration")
        ttk.Label(output, textvariable=self.output_status).pack(side="left", fill="x", expand=True)

        calibration = ttk.LabelFrame(frame, text="Calibration", padding=10)
        calibration.pack(fill="x", pady=8)
        ttk.Label(
            calibration,
            text="Hold each pose, click its button, and stay steady for 3 seconds. Up means away from you.",
            wraplength=620,
        ).pack(anchor="w")
        buttons = ttk.Frame(calibration)
        buttons.pack(fill="x", pady=6)
        self.pose_buttons: dict[Pose, ttk.Button] = {}
        for index, pose in enumerate(Pose):
            button = ttk.Button(buttons, text=pose.value.title(), command=partial(self.capture, pose))
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=2, pady=2)
            buttons.columnconfigure(index % 3, weight=1)
            self.pose_buttons[pose] = button
        ttk.Button(calibration, text="Finish calibration", command=self.finish_calibration).pack(fill="x")

        sensitivity = ttk.LabelFrame(frame, text="Sensitivity", padding=10)
        sensitivity.pack(fill="both", expand=True, pady=4)
        ttk.Label(
            sensitivity,
            text="Higher directional sensitivity needs less tilt. The dead zone suppresses movement near level.",
            wraplength=620,
        ).pack(anchor="w")
        self.control_variables: dict[str, tk.DoubleVar] = {}
        self._add_slider(sensitivity, "left", "Left", self.controls.left * 100, 50, 200)
        self._add_slider(sensitivity, "right", "Right", self.controls.right * 100, 50, 200)
        self._add_slider(sensitivity, "up", "Up / forward", self.controls.up * 100, 50, 200)
        self._add_slider(sensitivity, "down", "Down / backward", self.controls.down * 100, 50, 200)
        self._add_slider(sensitivity, "dead_zone", "Center dead zone", self.controls.dead_zone * 100, 0, 30)
        self._add_slider(
            sensitivity,
            "repeat_interval_ms",
            "Key repeat interval",
            self.controls.repeat_interval_ms,
            100,
            1000,
            " ms",
        )
    def _add_slider(
        self,
        parent: tk.Misc,
        key: str,
        label: str,
        value: float,
        minimum: int,
        maximum: int,
        suffix: str = "%",
    ) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        shown = tk.StringVar()
        variable = tk.DoubleVar(value=value)
        self.control_variables[key] = variable

        def render(raw: str) -> None:
            shown.set(f"{label}: {float(raw):.0f}{suffix}")

        ttk.Label(row, textvariable=shown, width=28).pack(side="left")
        slider = ttk.Scale(row, variable=variable, from_=minimum, to=maximum, command=render)
        slider.pack(side="left", fill="x", expand=True)
        slider.bind("<ButtonRelease-1>", lambda _event: self.apply_controls())
        render(str(value))

    def apply_controls(self) -> None:
        self.controls = ControlSettings(
            left=self.control_variables["left"].get() / 100,
            right=self.control_variables["right"].get() / 100,
            up=self.control_variables["up"].get() / 100,
            down=self.control_variables["down"].get() / 100,
            dead_zone=self.control_variables["dead_zone"].get() / 100,
            repeat_interval_ms=round(self.control_variables["repeat_interval_ms"].get()),
        ).normalized()
        self.keyboard.set_repeat_interval(self.controls.repeat_interval_ms / 1000)
        if self.mapper is not None:
            self.mapper.set_settings(self.controls)
        self._save()

    def scan(self) -> None:
        self.watch_for_board = True
        self._scan_once()

    def _scan_once(self) -> None:
        if not self.watch_for_board or self.scan_in_progress or self.connected or self.connecting:
            return
        self.scan_in_progress = True
        self.status.set("Scanning for compatible balance boards…")
        future = self.runner.submit(self.bridge.scan(timeout=2.5))

        def complete(result: concurrent.futures.Future[Any]) -> None:
            try:
                self.events.put(("scan", result.result()))
            except Exception as error:
                self.events.put(("error", f"Bluetooth scan failed: {error}"))

        future.add_done_callback(complete)

    def connect(self) -> None:
        label = self.board_choice.get()
        board = self.boards.get(label)
        if board is None:
            self.status.set("Watching for a compatible balance board…")
            self._schedule_scan()
            return
        self.watch_for_board = True
        self.connecting = True
        self.stop_output()
        self.current_address = board.address
        self.samples = {pose: list(values) for pose, values in self.saved_samples.get(board.address, {}).items()}
        try:
            calibration = Calibration.from_samples(self.samples) if self.samples else None
        except ValueError:
            calibration = None
            self.samples = {}
        self.mapper = JoystickMapper(calibration, self.controls) if calibration is not None else None
        self._refresh_pose_buttons()
        self.runner.submit(self.bridge.start(board.address))

    def disconnect(self) -> None:
        self.watch_for_board = False
        self.connecting = False
        self.stop_output()
        self.connected = False
        self.runner.submit(self.bridge.stop())

    def capture(self, pose: Pose) -> None:
        if not self.connected:
            self.status.set("Connect and wait for live packets first.")
            return
        self.stop_output()
        self.capture_pose = pose
        self.capture_samples = []
        self.capture_until = time.monotonic() + 3.0
        self.status.set(f"Hold {pose.value.lower()} steady for 3 seconds…")

    def finish_calibration(self) -> None:
        if self.current_address is None:
            self.status.set("Connect a board before calibration.")
            return
        try:
            calibration = Calibration.from_samples(self.samples)
        except ValueError as error:
            self.status.set(str(error))
            return
        self.mapper = JoystickMapper(calibration, self.controls)
        self.saved_samples[self.current_address] = {pose: list(values) for pose, values in self.samples.items()}
        self._save()
        self.status.set("Calibration saved. Arrow output is ready.")
        self.start_output()

    def start_output(self) -> None:
        if not self.connected or self.mapper is None or self.capture_pose is not None:
            self.status.set("Connect the board and finish calibration first.")
            return
        self.mapper.reset()
        self.output_enabled = True
        self.next_output_attempt = 0.0
        self.output_status.set("Sending arrow keys automatically")

    def stop_output(self) -> None:
        self.output_enabled = False
        self.next_output_attempt = 0.0
        try:
            self.keyboard.release_all()
        except OSError as error:
            self.status.set(f"Could not release an arrow key: {error}")
        self.output_status.set("Waiting for connection and calibration")

    def _save(self) -> None:
        try:
            save_state(self.state_file, self.controls, self.saved_samples)
        except OSError as error:
            self.status.set(f"Could not save settings: {error}")

    def _refresh_pose_buttons(self) -> None:
        for pose, button in self.pose_buttons.items():
            count = len(self.samples.get(pose, []))
            button.configure(text=f"{pose.value.title()}{f' ✓ ({count})' if count else ''}")

    def _process_packet(self, packet: bytes) -> None:
        values = parse_packet(packet)
        if values is None:
            return
        self.packet_count += 1
        if self.capture_pose is not None and len(self.capture_samples) < 500:
            self.capture_samples.append(values)
            return
        if self.mapper is None:
            return
        stick = self.mapper.update(values)
        self.live_x = stick.x
        self.live_y = stick.y
        now = time.monotonic()
        if self.output_enabled and now >= self.next_output_attempt:
            try:
                self.keyboard.update(stick.keys)
            except OSError as error:
                try:
                    self.keyboard.release_all()
                except OSError:
                    pass
                self.next_output_attempt = now + 1.0
                self.output_status.set("Arrow output interrupted; retrying automatically")
                self.status.set(f"Arrow output failed: {error}")
        elapsed = now - self.last_rate_time
        rate = int((self.packet_count - self.last_rate_count) / elapsed) if elapsed >= 1.0 else None
        if rate is not None:
            self.last_rate_count = self.packet_count
            self.last_rate_time = now
            self.packet_rate = rate
        self._refresh_live()

    def _refresh_live(self) -> None:
        battery = f"{self.battery_level}%" if self.battery_level is not None else "—"
        self.live.set(
            f"X {self.live_x:+.2f}   Y {self.live_y:+.2f}   •   {self.packet_rate} packets/sec"
            f"   •   BoBo battery {battery}"
        )

    def _poll(self) -> None:
        for _ in range(300):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "packet":
                self._process_packet(payload)
            elif kind == "status":
                text, connected = payload
                self.connected = connected
                self.status.set(text)
                if connected:
                    self.connecting = False
                    self.start_output()
                else:
                    self.stop_output()
                    self.battery_level = None
                    self._refresh_live()
                    if "Reconnect stopped" in text:
                        self.connecting = False
                        self._schedule_scan()
            elif kind == "battery":
                self.battery_level = payload
                self._refresh_live()
            elif kind == "scan":
                self.scan_in_progress = False
                found: list[Board] = payload
                self.boards = {f"{board.name} — {board.address}": board for board in found}
                self.board_box.configure(values=list(self.boards))
                if not self.watch_for_board or self.connected or self.connecting:
                    continue
                if self.boards:
                    self.board_choice.set(next(iter(self.boards)))
                    self.status.set("Board found. Connecting automatically…")
                    self.connect()
                else:
                    self.status.set("No board visible yet. Watching automatically…")
                    self._schedule_scan()
            elif kind == "error":
                self.scan_in_progress = False
                self.status.set(str(payload))
                self._schedule_scan()
        if self.capture_pose is not None and time.monotonic() >= self.capture_until:
            pose = self.capture_pose
            captured = list(self.capture_samples)
            self.capture_pose = None
            self.capture_samples = []
            if len(captured) >= 10:
                self.samples[pose] = captured
                self.status.set(f"{pose.value.title()} captured ({len(captured)} packets).")
                self._refresh_pose_buttons()
            else:
                self.status.set(f"Only {len(captured)} packets captured. Try {pose.value.lower()} again.")
            if self.connected and self.mapper is not None:
                self.start_output()
        self.root.after(20, self._poll)

    def _schedule_scan(self, delay_ms: int = 1000) -> None:
        if self.watch_for_board:
            self.root.after(delay_ms, self._scan_once)

    def close(self) -> None:
        self.stop_output()
        try:
            self.runner.submit(self.bridge.stop()).result(timeout=3.0)
        except (concurrent.futures.TimeoutError, Exception):
            pass
        self.runner.stop()
        self.root.destroy()


def main() -> None:
    """Open the Windows desktop application."""

    if os.name != "nt":
        messagebox.showerror("WobblePad", "The desktop controller currently supports Windows only.")
        return
    root = tk.Tk()
    WobblePadApp(root)
    root.mainloop()
