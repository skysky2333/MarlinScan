from __future__ import annotations

import json
from pathlib import Path
import queue
import re
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from v3se_printer.models import GCodeJob, PortItem
from v3se_printer.printer import (
    MotionBounds,
    Position,
    PrinterCommandError,
    PrinterController,
    PrinterError,
    PrinterStateError,
    PrinterStoppedError,
    PrinterTimeoutError,
)
from v3se_printer.serial_worker import SerialWorker


class FakeSerial:
    def __init__(self, **settings: object) -> None:
        self.settings = settings
        self.input_resets = 0
        self.output_resets = 0
        self.closed = False

    def reset_input_buffer(self) -> None:
        self.input_resets += 1

    def reset_output_buffer(self) -> None:
        self.output_resets += 1

    def close(self) -> None:
        self.closed = True


class FakeWorker:
    def __init__(self, serial_port: FakeSerial, *, eol: str) -> None:
        self.serial_port = serial_port
        self.eol = eol
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.commands: list[GCodeJob] = []
        self.immediate: list[str] = []
        self.started = False
        self.closed = False
        self.clear_count = 0
        self.stalled: set[str] = set()
        self.rejected: set[str] = set()
        self.immediate_success = True
        self.position = Position(0.0, 0.0, 0.0, 0.0)

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True
        self.serial_port.close()

    def clear_queues(self) -> None:
        self.clear_count += 1

    def send_immediate(self, command: str, *, wait_for_write: bool = False, timeout_s: float = 2.0) -> bool:
        self.immediate.append(command)
        return self.immediate_success

    def enqueue(
        self,
        command: str,
        *,
        tag: str,
        show_in_log: bool,
        priority: str,
        timeout_s: float,
    ) -> bool:
        job = GCodeJob(command, tag, show_in_log, timeout_s, priority)  # type: ignore[arg-type]
        self.commands.append(job)
        if command in self.stalled:
            return True
        lines = self._run(command)
        for line in lines:
            self.events.put(("line", (line, False)))
        self.events.put(("job_done", (job, lines, command not in self.rejected, 0.001)))
        return True

    def _run(self, command: str) -> list[str]:
        if command == "M115":
            return ["FIRMWARE_NAME:Marlin 2.1 MACHINE_TYPE:Ender-3 V3 SE", "ok"]
        if command == "M114":
            p = self.position
            return [f"X:{p.x:g} Y:{p.y:g} Z:{p.z:g} E:{p.e or 0:g}", "ok"]
        if command == "G28":
            self.position = Position(0.0, 0.0, 0.0, self.position.e)
        elif command.startswith("G92 "):
            values = {axis: float(value) for axis, value in re.findall(r"\b([XYZ])([-+]?\d*\.?\d+)", command)}
            self.position = Position(
                values.get("X", self.position.x),
                values.get("Y", self.position.y),
                values.get("Z", self.position.z),
                self.position.e,
            )
        elif command.startswith("G0 "):
            values = {axis: float(value) for axis, value in re.findall(r"\b([XYZ])([-+]?\d*\.?\d+)", command)}
            self.position = Position(
                values.get("X", self.position.x),
                values.get("Y", self.position.y),
                values.get("Z", self.position.z),
                self.position.e,
            )
        return ["ok"]


class SerialWorkerTests(unittest.TestCase):
    def test_close_propagates_serial_transport_failure(self) -> None:
        class FailingSerial:
            def close(self) -> None:
                raise RuntimeError("serial close failed")

        worker = SerialWorker(FailingSerial(), eol="lf")  # type: ignore[arg-type]

        with self.assertRaisesRegex(RuntimeError, "serial close failed"):
            worker.close()

        self.assertTrue(worker._stop_event.is_set())


class PrinterControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.position_store_path = Path(self.directory.name) / "printer_positions.json"
        self.serials: list[FakeSerial] = []
        self.workers: list[FakeWorker] = []

        def serial_factory(**settings: object) -> FakeSerial:
            item = FakeSerial(**settings)
            self.serials.append(item)
            return item

        def worker_factory(serial_port: FakeSerial, *, eol: str) -> FakeWorker:
            item = FakeWorker(serial_port, eol=eol)
            self.workers.append(item)
            return item

        self.serial_factory = serial_factory
        self.worker_factory = worker_factory
        self.controller = self._new_controller()
        self.addCleanup(self._disconnect_controller, self.controller)

    def _new_controller(self) -> PrinterController:
        return PrinterController(
            MotionBounds(0, 220, 0, 220, 0, 250),
            serial_factory=self.serial_factory,
            worker_factory=self.worker_factory,  # type: ignore[arg-type]
            port_lister=lambda **_kwargs: [PortItem("Test printer", "/dev/test")],
            connect_delay_s=0,
            position_store_path=self.position_store_path,
        )

    @staticmethod
    def _disconnect_controller(controller: PrinterController) -> None:
        if controller.status().connected:
            controller.disconnect()

    @property
    def worker(self) -> FakeWorker:
        return self.workers[-1]

    def connect(self) -> None:
        self.controller.connect("/dev/test", baud=115200)

    def test_connect_lists_ports_executes_and_disconnects(self) -> None:
        self.assertEqual(self.controller.list_ports(), [PortItem("Test printer", "/dev/test")])

        status = self.controller.connect("/dev/test", baud=250000, eol="lf")
        self.assertTrue(status.connected)
        self.assertFalse(status.initialized)
        self.assertFalse(status.faulted)
        self.assertEqual(status.port, "/dev/test")
        self.assertEqual(status.baud, 250000)
        self.assertTrue(self.worker.started)
        self.assertEqual(self.worker.eol, "lf")
        self.assertEqual(self.serials[0].input_resets, 1)
        self.assertEqual(self.serials[0].output_resets, 1)

        lines = self.controller.execute("M115")
        self.assertIn("FIRMWARE_NAME:Marlin 2.1 MACHINE_TYPE:Ender-3 V3 SE", lines)
        self.assertEqual(self.controller.status().firmware, "Marlin 2.1")
        self.assertEqual(self.controller.status().machine, "Ender-3 V3 SE")

        status = self.controller.disconnect()
        self.assertFalse(status.connected)
        self.assertIsNone(status.port)
        self.assertIsNone(status.baud)
        self.assertTrue(self.worker.closed)
        self.assertTrue(self.serials[0].closed)

    def test_motion_requires_coordinate_initialization_and_bounded_methods(self) -> None:
        self.connect()

        with self.assertRaisesRegex(PrinterStateError, "Initialize coordinates"):
            self.controller.move_absolute(x=10)
        with self.assertRaisesRegex(PrinterStateError, "controller methods"):
            self.controller.execute("G0 X10")
        self.assertFalse(any(job.command.startswith("G0") for job in self.worker.commands))

        self.assertEqual(self.controller.home(), Position(0.0, 0.0, 0.0, 0.0))
        self.assertTrue(self.controller.status().initialized)
        position = self.controller.move_absolute(x=20, y=30, z=4, speed_mm_s=25)
        self.assertEqual(position, Position(20.0, 30.0, 4.0, 0.0))
        self.assertIn("G0 X20 Y30 Z4 F1500", [job.command for job in self.worker.commands])

        before = len(self.worker.commands)
        with self.assertRaisesRegex(ValueError, "X must be between"):
            self.controller.move_absolute(x=221)
        with self.assertRaisesRegex(ValueError, "Z must be between"):
            self.controller.move_relative(dz=-5)
        self.assertEqual(len(self.worker.commands), before)

    def test_manual_origin_initializes_and_relative_motion_uses_absolute_target(self) -> None:
        self.connect()
        self.worker.position = Position(80.0, 90.0, 12.0, 0.0)

        origin = self.controller.set_origin()
        self.assertEqual(origin, Position(0.0, 0.0, 0.0, 0.0))
        self.assertTrue(self.controller.status().initialized)
        position = self.controller.move_relative(dx=4, dy=5, dz=1, speed_mm_s=10)

        self.assertEqual(position, Position(4.0, 5.0, 1.0, 0.0))
        commands = [job.command for job in self.worker.commands]
        self.assertIn("G92 X0 Y0 Z0", commands)
        self.assertIn("G0 X4 Y5 Z1 F600", commands)
        self.assertNotIn("G91", commands)

    def test_remembered_position_survives_normal_disconnect_and_requires_explicit_restore(self) -> None:
        self.connect()
        self.controller.home()
        self.controller.move_absolute(x=12.5, y=34.0, z=56.0)
        self.assertEqual(self.controller.status().remembered_position, Position(12.5, 34.0, 56.0))
        self.controller.disconnect()

        saved = json.loads(self.position_store_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["printers"]["/dev/test"], {"x": 12.5, "y": 34.0, "z": 56.0})
        self.assertFalse(self.position_store_path.with_name("printer_positions.json.tmp").exists())

        restored_controller = self._new_controller()
        self.addCleanup(self._disconnect_controller, restored_controller)
        status = restored_controller.connect("/dev/test")
        self.assertFalse(status.initialized)
        self.assertIsNone(status.position)
        self.assertEqual(status.remembered_position, Position(12.5, 34.0, 56.0))
        with self.assertRaisesRegex(PrinterStateError, "Initialize coordinates"):
            restored_controller.move_absolute(x=13.0)

        position = restored_controller.restore_remembered_position()
        self.assertEqual(position, Position(12.5, 34.0, 56.0, 0.0))
        self.assertTrue(restored_controller.status().initialized)
        self.assertIn(
            "G92 X12.5 Y34 Z56",
            [job.command for job in self.workers[-1].commands],
        )

    def test_remembered_position_is_associated_with_exact_printer_port(self) -> None:
        self.connect()
        self.controller.home()
        self.controller.move_absolute(x=10.0, y=20.0, z=30.0)
        self.controller.disconnect()

        other_controller = self._new_controller()
        self.addCleanup(self._disconnect_controller, other_controller)
        status = other_controller.connect("/dev/other")

        self.assertIsNone(status.remembered_position)
        with self.assertRaisesRegex(PrinterStateError, "No remembered position"):
            other_controller.restore_remembered_position()

    def test_normal_stop_interrupts_waiter_without_faulting_and_invalidates_coordinates(self) -> None:
        self.connect()
        self.controller.home()
        self.assertIsNotNone(self.controller.status().remembered_position)
        completed_syncs = sum(job.command == "M400" for job in self.worker.commands)
        self.worker.stalled.add("M400")
        outcome: list[PrinterError] = []

        def move() -> None:
            try:
                self.controller.move_absolute(x=50, timeout_s=5)
            except PrinterError as exc:
                outcome.append(exc)

        thread = threading.Thread(target=move)
        thread.start()
        deadline = time.monotonic() + 1.0
        while sum(job.command == "M400" for job in self.worker.commands) == completed_syncs and time.monotonic() < deadline:
            time.sleep(0.005)

        status = self.controller.stop()
        thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], PrinterStoppedError)
        self.assertFalse(status.connected)
        self.assertFalse(status.faulted)
        self.assertFalse(status.initialized)
        self.assertIsNone(status.remembered_position)
        self.assertEqual(self.worker.immediate, ["M410"])
        self.assertTrue(self.worker.closed)
        self.assertEqual(json.loads(self.position_store_path.read_text(encoding="utf-8"))["printers"], {})
        with self.assertRaises(PrinterStateError):
            self.controller.move_absolute(x=10)

    def test_emergency_stop_faults_immediately_and_requires_reconnect(self) -> None:
        self.connect()
        self.controller.home()

        status = self.controller.emergency_stop()

        self.assertTrue(status.connected)
        self.assertTrue(status.faulted)
        self.assertFalse(status.initialized)
        self.assertIsNone(status.remembered_position)
        self.assertEqual(json.loads(self.position_store_path.read_text(encoding="utf-8"))["printers"], {})
        self.assertEqual(self.worker.immediate, ["M112"])
        with self.assertRaisesRegex(PrinterStateError, "faulted"):
            self.controller.home()
        with self.assertRaisesRegex(PrinterStateError, "faulted"):
            self.controller.execute("M114")

        self.controller.disconnect()
        status = self.controller.connect("/dev/test")
        self.assertTrue(status.connected)
        self.assertFalse(status.faulted)
        self.assertFalse(status.initialized)

    def test_failed_emergency_write_faults_and_disconnects(self) -> None:
        self.connect()
        self.controller.home()
        self.worker.immediate_success = False

        with self.assertRaisesRegex(PrinterError, "Failed to send M112"):
            self.controller.emergency_stop()

        status = self.controller.status()
        self.assertFalse(status.connected)
        self.assertTrue(status.faulted)
        self.assertTrue(self.worker.closed)
        self.assertEqual(self.worker.immediate, ["M112"])

    def test_command_timeout_faults_and_disconnects(self) -> None:
        self.connect()
        self.worker.stalled.add("M115")

        with self.assertRaisesRegex(PrinterTimeoutError, "Timed out"):
            self.controller.execute("M115", timeout_s=0.03)

        status = self.controller.status()
        self.assertFalse(status.connected)
        self.assertTrue(status.faulted)
        self.assertFalse(status.initialized)
        self.assertTrue(self.worker.closed)
        self.assertTrue(self.serials[0].closed)

    def test_serial_worker_error_faults_and_releases_waiters(self) -> None:
        self.connect()
        self.worker.stalled.add("M115")
        outcome: list[PrinterError] = []

        def execute() -> None:
            try:
                self.controller.execute("M115", timeout_s=5)
            except PrinterError as exc:
                outcome.append(exc)

        thread = threading.Thread(target=execute)
        thread.start()
        deadline = time.monotonic() + 1.0
        while not self.worker.commands and time.monotonic() < deadline:
            time.sleep(0.005)
        self.worker.events.put(("error", "USB disconnected"))
        thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(str(outcome[0]), "USB disconnected")
        self.assertTrue(self.controller.status().faulted)
        self.assertFalse(self.controller.status().connected)

    def test_disconnect_interrupts_active_command(self) -> None:
        self.connect()
        self.worker.stalled.add("M115")
        outcome: list[PrinterError] = []

        def execute() -> None:
            try:
                self.controller.execute("M115", timeout_s=5)
            except PrinterError as exc:
                outcome.append(exc)

        thread = threading.Thread(target=execute)
        thread.start()
        deadline = time.monotonic() + 1.0
        while not self.worker.commands and time.monotonic() < deadline:
            time.sleep(0.005)

        status = self.controller.disconnect()
        thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], PrinterStoppedError)
        self.assertFalse(status.connected)
        self.assertTrue(self.worker.closed)

    def test_failed_rehome_revokes_initialized_coordinates(self) -> None:
        self.connect()
        self.controller.home()
        self.worker.rejected.add("G28")

        with self.assertRaisesRegex(PrinterCommandError, "rejected"):
            self.controller.home()

        status = self.controller.status()
        self.assertTrue(status.connected)
        self.assertFalse(status.initialized)
        self.assertIsNone(status.position)
        self.assertIsNone(status.remembered_position)

    def test_failed_motion_clears_persisted_position(self) -> None:
        self.connect()
        self.controller.home()
        self.controller.move_absolute(x=10.0)
        self.worker.rejected.add("G0 X20 F3000")

        with self.assertRaisesRegex(PrinterCommandError, "rejected"):
            self.controller.move_absolute(x=20.0)

        status = self.controller.status()
        self.assertFalse(status.initialized)
        self.assertIsNone(status.position)
        self.assertIsNone(status.remembered_position)
        self.assertEqual(json.loads(self.position_store_path.read_text(encoding="utf-8"))["printers"], {})

    def test_malformed_position_store_fails_during_controller_creation(self) -> None:
        self.position_store_path.write_text(
            '{"version": 1, "printers": {"/dev/test": {"x": "unknown", "y": 2, "z": 3}}}',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "finite numbers"):
            self._new_controller()


if __name__ == "__main__":
    unittest.main()
