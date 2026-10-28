import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'apps/siot_monitor/model.py'
spec = importlib.util.spec_from_file_location('siot_gesture_model', SOURCE)
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)


class TouchGesturesTests(unittest.TestCase):
    def setUp(self):
        self.gesture = model.TouchGestures()

    def update(self, x, y, down, enabled=True):
        return self.gesture.update(x, y, down, message_scroll=enabled)

    def test_button_tap_fires_only_once_on_release(self):
        self.assertIsNone(self.update(50, 450, True))
        for _ in range(4):self.assertIsNone(self.update(50, 450, True))
        self.assertEqual(self.update(0, 0, False), ('tap', 50, 450))
        self.assertIsNone(self.update(0, 0, False))

    def test_jitter_keeps_original_tap_target(self):
        self.update(300, 200, True)
        self.update(307, 207, True)
        self.assertEqual(self.update(0, 0, False), ('tap', 300, 200))

    def test_message_swipe_directions_do_not_open_message(self):
        for start, end, direction in [(250, 200, 1), (200, 250, -1)]:
            self.assertIsNone(self.update(300, start, True))
            self.assertIsNone(self.update(301, end, True))
            self.assertEqual(self.update(0, 0, False), ('swipe', direction))

    def test_short_or_horizontal_drag_is_not_tap_or_swipe(self):
        for dx, dy in [(0, 20), (70, 50), (32, 32)]:
            self.update(300, 200, True)
            self.update(300+dx, 200+dy, True)
            self.assertIsNone(self.update(0, 0, False))

    def test_returning_to_start_after_drag_is_not_a_tap(self):
        self.update(300, 200, True)
        self.update(300, 250, True)
        self.update(300, 200, True)
        self.assertIsNone(self.update(0, 0, False))

    def test_drag_outside_lists_does_not_page(self):
        for x, y in [(200, 200), (300, 90), (300, 400)]:
            self.update(x, y, True)
            self.update(x, y-50, True)
            self.assertIsNone(self.update(0, 0, False))

    def test_scrollbar_is_clamped_and_never_taps(self):
        self.assertEqual(self.update(620, 100, True), ('scroll', 0.))
        self.assertIsNone(self.update(620, 100, True))
        self.assertEqual(self.update(620, 241, True), ('scroll', .5))
        self.assertEqual(self.update(620, 450, True), ('scroll', 1.))
        self.assertEqual(self.update(0, 0, False), ('scroll', 1.))

    def test_thresholds_and_scrollbar_edges(self):
        self.update(300, 200, True)
        self.update(312, 200, True)
        self.assertEqual(self.update(0, 0, False), ('tap', 300, 200))
        self.update(300, 200, True)
        self.update(300, 168, True)
        self.assertEqual(self.update(0, 0, False), ('swipe', 1))
        self.assertEqual(self.update(184, 366, True), ('topic_scroll', 1.))
        self.update(0, 0, False)
        self.assertEqual(self.update(616, 382, True), ('scroll', 1.))
        self.update(0, 0, False)
        self.assertIsNone(self.update(615, 382, True))
        self.assertEqual(self.update(0, 0, False), ('tap', 615, 382))

    def test_mode_change_and_cancel_suppress_held_touch_until_release(self):
        self.update(300, 200, True)
        self.assertIsNone(self.update(300, 140, True, enabled=False))
        self.assertIsNone(self.update(0, 0, False, enabled=False))
        self.update(50, 450, True, enabled=False)
        self.gesture.cancel()
        self.assertIsNone(self.update(50, 450, True, enabled=False))
        self.assertIsNone(self.update(0, 0, False, enabled=False))
        self.update(50, 450, True, enabled=False)
        self.assertEqual(self.update(0, 0, False, enabled=False), ('tap', 50, 450))

    def test_disabled_scroll_mode_keeps_keyboard_taps(self):
        self.assertIsNone(self.update(620, 270, True, enabled=False))
        self.assertEqual(self.update(0, 0, False, enabled=False), ('tap', 620, 270))

    def test_out_of_screen_cancels_contact(self):
        self.update(10, 200, True)
        self.update(-1, 200, True)
        self.update(10, 200, True)
        self.assertIsNone(self.update(0, 0, False))

    def test_topic_swipe_and_scroll_have_distinct_events(self):
        self.update(90, 250, True)
        self.update(90, 150, True)
        self.assertEqual(self.update(0, 0, False), ('topic_swipe', 1))
        self.assertEqual(self.update(190, 233, True), ('topic_scroll', .5))
        self.assertEqual(self.update(0, 0, False), ('topic_scroll', .5))

    def test_regions_are_latched_even_when_drag_crosses_columns(self):
        self.update(180, 300, True)
        self.update(220, 150, True)
        self.assertEqual(self.update(0, 0, False), ('topic_swipe', 1))
        self.update(210, 300, True)
        self.update(180, 150, True)
        self.assertEqual(self.update(0, 0, False), ('swipe', 1))
        self.update(190, 233, True)
        self.assertEqual(self.update(620, 366, True), ('topic_scroll', 1.))
        self.assertEqual(self.update(0, 0, False), ('topic_scroll', 1.))


if __name__ == '__main__':
    unittest.main()
