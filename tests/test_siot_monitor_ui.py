"""Screen interaction contracts, without opening Maix hardware."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1] / 'apps/siot_monitor'
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('siot_screen', ROOT / 'main.py')
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.state = ui.MonitorModel()
        self.state.seed_topics(['alice/first', 'alice/second'])
        self.screen = ui.Screen(self.state)
        self.screen.account_prefix = 'alice/'
        self.history = Mock()
        self.history.enqueue_topic_change.return_value = 7

    def click(self, action):
        self.screen.draw()
        rect = next(rect for rect, key in self.screen.buttons if key == action)
        x, y, w, h = rect
        return self.screen.handle(x+w//2, y+h//2, None, self.history)

    def test_new_topic_requires_explicit_create_and_never_publishes(self):
        self.click('new_topic')
        self.assertEqual(self.screen.topic.value, 'alice/')
        self.assertEqual(self.screen.focus, 'topic')
        self.history.enqueue_topic_change.assert_not_called()
        self.screen.topic.insert('new')
        self.click('create_confirm')
        self.history.enqueue_topic_change.assert_called_once_with('create', 'alice/new')
        self.click('create_confirm')
        self.history.enqueue_topic_change.assert_called_once()
        self.screen.topic_result(('topic_created', 7, 'alice/new'), self.history)
        self.assertEqual(self.screen.page, 'monitor')
        self.assertEqual(self.state.selected_topic, 'alice/new')

    def test_cancel_delete_does_not_call_server(self):
        self.click('delete_topic')
        self.assertEqual(self.screen.delete_target, 'alice/first')
        self.click('monitor')
        self.history.enqueue_topic_change.assert_not_called()
        self.assertIn('alice/first', self.state.topics)

    def test_delete_confirms_captured_name_and_removes_only_after_success(self):
        self.click('delete_topic')
        self.state.selected_topic = 'alice/second'
        self.click('delete_confirm')
        self.history.enqueue_topic_change.assert_called_once_with('delete', 'alice/first')
        self.assertIn('alice/first', self.state.topics)
        self.screen.topic_result(('topic_deleted', 99, 'alice/first'), self.history)
        self.assertIn('alice/first', self.state.topics)
        self.screen.topic_result(('topic_deleted', 7, 'alice/first'), self.history)
        self.assertNotIn('alice/first', self.state.topics)
        self.assertIn('alice/second', self.state.topics)

    def test_delete_error_keeps_topic_and_allows_retry(self):
        self.click('delete_topic')
        self.click('delete_confirm')
        self.screen.topic_result(('topic_error', 7, 'failed'), self.history)
        self.assertIn('alice/first', self.state.topics)
        self.assertIsNone(self.screen.pending_topic_change)
        self.assertEqual(self.screen.page, 'delete')

    def test_clear_requires_confirmation_and_cancel_preserves_data(self):
        self.state.receive('alice/first','old',1)
        self.click('clear_topic')
        self.assertEqual(self.screen.page,'clear_data')
        self.history.enqueue_topic_change.assert_not_called()
        self.click('monitor')
        self.assertEqual(len(self.state.history()),1)

    def test_clear_retains_topic_and_messages_received_after_confirmation(self):
        self.state.receive('alice/first','old',1)
        self.screen.last_media_payload=('alice/first',b'old image',1.)
        self.click('clear_topic')
        self.click('clear_confirm')
        self.history.enqueue_topic_change.assert_called_once_with('clear','alice/first')
        self.state.receive('alice/first','new',2)
        self.state.add_history('alice/first',[{'id':10,'content':'new','timestamp':2}])
        self.screen.topic_result(('topic_cleared',7,'alice/first'),self.history)
        self.assertIn('alice/first',self.state.topics)
        self.assertEqual([m['text'] for m in self.state.history()],['new'])
        self.assertEqual(self.screen.page,'monitor')
        self.assertIsNone(self.screen.last_media_payload)

    def test_failed_clear_does_not_discard_cache(self):
        self.state.receive('alice/first','old',1)
        self.click('clear_topic');self.click('clear_confirm')
        self.screen.topic_result(('topic_error',7,'failed'),self.history)
        self.assertEqual(len(self.state.history()),1)

    def test_unconfirmed_send_blocks_topic_management(self):
        self.screen.pending_mid = 3
        self.click('delete_topic')
        self.assertEqual(self.screen.page, 'monitor')
        self.history.enqueue_topic_change.assert_not_called()

    def test_backspace_deletes_digit_english_and_chinese_in_focused_field(self):
        self.screen.page = 'edit'
        self.screen.payload = ui.TextEditor('中a1')
        for value in ['中a', '中', '']:
            self.click('backspace')
            self.assertEqual(self.screen.payload.value, value)
        self.assertEqual(self.screen.topic.value, 'siot/test')

    def test_message_swipe_is_bounded_and_newest_first(self):
        for i in range(5):self.state.receive('alice/first',str(i),i+1)
        self.screen.draw()
        actions=[key for _,key in self.screen.buttons]
        self.assertNotIn('messages_prev',actions)
        first=next(key[1] for key in actions if isinstance(key,tuple) and key[0]=='detail')
        self.assertEqual(first['text'],'4')
        self.screen.gesture(('swipe',1),None,self.history)
        self.screen.draw()
        actions=[key for _,key in self.screen.buttons]
        self.assertNotIn('messages_next',actions)
        self.assertNotIn('messages_prev',actions)
        first=next(key[1] for key in actions if isinstance(key,tuple) and key[0]=='detail')
        self.assertEqual(first['text'],'0')
        self.screen.gesture(('swipe',1),None,self.history)
        self.assertEqual(self.screen.message_page,1)
        self.screen.gesture(('scroll',0),None,self.history)
        self.assertEqual(self.screen.message_page,0)

    def test_search_filters_case_insensitively_and_clear_restores_topics(self):
        self.click('search_topic')
        self.screen.search.insert('SECOND')
        self.click('search_apply')
        self.assertEqual(self.screen.topic_names(),['alice/second'])
        self.assertEqual(self.state.selected_topic,'alice/second')
        self.click('clear_search')
        self.assertEqual(len(self.screen.topic_names()),2)

    def test_topic_scroll_does_not_change_messages_or_open_topic(self):
        self.state.seed_topics(['alice/'+str(i) for i in range(20)])
        self.screen.gesture(('topic_scroll',1),None,self.history)
        self.assertEqual(self.screen.topic_page,3)
        self.assertEqual(self.screen.message_page,0)
        self.assertEqual(self.state.selected_topic,'alice/first')

    def test_display_modes_keep_original_messages_and_disable_chart_swipe(self):
        for i in range(5):self.state.receive('alice/first',str(i),i+1)
        self.click('view_chart')
        self.screen.gesture(('swipe',1),None,self.history)
        self.assertEqual(self.screen.message_page,0)
        self.assertEqual(len(self.state.history()),5)
        self.click('view_messages')
        self.assertEqual(self.screen.view,'messages')

    def media_session(self):
        session=Mock()
        session.mode=None;session.topic=None;session.continuous_topic=None;session.notice=''
        session.poll.return_value={'sequence':0,'frame':None,'status':'waiting','fps':0}
        self.screen.media_session=session
        return session

    def test_media_button_opens_three_source_choices_without_opening_camera(self):
        session=self.media_session()
        self.click('view_media')
        self.assertEqual(self.screen.page,'media_menu')
        self.screen.draw()
        actions=[a for _,a in self.screen.buttons]
        for key in ['media_topic_start','media_lan_menu','media_camera_start']:self.assertIn(key,actions)
        session.start_camera.assert_not_called()

    def test_media_topic_binds_selected_topic_and_camera_start_is_explicit(self):
        session=self.media_session();self.click('view_media');self.click('media_topic_start')
        session.start_topic.assert_called_once_with('alice/first',None)
        self.assertEqual(self.screen.page,'media_view')
        self.click('media_menu');self.click('media_camera_start')
        session.start_camera.assert_not_called()
        self.assertEqual(self.screen.page,'media_format')
        self.click('media_format_base64')
        session.start_camera.assert_called_once_with('base64',target_fps=5)

    def test_returning_to_monitor_stops_media_sources_and_sending(self):
        session=self.media_session();self.click('view_media');self.click('monitor')
        self.assertEqual(self.screen.page,'monitor')
        self.assertEqual(session.stop.call_count,2)

    def test_media_url_editor_routes_text_and_starts_only_on_confirm(self):
        session=self.media_session();self.click('view_media');self.click('media_lan_menu');self.click('media_url_edit')
        self.assertEqual(self.screen.focus,'media_url')
        self.screen.editor().insert('http://127.0.0.1/image.jpg')
        session.start_url.assert_not_called()
        self.click('media_url_start')
        session.start_url.assert_called_once_with('http://127.0.0.1/image.jpg')

    def test_lan_sender_is_explicit_third_level_choice(self):
        session=self.media_session();self.click('view_media');self.click('media_lan_menu')
        session.start_lan_sender.assert_not_called()
        self.click('media_lan_start')
        session.start_lan_sender.assert_called_once()
        self.assertEqual(self.screen.page,'media_view')

    def test_large_recognition_has_bounded_explicitly_truncated_detail(self):
        session=self.media_session();self.screen.page='media_view'
        self.screen.media_state={'recognition':'中'*5000,'received_at':1.,'status':'image','frame':None}
        self.click('media_result_detail')
        self.assertTrue(self.screen.detail['truncated'])
        self.assertLessEqual(len(self.screen.detail['text'].encode('utf-8')),4096)
        self.assertEqual(self.screen.page,'detail')
        session.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
