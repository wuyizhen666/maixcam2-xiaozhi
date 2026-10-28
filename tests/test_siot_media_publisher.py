import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]/'apps/siot_monitor'
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('siot_media_publisher',ROOT/'media_publisher.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Info:
    def __init__(self, complete=True, rc=0):
        self.rc=rc
        self.done=threading.Event()
        if complete:self.done.set()
    def is_published(self):return self.done.is_set()
    def wait_for_publish(self, timeout=None):self.done.wait(timeout)


class FakeClient:
    def __init__(self):
        self.records=[];self.raw_payloads=[];self.infos=[];self.complete=True;self.rc=0;self.stopped=False
    def username_pw_set(self,*args):pass
    def reconnect_delay_set(self,**kwargs):pass
    def max_queued_messages_set(self,value):self.queue_limit=value
    def max_inflight_messages_set(self,value):self.inflight_limit=value
    def connect_async(self,*args,**kwargs):pass
    def loop_start(self):self.on_connect(self,None,None,types.SimpleNamespace(is_failure=False),None)
    def disconnect(self):self.on_disconnect(self,None,None,None,None)
    def loop_stop(self):self.stopped=True
    def publish(self,topic,payload,**kwargs):
        self.raw_payloads.append(payload)
        value=json.loads(payload) if payload.startswith(b'{') else payload.decode('ascii')
        self.records.append((time.monotonic(),topic,value,kwargs))
        info=Info(self.complete,self.rc);self.infos.append(info);return info


def wait_for(predicate, timeout=2):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if predicate():return True
        time.sleep(.005)
    return bool(predicate())


class MediaPublisherTests(unittest.TestCase):
    def setUp(self):
        self.clients=[]
        def factory():
            c=FakeClient();self.clients.append(c);return c
        self.publisher=module.MediaPublisher({'host':'localhost','username':'fixture','password':'fixture'},factory=factory)
        self.addCleanup(self.publisher.stop)
        data=io.BytesIO();Image.new('RGB',(320,240),'green').save(data,format='JPEG');self.jpeg=data.getvalue()
    def submit(self,seq=1,topic='camera/live'):
        return self.publisher.submit(topic,self.jpeg,seq,time.time(),320,240)

    def test_start_is_idempotent_and_stop_restart_uses_new_connection(self):
        self.publisher.start();self.publisher.start()
        self.assertEqual(len(self.clients),1)
        self.assertTrue(self.publisher.snapshot()['connected'])
        old=self.clients[0];self.publisher.stop()
        self.assertFalse(self.publisher.snapshot()['connected'])
        self.publisher.start()
        self.assertEqual(len(self.clients),2)
        # Stale callbacks from the old connection cannot change the new state.
        old.on_disconnect(old,None,None,None,None)
        self.assertTrue(self.publisher.snapshot()['connected'])

    def test_frame_envelope_and_local_send_status(self):
        self.publisher.start();rid=self.submit(42)
        self.assertIsInstance(rid,int)
        self.assertTrue(wait_for(lambda:self.publisher.snapshot()['sent']==1))
        _,topic,payload,options=self.clients[0].records[0]
        self.assertEqual(topic,'camera/live')
        self.assertEqual(options,{'qos':0,'retain':False})
        self.assertEqual(payload['type'],'image');self.assertEqual(payload['mime'],'image/jpeg')
        self.assertEqual(payload['encoding'],'base64');self.assertEqual(payload['sequence'],42)
        self.assertEqual((payload['width'],payload['height']),(320,240))
        self.assertEqual(base64.b64decode(payload['data']),self.jpeg)
        self.assertEqual(self.publisher.snapshot()['last_sequence'],42)
        self.assertEqual(self.clients[0].queue_limit,1)
        self.assertEqual(self.clients[0].inflight_limit,1)

    def test_one_pending_latest_frame_replaces_older_and_counts_drops(self):
        self.publisher.start();c=self.clients[0];c.complete=False
        self.submit(1);self.assertTrue(wait_for(lambda:len(c.records)==1))
        self.submit(2);self.submit(3);self.submit(4)
        self.assertEqual(self.publisher.snapshot()['dropped'],2)
        self.assertEqual(len(c.records),1)
        c.complete=True;c.infos[0].done.set()
        self.assertTrue(wait_for(lambda:len(c.records)==2))
        self.assertEqual([r[2]['sequence'] for r in c.records],[1,4])

    def test_publish_calls_are_throttled_to_five_per_second(self):
        self.publisher.start();c=self.clients[0]
        for seq in range(3):
            self.submit(seq)
            self.assertTrue(wait_for(lambda:len(c.records)>=seq+1))
        gaps=[b[0]-a[0] for a,b in zip(c.records,c.records[1:])]
        self.assertTrue(all(gap>=.195 for gap in gaps),gaps)

    def test_offline_frames_are_dropped_not_replayed_on_reconnect(self):
        self.publisher.start();c=self.clients[0]
        c.on_disconnect(c,None,None,None,None)
        for sequence in (1,2):
            with self.assertRaisesRegex(RuntimeError,'尚未连接'):
                self.submit(sequence)
        self.assertEqual(len(c.records),0)
        self.assertEqual(self.publisher.snapshot()['dropped'],2)
        c.on_connect(c,None,None,types.SimpleNamespace(is_failure=False),None)
        self.submit(3)
        self.assertTrue(wait_for(lambda:len(c.records)==1))
        self.assertEqual(c.records[0][2]['sequence'],3)

    def test_stop_clears_queued_video_and_does_not_block_on_pending_send(self):
        self.publisher.start();c=self.clients[0];c.complete=False
        self.submit(1);self.assertTrue(wait_for(lambda:len(c.records)==1))
        self.submit(2)
        started=time.monotonic();self.publisher.stop()
        self.assertLess(time.monotonic()-started,.2)
        c.infos[0].done.set();time.sleep(.05)
        self.assertEqual(len(c.records),1)
        self.publisher.start();self.submit(3)
        self.assertTrue(wait_for(lambda:len(self.clients[1].records)==1))
        self.assertEqual(self.clients[1].records[0][2]['sequence'],3)

    def test_invalid_payload_metadata_and_topic_rejected(self):
        self.publisher.start()
        bad=[('bad/#',self.jpeg,1,1.,320,240),('camera/live',b'not jpeg',1,1.,320,240),
             ('camera/live',b'\xff\xd8'+bytes(512*1024)+b'\xff\xd9',1,1.,320,240),
             ('camera/live',self.jpeg,True,1.,320,240),('camera/live',self.jpeg,-1,1.,320,240),
             ('camera/live',self.jpeg,1,float('nan'),320,240),('camera/live',self.jpeg,1,1.,True,240),
             ('camera/live',self.jpeg,1,1.,0,240),('camera/live',self.jpeg,1,1.,320,4096)]
        for args in bad:
            with self.assertRaises(ValueError):self.publisher.submit(*args)
        self.assertEqual(self.clients[0].records,[])

    def test_mismatched_jpeg_dimensions_fail_in_worker_without_publish(self):
        self.publisher.start();self.publisher.submit('camera/live',self.jpeg,1,1.,640,480)
        self.assertTrue(wait_for(lambda:self.publisher.snapshot()['error'] is not None))
        self.assertEqual(self.clients[0].records,[])

    def test_disconnect_during_encoding_drops_stale_frame_before_publish(self):
        self.publisher.start();c=self.clients[0]
        entered=threading.Event();release=threading.Event()
        encode=self.publisher._encode
        def slow_encode(frame,wire_format='json'):
            entered.set();release.wait(2);return encode(frame,wire_format)
        self.publisher._encode=slow_encode
        self.submit(1);self.assertTrue(entered.wait(1))
        c.on_disconnect(c,None,None,None,None)
        c.on_connect(c,None,None,types.SimpleNamespace(is_failure=False),None)
        release.set()
        self.assertTrue(wait_for(lambda:self.publisher.snapshot()['dropped']==1))
        self.assertEqual(c.records,[])
        self.publisher._encode=encode;self.submit(2)
        self.assertTrue(wait_for(lambda:self.publisher.snapshot()['sent']==1))
        self.assertEqual(c.records[0][2]['sequence'],2)

    def test_rejected_qos0_frame_is_not_counted_as_sent_or_retried(self):
        self.publisher.start();c=self.clients[0];c.rc=4
        self.submit(1)
        self.assertTrue(wait_for(lambda:self.publisher.snapshot()['dropped']==1))
        self.assertEqual(self.publisher.snapshot()['sent'],0)
        self.assertIsNotNone(self.publisher.snapshot()['error'])
        time.sleep(.25);self.assertEqual(len(c.records),1)
        c.rc=0;self.submit(2)
        self.assertTrue(wait_for(lambda:self.publisher.snapshot()['sent']==1))
        self.assertEqual(self.publisher.snapshot()['last_sequence'],2)

    def test_encoding_and_publish_run_on_worker_not_submit_caller(self):
        caller=threading.get_ident();seen=[]
        self.publisher.start();c=self.clients[0];encode=self.publisher._encode;publish=c.publish
        def checked_encode(frame,wire_format='json'):seen.append(threading.get_ident());return encode(frame,wire_format)
        def checked_publish(*args,**kwargs):seen.append(threading.get_ident());return publish(*args,**kwargs)
        self.publisher._encode=checked_encode;c.publish=checked_publish
        self.submit(1);self.assertTrue(wait_for(lambda:self.publisher.snapshot()['sent']==1))
        self.assertEqual(len(seen),2);self.assertTrue(all(x!=caller for x in seen))

    def test_mindplus_data_url_preserves_exact_jpeg_without_json(self):
        self.publisher.set_format('base64');self.publisher.start();self.submit(73)
        self.assertTrue(wait_for(lambda:self.publisher.snapshot()['sent']==1))
        payload=self.clients[0].raw_payloads[0]
        self.assertEqual(payload,b'data:image/jpeg;base64,'+base64.b64encode(self.jpeg))
        self.assertEqual(base64.b64decode(payload.split(b',',1)[1],validate=True),self.jpeg)
        self.assertFalse(payload.startswith(b'"'))
        self.assertEqual(self.clients[0].records[0][3],{'qos':0,'retain':False})
        self.assertEqual(self.publisher.snapshot()['wire_format'],'base64')
        self.assertEqual(self.publisher.snapshot()['target_fps'],5)
        self.assertEqual(self.publisher.snapshot()['last_sequence'],73)

    def test_json_default_and_active_format_change_rejected(self):
        self.assertEqual(self.publisher.snapshot()['wire_format'],'json')
        self.assertEqual(self.publisher.snapshot()['target_fps'],5)
        self.publisher.start()
        for wire_format in ('json','base64'):
            with self.assertRaises(RuntimeError):self.publisher.set_format(wire_format)
        self.submit(9);self.assertTrue(wait_for(lambda:self.publisher.snapshot()['sent']==1))
        self.assertEqual(self.clients[0].records[0][2]['sequence'],9)
        self.assertEqual(self.publisher.snapshot()['wire_format'],'json')

    def test_invalid_format_is_rejected_before_start(self):
        for invalid in ('BASE64','raw','',None,[],1):
            with self.assertRaises(ValueError):self.publisher.set_format(invalid)
        self.assertEqual(self.publisher.snapshot()['wire_format'],'json')
        with self.assertRaises(ValueError):module.MediaPublisher({},wire_format='raw')
        other=module.MediaPublisher({},wire_format='base64')
        self.assertEqual(other.snapshot()['wire_format'],'base64')

    def test_stop_switch_discards_old_queued_and_inflight_frames(self):
        self.publisher.start();old=self.clients[0];old.complete=False
        self.submit(1);self.assertTrue(wait_for(lambda:len(old.records)==1));self.submit(2)
        self.publisher.stop();self.publisher.set_format('base64');self.publisher.start();self.submit(3)
        self.assertTrue(wait_for(lambda:len(self.clients[1].records)==1))
        old.infos[0].done.set();time.sleep(.05)
        self.assertEqual(len(old.records),1)
        self.assertEqual(self.clients[1].raw_payloads,[b'data:image/jpeg;base64,'+base64.b64encode(self.jpeg)])
        self.assertEqual(self.publisher.snapshot()['last_sequence'],3)

    def test_encoding_uses_immutable_format_across_restart(self):
        entered=threading.Event();release=threading.Event();finished=threading.Event();formats=[]
        encode=self.publisher._encode
        def paused_encode(frame,wire_format='json'):
            formats.append((frame[3],wire_format))
            if frame[3]==1:
                entered.set();release.wait(2)
            value=encode(frame,wire_format)
            if frame[3]==1:finished.set()
            return value
        self.publisher._encode=paused_encode;self.publisher.start();self.submit(1)
        self.assertTrue(entered.wait(1))
        self.publisher.stop();self.publisher.set_format('base64');self.publisher.start();self.submit(2)
        self.assertTrue(wait_for(lambda:self.publisher.snapshot()['sent']==1))
        release.set();self.assertTrue(finished.wait(1));time.sleep(.05)
        self.assertEqual(formats,[(1,'json'),(2,'base64')])
        self.assertEqual(self.clients[0].records,[])
        self.assertEqual(self.clients[1].raw_payloads,[b'data:image/jpeg;base64,'+base64.b64encode(self.jpeg)])

    def test_mindplus_publish_calls_are_throttled_to_two_per_second(self):
        self.publisher.set_format('base64');self.publisher.set_rate(2);self.publisher.start();client=self.clients[0]
        for sequence in range(3):
            self.submit(sequence)
            self.assertTrue(wait_for(lambda:len(client.records)>=sequence+1))
        times=[record[0] for record in client.records]
        self.assertTrue(all(b-a>=.495 for a,b in zip(times,times[1:])),times)
        self.assertGreaterEqual(times[-1]-times[0],.995)

    def test_invalid_rate_and_active_rate_change_are_rejected(self):
        for rate in (0,-1,3,20,True,None,'5',5.0,[]):
            with self.assertRaises(ValueError):self.publisher.set_rate(rate)
        self.assertEqual(self.publisher.snapshot()['target_fps'],5)
        self.publisher.set_rate(1)
        self.assertEqual(self.publisher.snapshot()['target_fps'],1)
        self.publisher.start()
        for rate in (1,2,5,10):
            with self.assertRaises(RuntimeError):self.publisher.set_rate(rate)
        self.assertEqual(self.publisher.snapshot()['target_fps'],1)

    def test_chosen_rate_survives_stopped_format_switch_and_restart(self):
        self.publisher.set_rate(10);self.publisher.start();self.publisher.stop()
        self.publisher.set_format('base64')
        self.assertEqual(self.publisher.snapshot()['target_fps'],10)
        self.publisher.start();self.submit(1)
        self.assertTrue(wait_for(lambda:self.publisher.snapshot()['sent']==1))
        self.assertEqual(self.clients[1].raw_payloads,[b'data:image/jpeg;base64,'+base64.b64encode(self.jpeg)])
        self.publisher.stop();self.publisher.set_format('json')
        self.assertEqual(self.publisher.snapshot()['target_fps'],10)

    def test_mindplus_selected_five_and_ten_fps_intervals(self):
        for rate,minimum in ((5,.195),(10,.095)):
            self.publisher.stop();self.publisher.set_format('base64');self.publisher.set_rate(rate)
            self.publisher.start();client=self.clients[-1]
            for sequence in range(3):
                self.submit(sequence)
                self.assertTrue(wait_for(lambda:len(client.records)>=sequence+1))
            times=[record[0] for record in client.records]
            self.assertTrue(all(b-a>=minimum for a,b in zip(times,times[1:])),(rate,times))
            self.assertEqual(self.publisher.snapshot()['target_fps'],rate)


if __name__=='__main__':unittest.main()
