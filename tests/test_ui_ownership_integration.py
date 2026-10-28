import unittest
from unittest.mock import Mock,patch
from test_mcp_hardware import service


class ExternalUiTests(unittest.TestCase):
    def test_owner_suppresses_popup_without_changing_configuration(self):
        cfg={'enabled':True,'popup_enabled':True,'touch_wake':True}
        owner=Mock();owner.active.return_value=True
        with patch.object(service,'external_ui',owner),patch.object(service.S,'cfg',cfg), \
                patch.object(service.S,'popup_dismissed',False),patch.object(service.S,'session_state','listening'), \
                patch.object(service.S,'popup_visible',False),patch.object(service.ui_render,'render') as render:
            service.render_popup(visible=True)
            self.assertFalse(service.S.popup_visible)
            self.assertEqual(render.call_args.kwargs['state'],'idle')
            self.assertTrue(cfg['popup_enabled'])
            owner.active.return_value=False
            service.render_popup(visible=True)
            self.assertTrue(service.S.popup_visible)
            self.assertEqual(render.call_args.kwargs['state'],'listening')

    def test_owner_blocks_only_touch_wake_not_voice(self):
        owner=Mock();owner.active.return_value=True
        with patch.object(service,'external_ui',owner), \
                patch.object(service.S,'cfg',{'touch_wake':True}), \
                patch.object(service.S,'popup_visible',False),patch.object(service.S,'session_active',False):
            self.assertTrue(service.popup_wake_is_blocked('touch_hold'))
            self.assertFalse(service.popup_wake_is_blocked('wake'))
            owner.active.return_value=False
            self.assertFalse(service.popup_wake_is_blocked('touch_hold'))


if __name__=='__main__':unittest.main()
