from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np  # type: ignore

from v3se_printer.calibration import (
    CalibrationError,
    ExposureReading,
    FocusMesh,
    FocusSample,
    NormalizedROI,
    RAW_HIGHLIGHT_TARGET,
    analyze_exposure,
    choose_shutter,
    exposure_error_ev,
    fit_focus_mesh,
    fine_focus_positions,
    focus_score,
    next_raw_shutter,
    next_shutter,
    parse_shutter_seconds,
    resolve_focus_peak,
    run_focus_sweep,
    shutter_choices,
)


class ROIAndExposureTests(unittest.TestCase):
    def test_roi_must_fit_inside_image(self) -> None:
        with self.assertRaises(ValueError):
            NormalizedROI(0.8, 0.2, 0.3, 0.4)

    def test_exposure_uses_only_selected_region(self) -> None:
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        image[25:75, 25:75] = 128
        reading = analyze_exposure(image, NormalizedROI(0.25, 0.25, 0.5, 0.5))
        self.assertEqual(reading.metered_luminance, 128.0)
        self.assertEqual(reading.percentile_99, 128.0)
        self.assertEqual(reading.clipped_fraction, 0.0)
        self.assertTrue(reading.accepted)

    def test_meter_ignores_small_black_and_clipped_tails(self) -> None:
        image = np.full((100, 100), 128, dtype=np.uint8)
        image[:1] = 0
        image[-1:] = 255

        reading = analyze_exposure(image, NormalizedROI(0.0, 0.0, 1.0, 1.0))

        self.assertEqual(reading.metered_luminance, 128.0)
        self.assertEqual(reading.clipped_fraction, 0.01)
        self.assertTrue(reading.accepted)

    def test_exposure_tolerance_uses_metered_brightness_not_clipping(self) -> None:
        self.assertTrue(ExposureReading(128.0, 255.0, 0.5).accepted)
        self.assertFalse(ExposureReading(100.0, 128.0, 0.0).accepted)
        self.assertFalse(ExposureReading(160.0, 128.0, 0.0).accepted)

    def test_exposure_error_is_zero_only_at_target_meter_value(self) -> None:
        self.assertEqual(exposure_error_ev(ExposureReading(128.0, 240.0, 0.0)), 0.0)
        self.assertLess(exposure_error_ev(ExposureReading(127.0, 240.0, 0.0)), 0.0)
        self.assertGreater(exposure_error_ev(ExposureReading(129.0, 240.0, 0.0)), 0.0)

    def test_shutter_labels_and_nearest_choice(self) -> None:
        self.assertEqual(parse_shutter_seconds("1/125"), 1 / 125)
        self.assertEqual(parse_shutter_seconds("10/13"), 10 / 13)
        self.assertAlmostEqual(parse_shutter_seconds("Unknown value 0009"), 2.0 ** (-9.0 / 6.0))
        self.assertAlmostEqual(parse_shutter_seconds("Unknown value ffffffe3"), 2.0 ** (29.0 / 6.0))
        self.assertIsNone(parse_shutter_seconds("Bulb"))
        choices = shutter_choices(["1/100", "1/50", "1", "2"])
        self.assertEqual(choose_shutter(choices, 0.018)[0], "1/50")

    def test_unknown_nikon_step_is_ordered_by_duration_and_honors_one_second_limit(self) -> None:
        choices = shutter_choices(
            ["10/25", "Unknown value 0009", "1/3", "Unknown value ffffffe3"]
        )
        self.assertEqual([label for label, _seconds in choices], ["1/3", "Unknown value 0009", "10/25"])

    def test_jpeg_clipping_does_not_override_a_good_meter_reading(self) -> None:
        choices = shutter_choices(["1/100", "1/50", "1/25"])
        selected = next_shutter(ExposureReading(128.0, 255.0, 0.5), 1 / 50, choices)
        self.assertIsNone(selected)

    def test_low_meter_reading_selects_a_slower_shutter_despite_clipping(self) -> None:
        choices = shutter_choices(["1/100", "1/50", "1/25"])
        selected = next_shutter(ExposureReading(80.0, 255.0, 0.02), 1 / 50, choices)
        self.assertEqual(selected, ("1/25", 1 / 25))

    def test_fastest_shutter_returns_best_available_bright_exposure(self) -> None:
        choices = shutter_choices(["1/100", "1/50", "1/25"])
        self.assertIsNone(next_shutter(ExposureReading(200.0, 255.0, 0.5), 1 / 100, choices))

    def test_one_second_returns_best_available_dark_exposure(self) -> None:
        choices = shutter_choices(["1/2", "1"])
        self.assertIsNone(next_shutter(ExposureReading(20.0, 20.0, 0.0), 1.0, choices))

    def test_small_metering_error_advances_to_adjacent_shutter(self) -> None:
        choices = shutter_choices(["1/100", "1/50", "1/25"])
        self.assertEqual(next_shutter(ExposureReading(100.0, 100.0, 0.0), 1 / 50, choices), ("1/25", 1 / 25))
        self.assertEqual(next_shutter(ExposureReading(160.0, 160.0, 0.0), 1 / 50, choices), ("1/100", 1 / 100))
        self.assertIsNone(next_shutter(ExposureReading(128.0, 255.0, 0.5), 1 / 50, choices))

    def test_raw_highlight_level_selects_a_slower_nearest_shutter(self) -> None:
        choices = shutter_choices(["1/13", "10/25", "1/2", "1"])

        selected = next_raw_shutter(0.13675214, 1 / 13, choices)

        self.assertEqual(selected, ("1/2", 0.5))

    def test_raw_highlight_target_does_not_shorten_or_require_exact_match(self) -> None:
        choices = shutter_choices(["1/100", "1/50", "1/25", "1"])

        self.assertIsNone(next_raw_shutter(RAW_HIGHLIGHT_TARGET, 1 / 25, choices))
        self.assertIsNone(next_raw_shutter(0.84, 1 / 25, choices))

    def test_zero_raw_signal_selects_the_maximum_shutter(self) -> None:
        choices = shutter_choices(["1/100", "1/25", "1"])

        self.assertEqual(next_raw_shutter(0.0, 1 / 100, choices), ("1", 1.0))


class FocusTests(unittest.TestCase):
    def test_focus_region_requires_texture(self) -> None:
        image = np.full((100, 100, 3), 127, dtype=np.uint8)
        with self.assertRaises(CalibrationError):
            focus_score(image, NormalizedROI())

    def test_coarse_peak_must_be_interior(self) -> None:
        samples = [FocusSample(float(index), float(index)) for index in range(5)]
        with self.assertRaises(CalibrationError):
            fine_focus_positions(samples)

    def test_fine_sweep_refines_the_measured_coarse_bracket(self) -> None:
        coarse = [
            FocusSample(201.0, 106.3),
            FocusSample(202.0, 170.0),
            FocusSample(203.0, 354.9),
            FocusSample(204.0, 462.5),
            FocusSample(205.0, 196.6),
        ]
        positions = fine_focus_positions(coarse)
        self.assertEqual(positions, [203.0 + index * 0.25 for index in range(9)])

    def test_quadratic_fine_peak(self) -> None:
        peak = resolve_focus_peak(
            [FocusSample(0.9, 80.0), FocusSample(1.0, 100.0), FocusSample(1.1, 80.0)]
        )
        self.assertAlmostEqual(peak, 1.0)

    def test_fine_peak_requires_relative_prominence(self) -> None:
        with self.assertRaisesRegex(CalibrationError, "not prominent across the sweep"):
            resolve_focus_peak(
                [FocusSample(0.9, 99.0), FocusSample(1.0, 100.0), FocusSample(1.1, 99.0)]
            )

    def test_fine_peak_prominence_is_scale_invariant(self) -> None:
        for scale in (1.0, 1000.0):
            peak = resolve_focus_peak(
                [FocusSample(0.9, 80.0 * scale), FocusSample(1.0, 100.0 * scale), FocusSample(1.1, 80.0 * scale)]
            )
            self.assertAlmostEqual(peak, 1.0)

    def test_real_broad_peak_is_validated_against_sweep_endpoints(self) -> None:
        scores = [176.6823, 210.4027, 268.9718, 355.7057, 425.0124, 429.5639, 370.2674, 295.6154, 235.1370]
        samples = [
            FocusSample(202.58 + 0.25 * index, score)
            for index, score in enumerate(scores)
        ]
        self.assertAlmostEqual(resolve_focus_peak(samples), 203.7228, places=4)

    def test_two_sample_fine_plateau_selects_midpoint(self) -> None:
        samples = [
            FocusSample(0.0, 70.0),
            FocusSample(0.25, 100.0),
            FocusSample(0.5, 100.0),
            FocusSample(0.75, 70.0),
        ]
        self.assertEqual(resolve_focus_peak(samples), 0.375)

    def test_adaptive_focus_sweep_brackets_and_refines_measured_curve(self) -> None:
        moved: list[float] = []
        captured: list[float] = []
        fine_positions = [203.0 + index * 0.25 for index in range(9)]
        scores = [170.0, 354.9, 462.5, 196.6]
        scores.extend(700.0 - 400.0 * (z - 203.75) ** 2 for z in fine_positions)
        with (
            patch("v3se_printer.calibration.read_jpeg", return_value=np.empty((1, 1), dtype=np.uint8)),
            patch("v3se_printer.calibration.focus_score", side_effect=scores),
        ):
            peak, samples = run_focus_sweep(
                start_z=203.0,
                z_min=190.0,
                z_max=220.0,
                roi=NormalizedROI(),
                move_z=moved.append,
                capture=lambda _index, z: captured.append(z) or "unused.jpg",
            )
        self.assertEqual(captured[:4], [202.0, 203.0, 204.0, 205.0])
        self.assertEqual(captured[4:], fine_positions)
        self.assertEqual(len(samples), 13)
        self.assertAlmostEqual(peak, 203.75)
        self.assertLess(moved[-2], moved[-1])
        self.assertAlmostEqual(moved[-1], peak)

    def test_focus_sweep_requires_room_for_coarse_samples(self) -> None:
        with self.assertRaisesRegex(CalibrationError, "too small"):
            run_focus_sweep(
                start_z=1.0,
                z_min=0.0,
                z_max=2.0,
                roi=NormalizedROI(),
                move_z=lambda _z: None,
                capture=lambda _index, _z: "unused.jpg",
            )

    def test_focus_sweep_reports_samples_before_overly_broad_peak_raises(self) -> None:
        scores = [170.0, 354.9, 462.5, 196.6]
        scores.extend([100.0, 200.0, 300.0, 400.0, 500.0, 500.0, 500.0, 400.0, 300.0])
        reported: list[tuple[str, int, FocusSample]] = []
        events: list[tuple[str, str, bool | None]] = []
        with (
            patch("v3se_printer.calibration.read_jpeg", return_value=np.empty((1, 1), dtype=np.uint8)),
            patch("v3se_printer.calibration.focus_score", side_effect=scores),
            self.assertRaisesRegex(CalibrationError, "too broad"),
        ):
            run_focus_sweep(
                start_z=203.0,
                z_min=190.0,
                z_max=220.0,
                roi=NormalizedROI(),
                move_z=lambda _z: None,
                capture=lambda _index, _z: "unused.jpg",
                on_sample=lambda phase, index, sample: reported.append((phase, index, sample)),
                on_event=lambda phase, message, accepted: events.append((phase, message, accepted)),
            )
        self.assertEqual([phase for phase, _index, _sample in reported], ["coarse"] * 4 + ["fine"] * 9)
        self.assertEqual([index for _phase, index, _sample in reported], list(range(13)))
        self.assertEqual([sample.score for _phase, _index, sample in reported], scores)
        self.assertEqual(events[-1][2], False)
        self.assertIn("too broad", events[-1][1])

    def test_ambiguous_coarse_curve_expands_both_directions(self) -> None:
        moved: list[float] = []
        captured: list[float] = []
        events: list[tuple[str, str]] = []
        scores = [100.0, 100.0, 100.0, 90.0, 150.0, 100.0]
        scores.extend([80.0, 100.0, 140.0, 180.0, 220.0, 180.0, 140.0, 100.0, 80.0])
        with (
            patch("v3se_printer.calibration.read_jpeg", return_value=np.empty((1, 1), dtype=np.uint8)),
            patch("v3se_printer.calibration.focus_score", side_effect=scores),
        ):
            peak, _samples = run_focus_sweep(
                start_z=3.0,
                z_min=0.0,
                z_max=7.0,
                roi=NormalizedROI(),
                move_z=moved.append,
                capture=lambda _index, z: captured.append(z) or "unused.jpg",
                on_event=lambda phase, message, _accepted: events.append((phase, message)),
            )
        self.assertEqual(captured[:6], [2.0, 3.0, 4.0, 1.0, 5.0, 6.0])
        self.assertAlmostEqual(peak, 5.0)
        self.assertTrue(any("expanding both directions" in message for _phase, message in events))

    def test_fine_curve_expands_past_edge_before_resolving_peak(self) -> None:
        captured: list[float] = []
        scores = [100.0, 200.0, 300.0, 200.0]
        scores.extend([100.0, 120.0, 140.0, 160.0, 180.0, 200.0, 220.0, 240.0, 260.0, 200.0])
        with (
            patch("v3se_printer.calibration.read_jpeg", return_value=np.empty((1, 1), dtype=np.uint8)),
            patch("v3se_printer.calibration.focus_score", side_effect=scores),
        ):
            peak, _samples = run_focus_sweep(
                start_z=203.0,
                z_min=190.0,
                z_max=220.0,
                roi=NormalizedROI(),
                move_z=lambda _z: None,
                capture=lambda _index, z: captured.append(z) or "unused.jpg",
            )
        self.assertEqual(captured[-1], 205.25)
        self.assertAlmostEqual(peak, 204.9375)

    def test_monotonic_focus_sweep_fails_at_upper_hard_limit(self) -> None:
        with (
            patch("v3se_printer.calibration.read_jpeg", return_value=np.empty((1, 1), dtype=np.uint8)),
            patch("v3se_printer.calibration.focus_score", side_effect=[1.0, 2.0, 3.0, 4.0]),
            self.assertRaisesRegex(CalibrationError, "upper hard Z limit"),
        ):
            run_focus_sweep(
                start_z=2.0,
                z_min=0.0,
                z_max=4.0,
                roi=NormalizedROI(),
                move_z=lambda _z: None,
                capture=lambda _index, _z: "unused.jpg",
            )

    def test_flat_focus_sweep_fails_at_lower_hard_limit(self) -> None:
        events: list[tuple[str, str, bool | None]] = []
        with (
            patch("v3se_printer.calibration.read_jpeg", return_value=np.empty((1, 1), dtype=np.uint8)),
            patch("v3se_printer.calibration.focus_score", side_effect=[1.0, 1.0, 1.0, 1.0]),
            self.assertRaisesRegex(CalibrationError, "lower and upper hard Z limit"),
        ):
            run_focus_sweep(
                start_z=2.0,
                z_min=0.0,
                z_max=4.0,
                roi=NormalizedROI(),
                move_z=lambda _z: None,
                capture=lambda _index, _z: "unused.jpg",
                on_event=lambda phase, message, accepted: events.append((phase, message, accepted)),
            )
        self.assertIn("expanding upper Z", events[1][1])
        self.assertNotIn("both directions", events[1][1])
        self.assertEqual(events[-1][2], False)

    def test_focus_mesh_interpolates_quadrant_samples_and_center(self) -> None:
        mesh = FocusMesh(0.0, 10.0, 0.0, 10.0, 1.0, 2.0, 3.0, 4.0)

        self.assertEqual(mesh.z_at(2.5, 2.5), 1.0)
        self.assertEqual(mesh.z_at(7.5, 2.5), 2.0)
        self.assertEqual(mesh.z_at(2.5, 7.5), 3.0)
        self.assertEqual(mesh.z_at(7.5, 7.5), 4.0)
        self.assertEqual(mesh.z_at(5.0, 5.0), 2.5)

    def test_focus_mesh_fit_uses_quadrants_and_center(self) -> None:
        mesh = fit_focus_mesh(
            0.0,
            100.0,
            0.0,
            100.0,
            [
                (25.0, 25.0, 10.0),
                (75.0, 25.0, 12.0),
                (25.0, 75.0, 14.0),
                (75.0, 75.0, 16.0),
                (50.0, 50.0, 15.0),
            ],
        )

        self.assertAlmostEqual(mesh.z00, 10.4)
        self.assertAlmostEqual(mesh.z10, 12.4)
        self.assertAlmostEqual(mesh.z01, 14.4)
        self.assertAlmostEqual(mesh.z11, 16.4)
        self.assertAlmostEqual(mesh.z_at(50.0, 50.0), 13.4)
        with self.assertRaisesRegex(ValueError, "exactly five"):
            fit_focus_mesh(0.0, 100.0, 0.0, 100.0, [])
        with self.assertRaisesRegex(ValueError, "span a bilinear surface"):
            fit_focus_mesh(
                0.0,
                100.0,
                0.0,
                100.0,
                [(50.0, 50.0, float(index)) for index in range(5)],
            )

    def test_focus_mesh_extrapolates_quadrant_samples_to_coverage_edges(self) -> None:
        mesh = FocusMesh(0.0, 100.0, 0.0, 100.0, 107.5, 112.5, 117.5, 122.5)

        self.assertAlmostEqual(mesh.z_at(0.0, 0.0), 100.0)
        self.assertAlmostEqual(mesh.z_at(100.0, 0.0), 110.0)
        self.assertAlmostEqual(mesh.z_at(0.0, 100.0), 120.0)
        self.assertAlmostEqual(mesh.z_at(100.0, 100.0), 130.0)
        with self.assertRaisesRegex(ValueError, "outside"):
            mesh.z_at(100.01, 50.0)

    def test_focus_mesh_maximum_absolute_z_difference_uses_coverage_corners(self) -> None:
        mesh = FocusMesh(0.0, 100.0, 0.0, 100.0, 107.5, 112.5, 117.5, 122.5)
        coverage_zs = [
            mesh.z_at(mesh.x_min, mesh.y_min),
            mesh.z_at(mesh.x_max, mesh.y_min),
            mesh.z_at(mesh.x_min, mesh.y_max),
            mesh.z_at(mesh.x_max, mesh.y_max),
        ]
        maximum_absolute_difference = max(
            abs(left - right)
            for left in coverage_zs
            for right in coverage_zs
        )

        self.assertAlmostEqual(maximum_absolute_difference, 30.0)
        self.assertAlmostEqual(maximum_absolute_difference, max(coverage_zs) - min(coverage_zs))
        self.assertGreater(
            maximum_absolute_difference,
            max(mesh.z00, mesh.z10, mesh.z01, mesh.z11)
            - min(mesh.z00, mesh.z10, mesh.z01, mesh.z11),
        )


if __name__ == "__main__":
    unittest.main()
