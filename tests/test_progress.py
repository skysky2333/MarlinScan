from __future__ import annotations

import math
import unittest

from v3se_printer.progress import StepProgress, StepProgressTracker, format_step_progress


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class StepProgressTests(unittest.TestCase):
    def test_formats_finite_and_adaptive_progress(self) -> None:
        self.assertEqual(
            format_step_progress(StepProgress("capture", "Capturing", 2, 5, "tiles", 61.2)),
            "Capturing: 2/5 tiles; ETA 1m 2s",
        )
        self.assertEqual(
            format_step_progress(StepProgress("focus", "Autofocus", 3, None, "samples", None)),
            "Autofocus: 3 samples; ETA unavailable",
        )

    def test_contract_validates_text_counts_and_eta(self) -> None:
        self.assertEqual(
            StepProgress("capture", "Capturing", 2, 4, "tiles", 6.0),
            StepProgress("capture", "Capturing", 2, 4, "tiles", 6.0),
        )
        invalid = (
            ("", "Capturing", 0, 1, "tiles", None),
            ("capture", "", 0, 1, "tiles", None),
            ("capture", "Capturing", 0, 1, "", None),
            ("capture", "Capturing", -1, 1, "tiles", None),
            ("capture", "Capturing", True, 1, "tiles", None),
            ("capture", "Capturing", 2, 1, "tiles", None),
            ("capture", "Capturing", 0, -1, "tiles", None),
            ("capture", "Capturing", 0, True, "tiles", None),
            ("capture", "Capturing", 1, None, "tiles", 1.0),
            ("capture", "Capturing", 0, 1, "tiles", 1.0),
            ("capture", "Capturing", 1, 2, "tiles", -1.0),
            ("capture", "Capturing", 1, 2, "tiles", math.inf),
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                StepProgress(*values)

    def test_eta_uses_only_completed_phase_work(self) -> None:
        clock = ManualClock()
        tracker = StepProgressTracker(clock)

        self.assertIsNone(tracker.update("capture", "Capturing", 0, 4, "tiles").eta_seconds)
        clock.advance(2.0)
        self.assertEqual(tracker.update("capture", "Capturing", 1, 4, "tiles").eta_seconds, 6.0)
        clock.advance(2.0)
        self.assertEqual(tracker.update("capture", "Capturing", 2, 4, "tiles").eta_seconds, 4.0)
        clock.advance(20.0)
        self.assertEqual(tracker.update("capture", "Capturing", 2, 4, "tiles").eta_seconds, 4.0)

    def test_unknown_total_never_produces_eta(self) -> None:
        clock = ManualClock()
        tracker = StepProgressTracker(clock)

        tracker.update("focus", "Autofocus", 0, None, "samples")
        clock.advance(3.0)
        self.assertIsNone(tracker.update("focus", "Autofocus", 1, None, "samples").eta_seconds)
        clock.advance(30.0)
        progress = tracker.update("focus", "Autofocus", 1, 4, "samples")
        self.assertEqual(progress.eta_seconds, 9.0)

    def test_new_phase_resets_timing_evidence(self) -> None:
        clock = ManualClock()
        tracker = StepProgressTracker(clock)

        tracker.update("capture", "Capturing", 0, 2, "tiles")
        clock.advance(5.0)
        self.assertEqual(tracker.update("capture", "Capturing", 1, 2, "tiles").eta_seconds, 5.0)
        clock.advance(20.0)
        self.assertIsNone(tracker.update("develop", "Developing", 0, 3, "tiles").eta_seconds)
        clock.advance(2.0)
        self.assertEqual(tracker.update("develop", "Developing", 1, 3, "tiles").eta_seconds, 4.0)

    def test_phase_updates_fail_on_invalid_history(self) -> None:
        tracker = StepProgressTracker(ManualClock())
        with self.assertRaisesRegex(ValueError, "start with zero"):
            tracker.update("capture", "Capturing", 1, 3, "tiles")

        tracker.update("capture", "Capturing", 0, 3, "tiles")
        tracker.update("capture", "Capturing", 1, 3, "tiles")
        for values in (
            ("capture", "Downloading", 1, 3, "tiles"),
            ("capture", "Capturing", 1, 3, "files"),
            ("capture", "Capturing", 0, 3, "tiles"),
            ("capture", "Capturing", 1, 4, "tiles"),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                tracker.update(*values)

    def test_reset_clears_phase_history(self) -> None:
        tracker = StepProgressTracker(ManualClock())
        tracker.update("capture", "Capturing", 0, 1, "tile")
        tracker.reset()
        self.assertIsNone(tracker.current)
        self.assertIsNone(tracker.update("capture", "Capturing", 0, 1, "tile").eta_seconds)

    def test_clock_must_be_finite_and_monotonic(self) -> None:
        values = iter((1.0, 0.5))
        tracker = StepProgressTracker(lambda: next(values))
        tracker.update("capture", "Capturing", 0, 2, "tiles")
        with self.assertRaisesRegex(ValueError, "backward"):
            tracker.update("capture", "Capturing", 1, 2, "tiles")

        with self.assertRaisesRegex(ValueError, "finite"):
            StepProgressTracker(lambda: math.nan).update("capture", "Capturing", 0, 2, "tiles")


if __name__ == "__main__":
    unittest.main()
