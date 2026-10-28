import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_mcp_hardware import service


class PopupTouchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = pathlib.Path(directory.name) / 'cmd'
        self.task = Mock()
        self.task.done.return_value = False
        self.loop = Mock()
        self.feedback = Mock()
        self.external_ui = Mock()
        self.external_ui.active.return_value = False
        for obj, name, value in [
            (service, 'CMD_PATH', str(self.path)), (service, 'feedback', self.feedback),
            (service, 'external_ui', self.external_ui),
            (service, 'render_popup', Mock()), (service, 'apply_volume', Mock()),
            (service.S, 'popup_visible', True), (service.S, 'popup_dismissed', False),
            (service.S, 'session_task', self.task), (service.S, 'activation_task', None),
            (service.S, 'last_popup_cmd', 0.), (service.S, 'muted', False),
            (service.S, 'loop', self.loop), (service.S, 'wake_event_q', Mock()),
        ]:
            p = patch.object(obj, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)

    def tap(self, x, y):
        self.path.write_text(f'TAP {x} {y}\n')
        service.poll_popup_cmds()

    def test_actual_close_taps_close_instead_of_restarting(self):
        for x, y in [(461,621), (459,631), (477,636), (464,632), (470,627), (457,623)]:
            self.tap(x, y)
        self.assertTrue(service.S.popup_dismissed)
        self.task.cancel.assert_called()
        self.feedback.clear.assert_called()
        self.loop.call_soon_threadsafe.assert_not_called()

    def test_actual_speaker_tap_toggles_mute_only(self):
        self.tap(477,576)
        self.assertTrue(service.S.muted)
        self.task.cancel.assert_not_called()
        self.loop.call_soon_threadsafe.assert_not_called()

    def test_body_tap_does_not_restart_conversation(self):
        self.tap(240,320)
        self.loop.call_soon_threadsafe.assert_not_called()
        self.task.cancel.assert_not_called()

    def test_invalid_raw_coordinates_are_ignored(self):
        for x, y in [(-1,20), (481,20), (20,641), (20,-1)]:
            self.tap(x,y)
        self.loop.call_soon_threadsafe.assert_not_called()
        self.task.cancel.assert_not_called()

    def test_endpoint_coordinates_stay_inside_display(self):
        self.assertEqual(service.overlay_touch_to_screen(461,621), (621,18))
        for raw in [(0,0), (480,0), (0,640), (480,640)]:
            x,y = service.overlay_touch_to_screen(*raw)
            self.assertTrue(0 <= x < 640 and 0 <= y < 480)

    def test_queued_body_tap_cannot_reopen_after_close(self):
        self.tap(240,320)
        self.tap(461,621)
        self.assertTrue(service.popup_wake_is_blocked('cmd'))
        self.assertTrue(service.popup_wake_is_blocked('touch'))
        self.assertFalse(service.popup_wake_is_blocked('wake'))
        service.S.popup_dismissed = False
        self.assertTrue(service.popup_wake_is_blocked('cmd'))

    def test_explicit_close_blocks_desktop_touch_after_cooldown(self):
        self.tap(461,621)
        service.S.popup_visible = False
        with patch.object(service, 'mono', return_value=service.S.last_popup_cmd + 60):
            self.assertTrue(service.popup_wake_is_blocked('touch'))
            self.assertFalse(service.popup_wake_is_blocked('wake'))
            service.S.popup_dismissed = False
            self.assertTrue(service.popup_wake_is_blocked('touch'))

    def test_designated_hold_can_wake_after_close(self):
        self.tap(461,621)
        service.S.popup_visible = False
        with patch.object(service.S, 'session_active', False), \
                patch.object(service.S, 'cfg', {'touch_wake': True}), \
                patch.object(service, 'mono', return_value=service.S.last_popup_cmd + 60):
            self.assertFalse(service.popup_wake_is_blocked('touch_hold'))
            service.S.cfg['touch_wake'] = False
            self.assertTrue(service.popup_wake_is_blocked('touch_hold'))

    def test_icon_hold_timer_can_start_immediately_after_close(self):
        self.tap(461,621)
        service.S.popup_visible = False
        with patch.object(service.S, 'session_active', False), \
                patch.object(service.S, 'cfg', {'touch_wake': True}), \
                patch.object(service, 'mono', return_value=service.S.last_popup_cmd + .1):
            self.assertFalse(service.popup_wake_is_blocked('touch_hold'))


if __name__ == '__main__':
    unittest.main()
