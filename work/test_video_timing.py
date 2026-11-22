import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs/GameVoice'))
from video_timing import DeadlineTicker, FrameRate


class VideoTimingTests(unittest.TestCase):
    def test_rendering_time_does_not_accumulate_in_timer(self):
        ticker = DeadlineTicker(60, 0)
        now = 0.0
        for _ in range(600):
            # Rendering uses 7 ms. A relative 17 ms sleep would lose 30% FPS.
            now += .007
            now += ticker.next_delay_ms(now) / 1000
        self.assertAlmostEqual(now, 10.0, delta=.002)

    def test_late_frame_skips_deadlines_without_catchup_spin(self):
        ticker = DeadlineTicker(60, 0)
        delay = ticker.next_delay_ms(.403)
        self.assertGreater(delay, 1)
        self.assertLessEqual(delay, 17)
        self.assertGreater(ticker.deadline, .403)
        self.assertLess(ticker.deadline, .403 + 1 / 60)

    def test_capture_and_preview_report_their_own_delivered_rates(self):
        capture, preview = FrameRate(), FrameRate()
        for i in range(91):
            capture.record(i / 30)
            if i % 3 == 0:
                preview.record(i / 30)
        self.assertAlmostEqual(capture.value(3), 30)
        self.assertAlmostEqual(preview.value(3), 10)
        self.assertEqual(capture.value(3.6), 0)
        self.assertEqual(preview.value(3.6), 0)

    def test_startup_and_long_stall_do_not_show_configured_fps(self):
        meter = FrameRate()
        self.assertEqual(meter.value(0), 0)
        meter.record(0)
        self.assertEqual(meter.value(0), 0)
        meter.record(1 / 30)
        self.assertAlmostEqual(meter.value(1 / 30), 30)
        self.assertEqual(meter.value(20), 0)
        self.assertFalse(meter.stamps)


if __name__ == '__main__':
    unittest.main()
