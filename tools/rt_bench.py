from __future__ import annotations

import argparse
import statistics
import sys
import time

import serial  # type: ignore
from serial.tools import list_ports  # type: ignore


def _now() -> float:
    return time.monotonic()


def _eol_bytes(eol: str) -> bytes:
    return b"\r\n" if eol.lower() == "crlf" else b"\n"


def list_serial_ports() -> list[str]:
    return [p.device for p in list_ports.comports()]


def send_and_wait_ok(
    ser: serial.Serial,
    cmd: str,
    *,
    eol: bytes,
    timeout_s: float,
) -> tuple[float, list[str], bool]:
    cmd = cmd.strip()
    if not cmd:
        return (0.0, [], True)

    t0 = _now()
    ser.write(cmd.encode("ascii", errors="ignore") + eol)
    ser.flush()

    lines: list[str] = []
    deadline = _now() + timeout_s
    ok = False
    while _now() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        lines.append(text)
        lower = text.lower()
        if lower == "ok" or lower.startswith("ok "):
            ok = True
            break
        if lower.startswith("error"):
            ok = False
            break
    return (_now() - t0, lines, ok)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    if pct <= 0:
        return values[0]
    if pct >= 100:
        return values[-1]
    k = (len(values) - 1) * (pct / 100.0)
    f = int(k)
    c = min(len(values) - 1, f + 1)
    if c == f:
        return values[f]
    d = k - f
    return values[f] * (1.0 - d) + values[c] * d


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Benchmark Marlin serial ok timing for tiny XY jog streaming.")
    ap.add_argument("--port", default="/dev/cu.wchusbserial1120", help="Serial port device (e.g. /dev/cu.wchusbserial1120)")
    ap.add_argument("--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    ap.add_argument("--eol", choices=["crlf", "lf"], default="crlf", help="Line ending (default: crlf)")
    ap.add_argument("--home-xy", action="store_true", help="Send G28 X Y before benchmark (may move Z on some firmwares)")
    ap.add_argument(
        "--pre",
        action="append",
        default=[],
        help="Extra G-code to run before moves (repeatable). Use carefully.",
    )
    ap.add_argument(
        "--post",
        action="append",
        default=[],
        help="Extra G-code to run after moves (repeatable). Use carefully.",
    )
    ap.add_argument("--count", type=int, default=200, help="Number of move commands to send (default: 200)")
    ap.add_argument("--dx", type=float, default=1.0, help="Relative X per move in mm (default: 1.0)")
    ap.add_argument("--dy", type=float, default=0.0, help="Relative Y per move in mm (default: 0.0)")
    ap.add_argument("--feed", type=int, default=6000, help="Feedrate in mm/min for moves (default: 6000)")
    ap.add_argument("--pattern", choices=["line", "backforth"], default="backforth", help="Motion pattern")
    ap.add_argument("--warmup", type=int, default=10, help="Warmup moves (not counted in stats)")
    args = ap.parse_args(argv)

    ports = list_serial_ports()
    if args.port not in ports:
        sys.stderr.write("Available ports:\n")
        for p in ports:
            sys.stderr.write(f"  {p}\n")
        sys.stderr.write("\n")
        sys.stderr.write(f"Warning: requested port {args.port!r} not found in list_ports output.\n")

    eol = _eol_bytes(args.eol)

    print("SAFETY:")
    print("- This will move X/Y. It will not send Z moves.")
    print("- Ensure the nozzle is at a safe Z height and the bed is clear.")
    print("- Be ready to hit M112 / power off.\n")

    ser = serial.Serial(
        port=args.port,
        baudrate=args.baud,
        timeout=0.2,
        write_timeout=2,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    try:
        # Many Marlin boards reset on connect.
        time.sleep(2.0)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        dt, lines, ok = send_and_wait_ok(ser, "M115", eol=eol, timeout_s=5.0)
        print(f"M115 ok={ok} dt={dt*1000:.1f}ms lines={len(lines)}")
        if lines:
            print(lines[0])

        if args.home_xy:
            dt, _lines, ok = send_and_wait_ok(ser, "G28 X Y", eol=eol, timeout_s=120.0)
            print(f"G28 X Y ok={ok} dt={dt:.2f}s")
            dt, _lines, ok = send_and_wait_ok(ser, "M400", eol=eol, timeout_s=300.0)
            print(f"M400 ok={ok} dt={dt:.2f}s")

        send_and_wait_ok(ser, "G91", eol=eol, timeout_s=3.0)

        for cmd in list(args.pre):
            dt, _lines, ok = send_and_wait_ok(ser, str(cmd), eol=eol, timeout_s=30.0)
            print(f"pre: {cmd} ok={ok} dt={dt*1000:.1f}ms")

        def move_cmd(sign: float) -> str:
            dx = args.dx * sign
            dy = args.dy * sign
            parts = []
            if abs(dx) > 1e-12:
                parts.append(f"X{dx:g}")
            if abs(dy) > 1e-12:
                parts.append(f"Y{dy:g}")
            if not parts:
                parts.append("X0")
            joined = " ".join(parts)
            return f"G0 {joined} F{args.feed}"

        # Warmup (helps fill planner/buffer and stabilize ok timing).
        for _ in range(max(0, int(args.warmup))):
            send_and_wait_ok(ser, move_cmd(+1.0), eol=eol, timeout_s=10.0)

        dts: list[float] = []
        t_all = _now()
        for i in range(max(0, int(args.count))):
            if args.pattern == "line":
                sign = +1.0
            else:
                sign = +1.0 if (i % 2 == 0) else -1.0
            dt, _lines, ok = send_and_wait_ok(ser, move_cmd(sign), eol=eol, timeout_s=10.0)
            if not ok:
                print(f"ERROR at i={i}: dt={dt:.3f}s")
                return 2
            dts.append(dt)
        total_s = _now() - t_all

        if not dts:
            print("No samples collected.")
            return 0

        for cmd in list(args.post):
            dt, _lines, ok = send_and_wait_ok(ser, str(cmd), eol=eol, timeout_s=30.0)
            print(f"post: {cmd} ok={ok} dt={dt*1000:.1f}ms")

        hz = len(dts) / total_s if total_s > 0 else 0.0
        print("\nRESULTS:")
        print(f"- moves: {len(dts)}  total: {total_s:.3f}s  effective: {hz:.1f} moves/s")
        print(
            f"- ok latency: mean={statistics.mean(dts)*1000:.2f}ms  "
            f"p50={percentile(dts,50)*1000:.2f}ms  "
            f"p95={percentile(dts,95)*1000:.2f}ms  "
            f"max={max(dts)*1000:.2f}ms"
        )
        if len(dts) >= 2:
            print(f"- ok latency stdev={statistics.pstdev(dts)*1000:.2f}ms")

        return 0
    finally:
        try:
            ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
