from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import queue
import threading
import time
from typing import Callable

import serial  # type: ignore

from .models import GCodeJob, PortItem, Priority
from .parsers import parse_m114, parse_m115
from .ports import list_serial_ports
from .position_store import PrinterPositionStore
from .serial_worker import SerialWorker, default_timeout_for_command


class PrinterError(RuntimeError):
    pass


class PrinterStateError(PrinterError):
    pass


class PrinterCommandError(PrinterError):
    pass


class PrinterTimeoutError(PrinterError):
    pass


class PrinterStoppedError(PrinterError):
    pass


@dataclass(frozen=True)
class MotionBounds:
    x_min: float = 0.0
    x_max: float = 220.0
    y_min: float = 0.0
    y_max: float = 220.0
    z_min: float = 0.0
    z_max: float = 250.0

    def __post_init__(self) -> None:
        values = (self.x_min, self.x_max, self.y_min, self.y_max, self.z_min, self.z_max)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Motion bounds must be finite")
        if self.x_min >= self.x_max or self.y_min >= self.y_max or self.z_min >= self.z_max:
            raise ValueError("Motion bounds must have increasing minimums and maximums")

    def require(self, x: float, y: float, z: float) -> None:
        if not self.x_min <= x <= self.x_max:
            raise ValueError(f"X must be between {self.x_min:g} and {self.x_max:g} mm")
        if not self.y_min <= y <= self.y_max:
            raise ValueError(f"Y must be between {self.y_min:g} and {self.y_max:g} mm")
        if not self.z_min <= z <= self.z_max:
            raise ValueError(f"Z must be between {self.z_min:g} and {self.z_max:g} mm")


@dataclass(frozen=True)
class Position:
    x: float
    y: float
    z: float
    e: float | None = None


@dataclass(frozen=True)
class PrinterStatus:
    connected: bool
    initialized: bool
    faulted: bool
    port: str | None
    baud: int | None
    position: Position | None
    firmware: str | None
    machine: str | None
    error: str | None
    remembered_position: Position | None = None


@dataclass
class _CommandWaiter:
    event: threading.Event = field(default_factory=threading.Event)
    result: tuple[bool, list[str]] | None = None
    error: PrinterError | None = None


class PrinterController:
    _EXECUTE_COMMANDS = frozenset({"M105", "M114", "M115", "M119", "M400", "M503"})
    _DEFAULT_POSITION_STORE = Path.home() / ".marlinscan" / "printer_positions.json"

    def __init__(
        self,
        bounds: MotionBounds = MotionBounds(),
        *,
        serial_factory: Callable[..., object] = serial.Serial,
        worker_factory: Callable[..., SerialWorker] = SerialWorker,
        port_lister: Callable[..., list[PortItem]] = list_serial_ports,
        connect_delay_s: float = 2.0,
        position_store_path: str | Path = _DEFAULT_POSITION_STORE,
    ) -> None:
        if not math.isfinite(connect_delay_s) or connect_delay_s < 0:
            raise ValueError("connect_delay_s must be finite and non-negative")
        self.bounds = bounds
        self._serial_factory = serial_factory
        self._worker_factory = worker_factory
        self._port_lister = port_lister
        self._connect_delay_s = connect_delay_s
        self._position_store = PrinterPositionStore(position_store_path)
        self._state_lock = threading.RLock()
        self._command_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._serial: object | None = None
        self._worker: SerialWorker | None = None
        self._dispatcher: threading.Thread | None = None
        self._dispatcher_stop: threading.Event | None = None
        self._waiters: dict[str, _CommandWaiter] = {}
        self._tag_sequence = 0
        self._connected = False
        self._initialized = False
        self._faulted = False
        self._port: str | None = None
        self._baud: int | None = None
        self._position: Position | None = None
        self._firmware: str | None = None
        self._machine: str | None = None
        self._error: str | None = None
        self._remembered_position: Position | None = None

    def list_ports(self, *, include_dialin: bool = True) -> list[PortItem]:
        return self._port_lister(include_dialin=include_dialin)

    def status(self) -> PrinterStatus:
        with self._state_lock:
            return PrinterStatus(
                connected=self._connected,
                initialized=self._initialized,
                faulted=self._faulted,
                port=self._port,
                baud=self._baud,
                position=self._position,
                firmware=self._firmware,
                machine=self._machine,
                error=self._error,
                remembered_position=self._remembered_position,
            )

    def connect(self, port: str, *, baud: int = 115200, eol: str = "crlf") -> PrinterStatus:
        port = port.strip()
        if not port:
            raise ValueError("Serial port is required")
        if isinstance(baud, bool) or not isinstance(baud, int) or baud <= 0:
            raise ValueError("Baud rate must be a positive integer")
        if eol not in {"crlf", "lf"}:
            raise ValueError("EOL must be 'crlf' or 'lf'")
        remembered = self._position_store.get(port)
        if remembered is not None:
            self.bounds.require(*remembered)

        with self._lifecycle_lock, self._command_lock:
            with self._state_lock:
                if self._connected or self._worker is not None:
                    raise PrinterStateError("Printer is already connected")

            ser = self._serial_factory(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.2,
                write_timeout=2,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            worker = None
            dispatcher = None
            stop = None
            try:
                if self._connect_delay_s:
                    time.sleep(self._connect_delay_s)
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                worker = self._worker_factory(ser, eol=eol)
                worker.start()
                stop = threading.Event()
                dispatcher = threading.Thread(
                    target=self._event_loop,
                    args=(worker, stop),
                    name="printer-controller-events",
                    daemon=True,
                )
                with self._state_lock:
                    self._serial = ser
                    self._worker = worker
                    self._dispatcher = dispatcher
                    self._dispatcher_stop = stop
                    self._connected = True
                    self._initialized = False
                    self._faulted = False
                    self._port = port
                    self._baud = baud
                    self._position = None
                    self._firmware = None
                    self._machine = None
                    self._error = None
                    self._remembered_position = None if remembered is None else Position(*remembered)
                dispatcher.start()
            except Exception:
                if stop is not None:
                    stop.set()
                if dispatcher is not None:
                    with self._state_lock:
                        if self._dispatcher is dispatcher:
                            self._serial = None
                            self._worker = None
                            self._dispatcher = None
                            self._dispatcher_stop = None
                            self._connected = False
                            self._initialized = False
                            self._port = None
                            self._baud = None
                            self._position = None
                            self._firmware = None
                            self._machine = None
                            self._remembered_position = None
                            self._fail_waiters_locked(PrinterStoppedError("Printer connection failed"))
                if worker is not None:
                    worker.close()
                else:
                    ser.close()
                raise
        return self.status()

    def disconnect(self) -> PrinterStatus:
        with self._lifecycle_lock:
            worker, dispatcher, stop = self._detach(PrinterStoppedError("Printer disconnected"), fault=False)
            if stop is not None:
                stop.set()
            if worker is not None:
                worker.clear_queues()
                worker.close()
            with self._command_lock:
                pass
            if dispatcher is not None and dispatcher is not threading.current_thread():
                dispatcher.join(timeout=1.0)
                if dispatcher.is_alive():
                    raise PrinterError("Printer event dispatcher did not stop")
        return self.status()

    def execute(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
        priority: Priority = "high",
    ) -> list[str]:
        command = command.strip()
        head = command.split(maxsplit=1)[0].upper() if command else ""
        if head not in self._EXECUTE_COMMANDS:
            raise PrinterStateError("Use controller methods for motion and state-changing G-code")
        if priority not in {"high", "low"}:
            raise ValueError("priority must be 'high' or 'low'")
        with self._command_lock:
            return self._execute(command, timeout_s=timeout_s, priority=priority)

    def home(self, *, timeout_s: float = 120.0) -> Position:
        with self._command_lock:
            self._require_operable()
            self._begin_position_change()
            self._execute("G28", timeout_s=timeout_s)
            self._execute("M400", timeout_s=timeout_s)
            position = self._refresh_position_locked()
            return self._acknowledge_position(position)

    def set_origin(self, *, timeout_s: float = 5.0) -> Position:
        with self._command_lock:
            self._require_operable()
            self._begin_position_change()
            self._execute("G92 X0 Y0 Z0", timeout_s=timeout_s)
            position = self._refresh_position_locked()
            if any(abs(value) > 0.001 for value in (position.x, position.y, position.z)):
                raise PrinterCommandError("Printer did not accept the manual origin")
            return self._acknowledge_position(position)

    def restore_remembered_position(self, *, timeout_s: float = 5.0) -> Position:
        with self._command_lock:
            self._require_operable()
            with self._state_lock:
                remembered = self._remembered_position
            if remembered is None:
                raise PrinterStateError("No remembered position is available for this printer port")
            self._begin_position_change()
            self._execute(
                f"G92 X{remembered.x:g} Y{remembered.y:g} Z{remembered.z:g}",
                timeout_s=timeout_s,
            )
            position = self._refresh_position_locked(timeout_s=timeout_s)
            if any(
                abs(actual - expected) > 0.001
                for actual, expected in zip(
                    (position.x, position.y, position.z),
                    (remembered.x, remembered.y, remembered.z),
                )
            ):
                raise PrinterCommandError("Printer did not accept the remembered position")
            return self._acknowledge_position(position)

    def refresh_position(self, *, timeout_s: float = 5.0) -> Position:
        with self._command_lock:
            self._require_ready_position()
            return self._acknowledge_position(self._refresh_position_locked(timeout_s=timeout_s))

    def move_absolute(
        self,
        *,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        speed_mm_s: float = 50.0,
        timeout_s: float = 300.0,
    ) -> Position:
        axes = {name: self._coordinate(value, name) for name, value in (("X", x), ("Y", y), ("Z", z)) if value is not None}
        if not axes:
            raise ValueError("At least one target axis is required")
        speed = self._positive_number(speed_mm_s, "speed_mm_s")

        with self._command_lock:
            current = self._require_ready_position()
            target = Position(
                axes.get("X", current.x),
                axes.get("Y", current.y),
                axes.get("Z", current.z),
                current.e,
            )
            self.bounds.require(target.x, target.y, target.z)
            feed = max(1, int(round(speed * 60.0)))
            words = " ".join(f"{axis}{value:g}" for axis, value in axes.items())
            self._begin_position_change()
            self._execute("G90", timeout_s=5.0)
            self._execute(f"G0 {words} F{feed}", timeout_s=10.0)
            self._execute("M400", timeout_s=timeout_s)
            return self._acknowledge_position(self._refresh_position_locked())

    def move_relative(
        self,
        *,
        dx: float = 0.0,
        dy: float = 0.0,
        dz: float = 0.0,
        speed_mm_s: float = 50.0,
        timeout_s: float = 300.0,
    ) -> Position:
        deltas = tuple(self._coordinate(value, name) for name, value in (("dX", dx), ("dY", dy), ("dZ", dz)))
        if all(abs(value) <= 1e-12 for value in deltas):
            raise ValueError("At least one non-zero delta is required")
        with self._command_lock:
            current = self._require_ready_position()
            target = Position(current.x + deltas[0], current.y + deltas[1], current.z + deltas[2], current.e)
            self.bounds.require(target.x, target.y, target.z)
            return self.move_absolute(
                x=target.x,
                y=target.y,
                z=target.z,
                speed_mm_s=speed_mm_s,
                timeout_s=timeout_s,
            )

    def stop(self) -> PrinterStatus:
        error = PrinterStoppedError("Motion stopped; reconnect and initialize coordinates before motion")
        with self._state_lock:
            worker = self._require_connected_locked()
            port = self._port
            if port is None:
                raise PrinterStateError("Connected printer has no serial port")
            if self._faulted:
                raise PrinterStateError("Printer is faulted")
            self._initialized = False
            self._position = None
            self._error = str(error)
            self._fail_waiters_locked(error)
            worker.clear_queues()
            sent = worker.send_immediate("M410", wait_for_write=True)
        if not sent:
            self._fault_disconnect(worker, PrinterError("Failed to send M410"))
            self._forget_remembered_position(port)
            raise PrinterError("Failed to send M410")
        self._forget_remembered_position(port)
        return self.disconnect()

    def emergency_stop(self) -> PrinterStatus:
        error = PrinterStateError("Emergency stop sent; reconnect before motion")
        with self._state_lock:
            worker = self._require_connected_locked()
            port = self._port
            if port is None:
                raise PrinterStateError("Connected printer has no serial port")
            self._faulted = True
            self._initialized = False
            self._position = None
            self._error = str(error)
            self._fail_waiters_locked(error)
            worker.clear_queues()
            sent = worker.send_immediate("M112", wait_for_write=True)
        if not sent:
            write_error = PrinterError("Failed to send M112")
            self._fault_disconnect(worker, write_error)
            self._forget_remembered_position(port)
            raise write_error
        self._forget_remembered_position(port)
        return self.status()

    def _execute(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
        priority: Priority = "high",
    ) -> list[str]:
        command = command.strip()
        if not command:
            raise ValueError("Command is required")
        timeout = default_timeout_for_command(command) if timeout_s is None else self._positive_number(timeout_s, "timeout_s")
        with self._state_lock:
            worker = self._require_connected_locked()
            if self._faulted:
                raise PrinterStateError("Printer is faulted")
            self._tag_sequence += 1
            tag = f"controller:{self._tag_sequence}"
            waiter = _CommandWaiter()
            self._waiters[tag] = waiter
            queued = worker.enqueue(
                command,
                tag=tag,
                show_in_log=False,
                priority=priority,
                timeout_s=timeout,
            )
            if not queued:
                self._waiters.pop(tag)
                raise PrinterCommandError(f"Failed to queue command: {command}")

        if not waiter.event.wait(timeout):
            error = PrinterTimeoutError(f"Timed out after {timeout:g}s: {command}")
            with self._state_lock:
                self._waiters.pop(tag, None)
            self._fault_disconnect(worker, error)
            raise error
        if waiter.error is not None:
            raise waiter.error
        if waiter.result is None:
            raise PrinterError(f"Command completed without a result: {command}")
        ok, lines = waiter.result
        if not ok:
            raise PrinterCommandError(f"Printer rejected command: {command}")
        return lines

    def _refresh_position_locked(self, *, timeout_s: float = 5.0) -> Position:
        lines = self._execute("M114", timeout_s=timeout_s)
        for line in lines:
            parsed = parse_m114(line)
            if parsed is None:
                continue
            x, y, z, e = parsed
            if x is not None and y is not None and z is not None:
                position = Position(x, y, z, e)
                with self._state_lock:
                    self._position = position
                return position
        raise PrinterCommandError("Could not parse printer position from M114")

    def _event_loop(self, worker: SerialWorker, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                kind, payload = worker.events.get(timeout=0.05)
            except queue.Empty:
                continue
            with self._state_lock:
                if self._worker is not worker:
                    return
            if kind == "line":
                line, _show = payload  # type: ignore[misc]
                self._update_line(str(line))
            elif kind == "job_done":
                job, lines, ok, _elapsed = payload  # type: ignore[misc]
                self._complete_waiter(job, list(lines), bool(ok))
            elif kind == "job_slow":
                job, elapsed = payload  # type: ignore[misc]
                error = PrinterTimeoutError(f"Timed out after {float(elapsed):g}s: {job.command}")
                self._fault_disconnect(worker, error)
                return
            elif kind == "error":
                self._fault_disconnect(worker, PrinterError(str(payload)))
                return
            else:
                self._fault_disconnect(worker, PrinterError(f"Unknown serial worker event: {kind}"))
                return

    def _complete_waiter(self, job: GCodeJob, lines: list[str], ok: bool) -> None:
        for line in lines:
            self._update_line(line)
        with self._state_lock:
            waiter = self._waiters.pop(job.tag, None)
            if waiter is None:
                return
            waiter.result = (ok, lines)
            waiter.event.set()

    def _update_line(self, line: str) -> None:
        position = parse_m114(line)
        firmware = parse_m115(line)
        with self._state_lock:
            if position is not None:
                x, y, z, e = position
                if x is not None and y is not None and z is not None:
                    self._position = Position(x, y, z, e)
            if firmware is not None:
                detected_firmware, machine = firmware
                if detected_firmware is not None:
                    self._firmware = detected_firmware
                if machine is not None:
                    self._machine = machine

    def _require_connected_locked(self) -> SerialWorker:
        if not self._connected or self._worker is None:
            raise PrinterStateError("Printer is disconnected")
        return self._worker

    def _require_operable(self) -> SerialWorker:
        with self._state_lock:
            worker = self._require_connected_locked()
            if self._faulted:
                raise PrinterStateError("Printer is faulted")
            return worker

    def _require_ready_position(self) -> Position:
        with self._state_lock:
            self._require_connected_locked()
            if self._faulted:
                raise PrinterStateError("Printer is faulted")
            if not self._initialized or self._position is None:
                raise PrinterStateError("Initialize coordinates with G28 or G92 X0 Y0 Z0 before motion")
            return self._position

    def _begin_position_change(self) -> None:
        with self._state_lock:
            self._require_connected_locked()
            if self._port is None:
                raise PrinterStateError("Connected printer has no serial port")
            self._position_store.forget(self._port)
            self._remembered_position = None
            self._initialized = False
            self._position = None

    def _acknowledge_position(self, position: Position) -> Position:
        self.bounds.require(position.x, position.y, position.z)
        with self._state_lock:
            self._require_connected_locked()
            if self._faulted:
                raise PrinterStateError("Printer is faulted")
            if self._port is None:
                raise PrinterStateError("Connected printer has no serial port")
            self._position_store.remember(self._port, (position.x, position.y, position.z))
            self._position = position
            self._remembered_position = Position(position.x, position.y, position.z)
            self._initialized = True
            self._error = None
        return position

    def _forget_remembered_position(self, port: str) -> None:
        with self._state_lock:
            self._position_store.forget(port)
            if self._port == port:
                self._remembered_position = None

    def _detach(
        self,
        waiter_error: PrinterError,
        *,
        fault: bool,
    ) -> tuple[SerialWorker | None, threading.Thread | None, threading.Event | None]:
        with self._state_lock:
            worker = self._worker
            dispatcher = self._dispatcher
            stop = self._dispatcher_stop
            self._serial = None
            self._worker = None
            self._dispatcher = None
            self._dispatcher_stop = None
            self._connected = False
            self._initialized = False
            self._port = None
            self._baud = None
            self._position = None
            self._firmware = None
            self._machine = None
            self._remembered_position = None
            self._faulted = self._faulted or fault
            if fault:
                self._error = str(waiter_error)
            elif not self._faulted:
                self._error = None
            self._fail_waiters_locked(waiter_error)
            return worker, dispatcher, stop

    def _fault_disconnect(self, worker: SerialWorker, error: PrinterError) -> None:
        with self._state_lock:
            if self._worker is not worker:
                return
        detached_worker, _dispatcher, stop = self._detach(error, fault=True)
        if stop is not None:
            stop.set()
        if detached_worker is not None:
            detached_worker.clear_queues()
            detached_worker.close()

    def _fail_waiters_locked(self, error: PrinterError) -> None:
        waiters = list(self._waiters.values())
        self._waiters.clear()
        for waiter in waiters:
            waiter.error = error
            waiter.event.set()

    @staticmethod
    def _positive_number(value: float, name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be finite and positive")
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError(f"{name} must be finite and positive")
        return parsed

    @staticmethod
    def _coordinate(value: float, name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be finite")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"{name} must be finite")
        return parsed
