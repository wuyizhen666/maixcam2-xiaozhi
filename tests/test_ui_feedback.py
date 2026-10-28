import importlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'xiaozhi' / 'service'))
import ui_render
try:
    ui_feedback = importlib.import_module('ui_feedback')
except ModuleNotFoundError:
    ui_feedback = None


class Timer:
    def __init__(self, when, callback, args):
        self.when, self.callback, self.args = when, callback, args
        self.cancelled = False
    def cancel(self): self.cancelled = True


class Clock:
    def __init__(self): self.now = 0; self.timers = []
    def call_later(self, delay, callback, *args):
        timer = Timer(self.now + delay, callback, args)
        self.timers.append(timer)
        return timer
    def advance(self, seconds):
        end = self.now + seconds
        while True:
            due = [t for t in self.timers if not t.cancelled and t.when <= end]
            if not due: break
            timer = min(due, key=lambda t: t.when)
            self.timers.remove(timer); self.now = timer.when
            timer.callback(*timer.args)
        self.now = end


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ui_feedback, 'timed UI feedback is not implemented')
        self.clock = Clock(); self.changes = []
        self.feedback = ui_feedback.UiFeedback(lambda: self.changes.append(self.clock.now), loop=self.clock)
        self.owner = object()

    def test_photo_expires_at_three_seconds_without_other_events(self):
        self.feedback.show_photo(self.owner, b'photo')
        self.clock.advance(2.999)
        self.assertEqual(self.feedback.photo_jpeg, b'photo')
        self.clock.advance(.001)
        self.assertIsNone(self.feedback.photo_jpeg)
        self.assertEqual(self.changes, [0, 3])

    def test_replacement_photo_gets_its_own_full_three_seconds(self):
        self.feedback.show_photo(self.owner, b'first'); self.clock.advance(2)
        self.feedback.show_photo(self.owner, b'second'); self.clock.advance(1)
        self.assertEqual(self.feedback.photo_jpeg, b'second')
        self.clock.advance(2); self.assertIsNone(self.feedback.photo_jpeg)

    def test_tool_stays_visible_while_running_and_lingers_after_finish(self):
        token = self.feedback.begin_tool(self.owner, 'self.camera.take_photo')
        self.clock.advance(10)
        self.assertEqual(self.feedback.tool_name, 'self.camera.take_photo')
        self.feedback.finish_tool(token); self.clock.advance(2.99)
        self.assertEqual(self.feedback.tool_name, 'self.camera.take_photo')
        self.clock.advance(.01); self.assertEqual(self.feedback.tool_name, '')

    def test_finishing_older_tool_does_not_clear_newer_tool(self):
        first = self.feedback.begin_tool(self.owner, 'first')
        second = self.feedback.begin_tool(self.owner, 'second')
        self.feedback.finish_tool(first); self.clock.advance(3)
        self.assertEqual(self.feedback.tool_name, 'second')
        self.feedback.finish_tool(second); self.clock.advance(3)
        self.assertEqual(self.feedback.tool_name, '')

    def test_clear_owner_preserves_other_session_and_cancels_timers(self):
        old = self.feedback.begin_tool(self.owner, 'old')
        self.feedback.show_photo(self.owner, b'old-photo')
        other = object(); self.feedback.begin_tool(other, 'new')
        self.feedback.clear_owner(self.owner); self.feedback.finish_tool(old)
        self.clock.advance(10)
        self.assertIsNone(self.feedback.photo_jpeg)
        self.assertEqual(self.feedback.tool_name, 'new')

    def test_clear_removes_all_feedback_without_late_reappearance(self):
        token = self.feedback.begin_tool(self.owner, 'tool')
        self.feedback.finish_tool(token); self.feedback.show_photo(self.owner, b'photo')
        self.feedback.clear(); self.clock.advance(10)
        self.assertEqual(self.feedback.tool_name, '')
        self.assertIsNone(self.feedback.photo_jpeg)

    def test_imu_panel_collapses_at_three_seconds_but_keeps_compact_data(self):
        data={'acceleration_m_s2':{'x':1,'y':2,'z':3}}
        self.feedback.show_imu(self.owner,data)
        data['acceleration_m_s2']['x']=99
        self.assertEqual(self.feedback.imu_sample['acceleration_m_s2']['x'],1)
        self.clock.advance(2.999)
        self.assertTrue(self.feedback.imu_expanded)
        self.assertIsNotNone(self.feedback.imu_sample)
        self.clock.advance(.001)
        self.assertFalse(self.feedback.imu_expanded)
        self.assertIsNotNone(self.feedback.imu_sample)

    def test_new_imu_sample_resets_timer_and_close_clears_it(self):
        self.feedback.show_imu(self.owner,{'sample':1});self.clock.advance(2)
        self.feedback.show_imu(self.owner,{'sample':2});self.clock.advance(1)
        self.assertTrue(self.feedback.imu_expanded)
        self.assertEqual(self.feedback.imu_sample,{'sample':2})
        self.feedback.clear_owner(self.owner);self.clock.advance(8)
        self.assertIsNone(self.feedback.imu_sample)

    def test_live_updates_do_not_restart_large_panel_timer(self):
        self.feedback.show_imu(self.owner,{'sample':1});self.clock.advance(2)
        self.assertTrue(self.feedback.update_imu(self.owner,{'sample':2}))
        self.clock.advance(1)
        self.assertFalse(self.feedback.imu_expanded)
        self.assertEqual(self.feedback.imu_sample,{'sample':2})
        self.feedback.clear_owner(self.owner)
        self.assertFalse(self.feedback.update_imu(self.owner,{'sample':3}))
        self.assertIsNone(self.feedback.imu_sample)

    def test_clear_removes_imu_after_tool_label_has_expired(self):
        token=self.feedback.begin_tool(self.owner,'self.imu.read')
        self.feedback.finish_tool(token);self.feedback.show_imu(self.owner,{'sample':1})
        self.clock.advance(4);self.assertEqual(self.feedback.tool_name,'')
        self.feedback.clear();self.clock.advance(10)
        self.assertIsNone(self.feedback.imu_sample)


class RenderTests(unittest.TestCase):
    def test_photo_replaces_bubbles_and_footer_has_opaque_background(self):
        data = io.BytesIO(); Image.new('RGB', (640, 480), (255, 0, 0)).save(data, 'JPEG')
        with tempfile.TemporaryDirectory() as directory:
            image = ui_render.render(str(Path(directory) / 'popup.rgba'), state='test', clock=False,
                                     bubbles=[('ai', 'chat must not cover photo')],
                                     photo_jpeg=data.getvalue(), mcp_tool='self.camera.take_photo')
            self.assertGreater(image.getpixel((320, 240))[0], 240)
            self.assertLess(image.getpixel((320, 240))[1], 10)
            self.assertEqual(image.getpixel((5, 240)), (0, 0, 0, 255))
            self.assertGreater(image.getpixel((5, 470))[3], 240)
            without = ui_render.render(str(Path(directory) / 'after.rgba'), state='test', clock=False,
                                       bubbles=[], footer=False)
            self.assertEqual(without.getpixel((320, 240))[3], 0)

    def test_tool_name_can_be_drawn_without_chat_bubbles(self):
        with tempfile.TemporaryDirectory() as directory:
            with_name = ui_render.render(str(Path(directory) / 'name.rgba'), clock=False,
                                        mcp_tool='self.audio_speaker.get_volume_status')
            hidden = ui_render.render(str(Path(directory) / 'hidden.rgba'), clock=False)
            self.assertIsNotNone(with_name.crop((0, 438, 640, 480)).getbbox())
            self.assertIsNone(hidden.getbbox())


if __name__ == '__main__': unittest.main()
