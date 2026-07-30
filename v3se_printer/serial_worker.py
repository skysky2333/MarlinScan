from __future__ import annotations

import queue
import threading
import time

import serial  # type: ignore

from .models import GCodeJob, Priority


def default_timeout_for_command(cmd: str) -> float:
    head = cmd.strip().split(maxsplit=1)[0].upper() if cmd.strip() else ""
    if head in {"M105", "M114", "M115", "M119"}:
        return 5.0
    if head == "M503":
        return 30.0
    if head == "G28":
        return 120.0
    if head == "G29":
        return 600.0
    if head == "M400":
        return 300.0
    return 30.0


class SerialWorker:
    def __init__(self, ser: serial.Serial, *, eol: str) -> None:
        self._ser = ser
        self._eol = "\r\n" if eol == "crlf" else "\n"
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._io_loop, daemon=True)

        self._tx_high: queue.Queue[GCodeJob] = queue.Queue()
        self._tx_low: queue.Queue[GCodeJob] = queue.Queue()
        self._tx_immediate: queue.Queue[tuple[str, threading.Event | None]] = queue.Queue()

        # Events: ("line", (text, show)), ("job_done", (...)), ("job_slow", (...)), ("error", msg)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        self._ser.close()

    def clear_queues(self) -> None:
        try:
            while True:
                self._tx_high.get_nowait()
        except queue.Empty:
            pass
        try:
            while True:
                self._tx_low.get_nowait()
        except queue.Empty:
            pass
        try:
            while True:
                self._tx_immediate.get_nowait()
        except queue.Empty:
            pass

    def enqueue(
        self,
        command: str,
        *,
        tag: str = "",
        show_in_log: bool = True,
        priority: Priority = "high",
        timeout_s: float | None = None,
    ) -> bool:
        cmd = command.strip()
        if not cmd:
            return False

        job_timeout = default_timeout_for_command(cmd) if timeout_s is None else float(timeout_s)
        job = GCodeJob(
            command=cmd,
            tag=tag,
            show_in_log=show_in_log,
            timeout_s=job_timeout,
            priority=priority,
        )
        if priority == "high":
            self._tx_high.put(job)
        else:
            self._tx_low.put(job)
        return True

    def send_immediate(self, command: str, *, wait_for_write: bool = False, timeout_s: float = 2.0) -> bool:
        cmd = command.strip()
        if not cmd:
            return False
        written = threading.Event() if wait_for_write else None
        self._tx_immediate.put((cmd, written))
        return True if written is None else written.wait(timeout_s)

    def _io_loop(self) -> None:
        active: GCodeJob | None = None
        active_lines: list[str] = []
        active_start = 0.0
        active_warned = False

        try:
            while not self._stop_event.is_set():
                # Out-of-band commands (e.g., M112). These may interrupt normal responses.
                try:
                    while True:
                        cmd, written = self._tx_immediate.get_nowait()
                        try:
                            self._ser.write((cmd + self._eol).encode("ascii", errors="ignore"))
                            self._ser.flush()
                        except serial.SerialException as exc:
                            self.events.put(("error", f"write failed: {exc}"))
                            break
                        if written is not None:
                            written.set()
                        self.events.put(("line", (f"> {cmd}", True)))
                except queue.Empty:
                    pass

                # Start a new job if idle.
                if active is None:
                    job: GCodeJob | None = None
                    try:
                        job = self._tx_high.get_nowait()
                    except queue.Empty:
                        try:
                            job = self._tx_low.get_nowait()
                        except queue.Empty:
                            job = None

                    if job is not None:
                        try:
                            self._ser.write((job.command + self._eol).encode("ascii", errors="ignore"))
                            self._ser.flush()
                        except serial.SerialException as exc:
                            self.events.put(("error", f"write failed: {exc}"))
                            continue
                        active = job
                        active_lines = []
                        active_start = time.monotonic()
                        active_warned = False

                # Read any available line.
                try:
                    raw = self._ser.readline()
                except serial.SerialException as exc:
                    self.events.put(("error", f"Serial error: {exc}"))
                    break

                if raw:
                    text = raw.decode("utf-8", errors="replace").strip()
                    if text:
                        if active is None:
                            self.events.put(("line", (text, True)))
                        else:
                            active_lines.append(text)
                            self.events.put(("line", (text, bool(active.show_in_log))))
                            lower = text.lower()
                            if lower == "ok" or lower.startswith("ok ") or lower.startswith("error"):
                                ok = not lower.startswith("error")
                                elapsed = time.monotonic() - active_start
                                self.events.put(("job_done", (active, active_lines, ok, elapsed)))
                                active = None
                                active_lines = []
                                continue

                # Slow-job warning (do not abort; keep strict request/response ordering).
                if (
                    active is not None
                    and (not active_warned)
                    and (time.monotonic() - active_start) > float(active.timeout_s)
                ):
                    elapsed = time.monotonic() - active_start
                    self.events.put(("job_slow", (active, elapsed)))
                    active_warned = True
        except Exception as exc:
            self.events.put(("error", f"Unhandled IO thread error: {exc}"))
