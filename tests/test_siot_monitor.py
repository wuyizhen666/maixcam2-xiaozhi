import importlib.util
from pathlib import Path
import unittest
import sys
import types
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1] / 'apps/siot_monitor'
spec = importlib.util.spec_from_file_location('siot_monitor_model', ROOT / 'model.py')
model = importlib.util.module_from_spec(spec)
if spec.loader:
    spec.loader.exec_module(model)


class MonitorModelTests(unittest.TestCase):
    def test_publish_validation_uses_utf8_limits(self):
        model.validate_publish('demo/test', '你好')
        for topic in ['', 'demo/#', 'a/+', 'x\0y', '中' * 43]:
            with self.assertRaises(ValueError):
                model.validate_publish(topic, 'ok')
        with self.assertRaises(ValueError):
            model.validate_publish('demo/test', '中' * 683)

    def test_history_is_bounded_and_topic_selection_stays_put(self):
        state = model.MonitorModel(max_topics=3, max_messages=2)
        state.receive('a/one', b'first', 1)
        state.receive('b/two', b'second', 2)
        self.assertEqual(state.selected_topic, 'a/one')
        state.receive('a/one', b'third', 3)
        state.receive('a/one', b'fourth', 4)
        self.assertEqual([x['text'] for x in state.history()], ['third', 'fourth'])
        state.receive('c/three', b'x', 5)
        state.receive('d/four', b'y', 6)
        self.assertEqual(len(state.topics), 3)
        self.assertIn('a/one', state.topics)

    def test_editor_inserts_at_cursor_and_backspaces_unicode(self):
        editor = model.TextEditor('你好')
        editor.move(-1)
        editor.insert('测')
        self.assertEqual(editor.value, '你测好')
        editor.backspace()
        self.assertEqual(editor.value, '你好')
        editor.insert('\n')
        self.assertEqual(editor.value, '你\n好')

    def test_editor_limit_does_not_partially_insert(self):
        editor = model.TextEditor('a', limit=3)
        with self.assertRaises(ValueError):
            editor.insert('中')
        self.assertEqual(editor.value, 'a')

    def test_press_edge_does_not_repeat_while_held(self):
        edge = model.PressEdge()
        self.assertTrue(edge.update(True))
        self.assertFalse(edge.update(True))
        self.assertFalse(edge.update(False))
        self.assertTrue(edge.update(True))

    def test_long_payload_is_marked_truncated(self):
        state = model.MonitorModel()
        state.receive('demo/test', b'x' * 5000, 1)
        message = state.history()[0]
        self.assertEqual(message['bytes'], 5000)
        self.assertTrue(message['truncated'])
        self.assertLessEqual(len(message['text'].encode('utf-8')), 4096)

    def test_pages_are_clamped(self):
        self.assertEqual(model.page_items(list(range(8)), 99, 3), ([6, 7], 2, 3))
        self.assertEqual(model.page_items([], 9, 3), ([], 0, 1))

    def test_history_refresh_deduplicates_server_ids(self):
        state=model.MonitorModel()
        state.receive('demo/test',b'live',100)
        history=[{'id':7,'content':'历史','timestamp':90}]
        state.add_history('demo/test',history)
        state.add_history('demo/test',history)
        self.assertEqual(len(state.history()),2)
        self.assertEqual(state.history()[0]['source'],'历史')
        self.assertEqual(state.history()[1]['text'],'live')

    def test_saved_topic_namespace_returns_relative_rest_name(self):
        self.assertEqual(model.saved_topic_name('alice/project/topic','alice'),'project/topic')
        for topic in ['alice','alice/','bob/test','alice2/test']:
            with self.assertRaises(ValueError):model.saved_topic_name(topic,'alice')

    def test_hardware_edge_points_hit_back_and_outer_keyboard_keys(self):
        self.assertTrue(model.button_contains((8,6,80,42),1,0))
        self.assertTrue(model.button_contains((8,6,80,42),1,2))
        self.assertTrue(model.button_contains((8,254,58,40),0,270))
        self.assertTrue(model.button_contains((566,254,58,40),639,270))
        self.assertTrue(model.button_contains((528,432,104,44),639,479))

    def test_internal_button_regions_remain_disjoint(self):
        left=(8,254,58,40);right=(70,254,58,40)
        for x in range(640):
            self.assertFalse(model.button_contains(left,x,270) and model.button_contains(right,x,270))
        self.assertFalse(model.button_contains(left,68,270))


sys.path.insert(0,str(ROOT))
client_spec=importlib.util.spec_from_file_location('siot_monitor_client',ROOT/'mqtt_client.py')
client_module=importlib.util.module_from_spec(client_spec)
client_spec.loader.exec_module(client_module)


class FakeClient:
    def __init__(self):self.sent=[];self.mid=11;self.rc=0;self.before_return=None
    def username_pw_set(self,*args):pass
    def reconnect_delay_set(self,**kwargs):pass
    def max_queued_messages_set(self,*args):pass
    def subscribe(self,*args,**kwargs):pass
    def publish(self,*args,**kwargs):
        self.sent.append((args,kwargs));self.mid+=1
        if self.before_return:self.before_return(self.mid)
        return types.SimpleNamespace(rc=self.rc,mid=self.mid)


class MonitorClientTests(unittest.TestCase):
    def setUp(self):
        self.registry=types.SimpleNamespace(ensure_topic=Mock())
        self.client=client_module.MonitorClient({'username':'fixture','password':'fixture'},factory=FakeClient,registry=self.registry)

    def process_next(self):
        self.client._process_send(self.client._send_queue.get_nowait())

    def test_offline_publish_is_rejected_without_queueing(self):
        with self.assertRaises(RuntimeError):self.client.publish('fixture/test','hello')
        self.assertEqual(self.client.client.sent,[])

    def test_save_uses_qos_one_and_does_not_retain(self):
        self.client.connected=True
        request=self.client.publish('fixture/test','你好',True)
        self.assertEqual(self.client.client.sent,[], 'No network work may run on the UI thread')
        self.registry.ensure_topic.assert_not_called()
        self.process_next()
        self.registry.ensure_topic.assert_called_once_with('fixture/test')
        args,kw=self.client.client.sent[-1]
        self.assertEqual(args,('fixture/test','你好'.encode()))
        self.assertEqual(kw,{'qos':1,'retain':False})
        self.client.publish('demo/test','0',False)
        self.process_next()
        self.assertEqual(self.client.client.sent[-1][1]['qos'],0)

    def test_foreign_namespace_rejected_before_queueing_saved_message(self):
        self.client.connected=True
        with self.assertRaises(ValueError):self.client.publish('other/test','hello',True)
        self.assertTrue(self.client._send_queue.empty())

    def test_registration_failure_never_publishes_or_downgrades(self):
        self.client.connected=True
        self.registry.ensure_topic.side_effect=RuntimeError('registration failed')
        request=self.client.publish('fixture/new','hello',True)
        self.process_next()
        self.assertEqual(self.client.client.sent,[])
        self.assertTrue(any(e[0]=='send_error' and e[1]==request for e in self.client.drain()))
        self.assertFalse(self.client._outstanding)

    def test_send_capacity_counts_unacknowledged_messages(self):
        self.client.connected=True
        for _ in range(8):self.client.publish('ordinary/topic','x',False)
        with self.assertRaises(RuntimeError):self.client.publish('ordinary/topic','x',False)

    def test_qos1_disconnect_race_remains_pending_until_ack_without_retry(self):
        self.client.connected=True
        self.client.client.rc=4  # Public Paho MQTT_ERR_NO_CONN, after QoS1 is queued.
        request=self.client.publish('fixture/test','one request',True)
        self.process_next()
        events=self.client.drain()
        self.assertTrue(any(e[0]=='send_progress' and '重连' in e[2] for e in events))
        self.assertFalse(any(e[0] in ('send_error','published') for e in events))
        self.assertIn(request,self.client._outstanding)
        self.assertEqual(self.client._pending_mids[12],(request,True))
        self.client._on_disconnect(None,None,None,None,None)
        self.assertIn(request,self.client._outstanding)
        self.client._on_connect(self.client.client,None,None,types.SimpleNamespace(is_failure=False),None)
        self.assertEqual(len(self.client.client.sent),1, 'Paho owns reconnect replay; never publish again')
        self.client._on_publish(None,None,12,types.SimpleNamespace(is_failure=False),None)
        events=self.client.drain()
        self.assertIn(('published',request,True,True),events)
        self.assertNotIn(request,self.client._outstanding)
        self.assertNotIn(12,self.client._pending_mids)

    def test_qos0_disconnect_race_reports_not_accepted(self):
        self.client.connected=True
        self.client.client.rc=4
        request=self.client.publish('ordinary/topic','not queued',False)
        self.process_next()
        events=self.client.drain()
        self.assertTrue(any(e[0]=='send_error' and e[1]==request for e in events))
        self.assertFalse(any(e[0]=='published' for e in events))
        self.assertNotIn(request,self.client._outstanding)
        self.assertFalse(self.client._pending_mids)

    def test_qos1_no_connection_with_early_ack_finishes_after_progress(self):
        self.client.connected=True
        self.client.client.rc=4
        self.client.client.before_return=lambda mid:self.client._on_publish(
            None,None,mid,types.SimpleNamespace(is_failure=False),None)
        request=self.client.publish('fixture/test','early acknowledgement',True)
        self.process_next()
        events=self.client.drain()
        self.assertEqual(events[-1],('published',request,True,True))
        self.assertTrue(any(e[0]=='send_progress' and '重连' in e[2] for e in events[:-1]))
        self.assertFalse(any(e[0]=='send_error' for e in events))
        self.assertFalse(self.client._outstanding)
        self.assertFalse(self.client._pending_mids)
        self.assertFalse(self.client._early_acks)

    def test_bounded_message_queue_does_not_drop_control_ack(self):
        message=types.SimpleNamespace(topic='demo/test',payload=b'hello',retain=False)
        for _ in range(300):self.client._on_message(None,None,message)
        self.client.connected=True
        request=self.client.publish('fixture/test','ack',True)
        self.process_next()
        self.client._on_publish(None,None,12,types.SimpleNamespace(is_failure=False),None)
        events=self.client.drain()
        self.assertIn(('published',request,True,True),events)
        self.assertEqual(self.client.dropped,44)
        self.assertEqual(sum(e[0]=='message' for e in events),64)

    def test_media_retains_full_payload_only_for_selected_topic(self):
        self.client.set_media_topic('camera/one')
        payload=b'x'*8192
        self.client._on_message(None,None,types.SimpleNamespace(topic='camera/other',payload=payload,retain=False))
        self.assertFalse(any(e[0]=='media' for e in self.client.drain()))
        self.client._on_message(None,None,types.SimpleNamespace(topic='camera/one',payload=payload,retain=False))
        events=self.client.drain()
        media=next(e for e in events if e[0]=='media')
        text=next(e for e in events if e[0]=='message')
        self.assertEqual(media[1:3],('camera/one',payload))
        self.assertEqual(len(text[2]),4096)
        self.assertEqual(text[5],8192)

    def test_media_latest_frame_wins_and_topic_switch_clears_old_frame(self):
        self.client.set_media_topic('camera/one')
        for payload in [b'one',b'two',b'three']:
            self.client._on_message(None,None,types.SimpleNamespace(topic='camera/one',payload=payload,retain=False))
        self.client.set_media_topic('camera/one')
        self.assertEqual([e[2] for e in self.client.drain() if e[0]=='media'],[b'three'])
        self.client._on_message(None,None,types.SimpleNamespace(topic='camera/one',payload=b'old',retain=False))
        self.client.set_media_topic('camera/two')
        self.assertFalse(any(e[0]=='media' for e in self.client.drain()))
        self.client._on_message(None,None,types.SimpleNamespace(topic='camera/one',payload=b'late',retain=False))
        self.assertFalse(any(e[0]=='media' for e in self.client.drain()))
        self.client.set_media_topic(None)

    def test_selected_oversized_media_emits_bounded_error(self):
        self.client.set_media_topic('camera/one')
        self.client._on_message(None,None,types.SimpleNamespace(topic='camera/one',payload=b'x'*(3*1024*1024+1),retain=False))
        events=self.client.drain()
        self.assertFalse(any(e[0]=='media' for e in events))
        self.assertTrue(any(e[0]=='media_error' and e[1]=='camera/one' for e in events))


class TopicRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.api=client_module.HistoryClient({'host':'localhost','username':'alice','password':'fixture'})

    def test_registered_topic_does_not_create_again(self):
        self.api._request=Mock(return_value={'code':0,'data':{'topics':[{'name':'alice/monitor'}]}})
        self.api.ensure_topic('alice/monitor')
        self.assertEqual(self.api._request.call_count,1)

    def test_missing_topic_is_created_relative_then_verified(self):
        self.api._request=Mock(side_effect=[{'code':0,'data':{'topics':[]}},
                                           {'code':0,'data':None},
                                           {'code':0,'data':{'topics':[{'name':'alice/project/new'}]}}])
        self.api.ensure_topic('alice/project/new')
        create=self.api._request.call_args_list[1]
        self.assertEqual(create.args,('/topics',{'name':'project/new'}))
        self.assertEqual(create.kwargs,{'method':'PUT'})

    def test_wrong_full_name_after_create_is_failure(self):
        self.api._request=Mock(side_effect=[{'code':0,'data':{'topics':[]}},
                                           {'code':0,'data':{}},
                                           {'code':0,'data':{'topics':[{'name':'alice/alice/new'}]}}])
        with self.assertRaises(RuntimeError):self.api.ensure_topic('alice/new')

    def test_create_topic_does_not_publish_a_message(self):
        self.api.ensure_topic=Mock()
        self.api.create_topic('alice/new')
        self.api.ensure_topic.assert_called_once_with('alice/new')

    def test_refresh_pressure_cannot_discard_mutations(self):
        requests=[self.api.enqueue_topic_change('create','alice/topic%d'%i) for i in range(4)]
        for i in range(25):
            self.api.refresh_topics()
            self.api.refresh_history('alice/read%d'%i)
        with self.assertRaises(RuntimeError):self.api.enqueue_topic_change('create','alice/extra')
        queued=[self.api.requests.get_nowait() for _ in range(4)]
        self.assertEqual([r[1] for r in queued],requests)
        self.assertEqual([r[2] for r in queued],['alice/topic%d'%i for i in range(4)])

    def test_enqueue_mutation_has_no_network_side_effect(self):
        self.api.create_topic=Mock()
        self.api._request=Mock()
        rid=self.api.enqueue_topic_change('create','alice/new')
        self.api.create_topic.assert_not_called()
        self.api._request.assert_not_called()
        self.api._process_change(self.api.requests.get_nowait())
        self.api.create_topic.assert_called_once_with('alice/new')
        self.assertIn(('topic_created',rid,'alice/new'),self.api.drain())

    def test_mutation_result_is_not_dropped_by_history_notifications(self):
        self.api.create_topic=Mock()
        rid=self.api.enqueue_topic_change('create','alice/new')
        self.api._process_change(self.api.requests.get_nowait())
        for _ in range(30):self.api._emit_read(('topics',[]))
        self.assertIn(('topic_created',rid,'alice/new'),self.api.drain())

    def test_failed_change_emits_safe_error_with_request_id(self):
        self.api.create_topic=Mock(side_effect=RuntimeError('sensitive-server-detail'))
        rid=self.api.enqueue_topic_change('create','alice/new')
        self.api._process_change(self.api.requests.get_nowait())
        event=self.api.drain()[0]
        self.assertEqual(event[:2],('topic_error',rid))
        self.assertNotIn('sensitive-server-detail',event[2])

    def test_invalid_action_or_foreign_topic_not_enqueued(self):
        for action,topic in [('rename','alice/a'),('create','other/a'),('delete','alice/')]:
            with self.assertRaises(ValueError):self.api.enqueue_topic_change(action,topic)
        self.assertTrue(self.api.requests.empty())

    def test_delete_binds_id_name_and_confirms_absence(self):
        topic={'id':17,'name':'alice/to-delete'}
        self.api._request=Mock(side_effect=[{'code':0,'data':{'topics':[topic]}},
                                           {'code':0,'data':{'topics':[topic]}},
                                           {'code':0,'data':None},
                                           {'code':0,'data':{'topics':[]}}])
        self.api.delete_topic('alice/to-delete')
        request=self.api._request.call_args_list[2]
        self.assertEqual(request.args,('/topics/17',))
        self.assertEqual(request.kwargs,{'method':'DELETE'})
        self.assertEqual(self.api._request.call_count,4)

    def test_recreated_or_renamed_topic_is_not_deleted(self):
        for changed in [{'id':18,'name':'alice/to-delete'},
                        {'id':17,'name':'alice/different'}]:
            with self.subTest(changed=changed):
                self.api._request=Mock(side_effect=[{'code':0,'data':{'topics':[{'id':17,'name':'alice/to-delete'}]}},
                                                   {'code':0,'data':{'topics':[changed]}}])
                with self.assertRaises(RuntimeError):self.api.delete_topic('alice/to-delete')
                self.assertFalse(any(c.kwargs.get('method')=='DELETE' for c in self.api._request.call_args_list))

    def test_invalid_or_missing_topic_id_is_not_deleted(self):
        for record in [None,{'name':'alice/to-delete','id':'17/other'},
                       {'name':'alice/to-delete','id':True},
                       {'name':'alice/to-delete','id':0}]:
            with self.subTest(record=record):
                self.api._request=Mock(return_value={'code':0,'data':{'topics':[] if record is None else [record]}})
                with self.assertRaises(RuntimeError):self.api.delete_topic('alice/to-delete')
                self.assertFalse(any(c.kwargs.get('method')=='DELETE' for c in self.api._request.call_args_list))

    def test_success_code_without_confirmed_delete_is_not_success(self):
        topic={'id':17,'name':'alice/to-delete'}
        self.api._request=Mock(side_effect=[{'code':0,'data':{'topics':[topic]}},
                                           {'code':0,'data':{'topics':[topic]}},
                                           {'code':0,'data':None},
                                           {'code':0,'data':{'topics':[topic]}}])
        request=self.api.enqueue_topic_change('delete','alice/to-delete')
        self.api._process_change(self.api.requests.get_nowait())
        result=self.api.drain()
        self.assertEqual(result[0][:2],('topic_error',request))
        self.assertFalse(any(r[0]=='topic_deleted' for r in result))

    def test_delete_worker_reports_request_identity(self):
        self.api.delete_topic=Mock()
        request=self.api.enqueue_topic_change('delete','alice/to-delete')
        self.api._process_change(self.api.requests.get_nowait())
        self.api.delete_topic.assert_called_once_with('alice/to-delete')
        self.assertIn(('topic_deleted',request,'alice/to-delete'),self.api.drain())

    def test_stale_reads_are_delivered_before_delete_completion(self):
        self.api.delete_topic=Mock()
        self.api._emit_read(('topics',['alice/to-delete']))
        self.api._emit_read(('history','alice/to-delete',[{'id':1,'content':'old','timestamp':1}]))
        rid=self.api.enqueue_topic_change('delete','alice/to-delete')
        self.api._process_change(self.api.requests.get_nowait())
        events=self.api.drain()
        self.assertEqual(events[-1],('topic_deleted',rid,'alice/to-delete'))
        displayed=set()
        for event in events:
            if event[0]=='topics':displayed.update(event[1])
            elif event[0]=='history':displayed.add(event[1])
            elif event[0]=='topic_deleted':displayed.discard(event[2])
        self.assertNotIn('alice/to-delete',displayed)

    def test_delete_discards_only_previously_queued_history_for_that_topic(self):
        self.api.delete_topic=Mock()
        self.api.refresh_history('alice/to-delete')
        self.api.refresh_topics()
        self.api.refresh_history('alice/keep')
        self.api.refresh_history('alice/to-delete')
        self.api._process_change(('delete',1,'alice/to-delete'))
        queued=[]
        while not self.api.read_requests.empty():queued.append(self.api.read_requests.get_nowait())
        self.assertEqual(queued,[('topics',None),('history','alice/keep')])
        # Later explicit refreshes remain allowed; deletion is not a blacklist.
        self.api.refresh_history('alice/to-delete')
        self.assertEqual(self.api.read_requests.get_nowait(),('history','alice/to-delete'))

    def test_failed_delete_does_not_discard_history_reads(self):
        self.api.delete_topic=Mock(side_effect=RuntimeError('failed'))
        self.api.refresh_history('alice/to-delete')
        self.api._process_change(('delete',1,'alice/to-delete'))
        self.assertEqual(self.api.read_requests.get_nowait(),('history','alice/to-delete'))

    def test_genuinely_new_read_stays_after_delete_completion(self):
        self.api.delete_topic=Mock()
        self.api._process_change(('delete',1,'alice/topic'))
        self.api._emit_read(('topics',['alice/topic']))
        self.assertEqual(self.api.drain(),[('topic_deleted',1,'alice/topic'),
                                          ('topics',['alice/topic'])])


class TopicClearTests(unittest.TestCase):
    TOPIC='alice/chart'

    def setUp(self):
        self.api=client_module.HistoryClient({'host':'localhost','username':'alice','password':'fixture'})

    def route(self, before, after, final_id=17, fail_post=False):
        cleared=[False]
        def request(path,body=None,authenticated=True,method=None):
            self.assertNotIn(method,('DELETE','PUT'))
            if path.startswith('/topics?'):
                record={'id':final_id if cleared[0] else 17,'name':self.TOPIC}
                return {'code':0,'data':{'topics':[record],'total':1}}
            if path.startswith('/messages/getMsgByTopic?'):
                self.assertEqual(body,{'topic':self.TOPIC})
                query=client_module.urllib.parse.parse_qs(client_module.urllib.parse.urlsplit(path).query)
                offset=int(query['offset'][0]);length=int(query['length'][0])
                messages=after if cleared[0] else before
                return {'code':0,'data':{'messages':messages[offset:offset+length],'total':len(messages)}}
            self.assertEqual(path,'/messages/delMsgByTopic')
            self.assertEqual(method,'POST')
            self.assertEqual(body,{'topic':self.TOPIC})
            if fail_post:raise RuntimeError('synthetic request failure')
            cleared[0]=True
            return {'code':0,'data':None}
        self.api._request=Mock(side_effect=request)

    def test_clear_uses_post_and_preserves_registered_topic(self):
        self.route([{'id':1,'content':'10'}],[])
        self.api.clear_topic(self.TOPIC)
        mutations=[c for c in self.api._request.call_args_list if c.kwargs.get('method')]
        self.assertEqual(len(mutations),1)
        self.assertEqual(mutations[0].args,('/messages/delMsgByTopic',{'topic':self.TOPIC}))
        self.assertEqual(mutations[0].kwargs,{'method':'POST'})

    def test_new_messages_after_clear_are_allowed(self):
        self.route([{'id':1,'content':'old'}],[{'id':2,'content':'new'}])
        rid=self.api.enqueue_topic_change('clear',self.TOPIC)
        self.api._process_change(self.api.requests.get_nowait())
        self.assertIn(('topic_cleared',rid,self.TOPIC),self.api.drain())

    def test_old_message_hidden_behind_new_page_is_not_success(self):
        after=[{'id':n,'content':'new'} for n in range(100,130)]+[{'id':1,'content':'old'}]
        self.route([{'id':1,'content':'old'}],after)
        with self.assertRaises(RuntimeError):self.api.clear_topic(self.TOPIC)

    def test_changed_topic_id_after_clear_is_not_success(self):
        self.route([{'id':1}],[],final_id=18)
        rid=self.api.enqueue_topic_change('clear',self.TOPIC)
        self.api._process_change(self.api.requests.get_nowait())
        result=self.api.drain()
        self.assertEqual(result[0][:2],('topic_error',rid))
        self.assertFalse(any(e[0]=='topic_cleared' for e in result))

    def test_foreign_namespace_is_rejected_without_network(self):
        self.api._request=Mock()
        with self.assertRaises(ValueError):self.api.clear_topic('other/chart')
        with self.assertRaises(ValueError):self.api.enqueue_topic_change('clear','other/chart')
        self.api._request.assert_not_called()

    def test_api_failure_does_not_report_cleared_or_discard_history(self):
        self.route([{'id':1}],[],fail_post=True)
        self.api.refresh_history(self.TOPIC)
        rid=self.api.enqueue_topic_change('clear',self.TOPIC)
        self.api._process_change(self.api.requests.get_nowait())
        result=self.api.drain()
        self.assertEqual(result[0][:2],('topic_error',rid))
        self.assertEqual(self.api.read_requests.get_nowait(),('history',self.TOPIC))

    def test_clear_completion_follows_old_read_and_discards_stale_requests(self):
        self.route([{'id':1}],[])
        self.api._emit_read(('history',self.TOPIC,[{'id':1,'content':'old'}]))
        self.api.refresh_history(self.TOPIC)
        self.api.refresh_history('alice/keep')
        rid=self.api.enqueue_topic_change('clear',self.TOPIC)
        self.api._process_change(self.api.requests.get_nowait())
        self.assertEqual(self.api.drain()[-1],('topic_cleared',rid,self.TOPIC))
        self.assertEqual(self.api.read_requests.get_nowait(),('history','alice/keep'))
        self.assertTrue(self.api.read_requests.empty())

    def test_large_continuing_history_has_bounded_verification(self):
        self.route([{'id':1}],[{'id':n} for n in range(100,1000)])
        with self.assertRaisesRegex(RuntimeError,'bounded page budget'):
            self.api.clear_topic(self.TOPIC)
        history_calls=[c for c in self.api._request.call_args_list if c.args[0].startswith('/messages/getMsgByTopic?')]
        self.assertEqual(len(history_calls),17)  # one snapshot + at most 16 verification pages

    def test_changed_id_before_clear_prevents_mutating_request(self):
        self.api._lookup_topic=Mock(side_effect=[{'id':17,'name':self.TOPIC},
                                                {'id':18,'name':self.TOPIC}])
        self.api._history_id_page=Mock(return_value=({'1'},1,1))
        self.api._request=Mock()
        with self.assertRaises(RuntimeError):self.api.clear_topic(self.TOPIC)
        self.api._request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
