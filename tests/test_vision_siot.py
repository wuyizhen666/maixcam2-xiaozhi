import base64
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('vision_siot',ROOT/'xiaozhi/service/vision_siot.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def wait_for(predicate, timeout=2):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return True
        time.sleep(.005)
    return bool(predicate())


class Client:
    def __init__(self):
        self.records=[];self.auto_ack=True;self.result_rc=0;self.mid=0
    def username_pw_set(self,*args):pass
    def max_queued_messages_set(self,n):self.queue_limit=n
    def max_inflight_messages_set(self,n):self.inflight_limit=n
    def reconnect_delay_set(self,**kwargs):pass
    def connect_async(self,*args,**kwargs):pass
    def loop_start(self):self.on_connect(self,None,None,types.SimpleNamespace(is_failure=False),None)
    def disconnect(self):self.on_disconnect(self,None,None,None,None)
    def loop_stop(self):pass
    def publish(self,topic,payload,qos,retain):
        self.mid+=1
        self.records.append((topic,json.loads(payload),qos,retain,self.mid,threading.get_ident()))
        rc=self.result_rc if qos==1 else 0
        if self.auto_ack and rc==0:self.ack(self.mid)
        return types.SimpleNamespace(mid=self.mid,rc=rc)
    def ack(self,mid,failed=False):
        self.on_publish(self,None,mid,types.SimpleNamespace(is_failure=failed),None)


class VisionSiotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'monitor.json'
        self.config=dict(host='localhost',port=1883,username='fixture',password='SECRET-fixture')
        self.path.write_text(json.dumps(self.config),encoding='utf-8')
        self.clients=[]
        def factory():
            client=Client();self.clients.append(client);return client
        self.bridge=module.VisionSiotBridge(str(self.path),factory=factory)
        self.addCleanup(self.bridge.stop)
        image=io.BytesIO();Image.new('RGB',(400,300),'orange').save(image,'JPEG');self.jpeg=image.getvalue()

    def test_original_jpeg_and_recognition_are_paired_without_modification(self):
        self.bridge.start();caller=threading.get_ident()
        capture=self.bridge.begin_capture(self.jpeg,'画面里有什么？')
        self.assertTrue(wait_for(lambda:len(self.clients[0].records)==1))
        self.bridge.complete_capture(capture,'橙色画面\n第二行完整文字')
        self.assertTrue(wait_for(lambda:self.bridge.snapshot(capture)['result_status']=='broker_ack'))
        records=self.clients[0].records
        self.assertEqual(len(records),3)
        initial,update,result=[x[1] for x in records]
        self.assertEqual(initial['capture_id'],capture)
        self.assertEqual(update['capture_id'],capture);self.assertEqual(result['capture_id'],capture)
        for packet in (initial,update):
            self.assertEqual(base64.b64decode(packet['data']),self.jpeg)
            self.assertEqual((packet['width'],packet['height']),(400,300))
            self.assertEqual(packet['question'],'画面里有什么？')
        self.assertEqual(initial['timestamp'],update['timestamp'])
        self.assertEqual(update['recognition'],'橙色画面\n第二行完整文字')
        self.assertEqual(result['text'],update['recognition'])
        self.assertEqual([r[0] for r in records],['fixture/vision/image','fixture/vision/image','fixture/vision/result'])
        self.assertEqual([r[2] for r in records],[0,0,1])
        self.assertTrue(all(not r[3] and r[5]!=caller for r in records))
        state=json.dumps(self.bridge.snapshot(capture))
        self.assertNotIn('SECRET',state);self.assertNotIn('橙色',state);self.assertNotIn('data',state)

    def test_missing_config_does_not_raise_or_block_capture(self):
        self.path.unlink();self.bridge.start()
        started=time.monotonic();capture=self.bridge.begin_capture(self.jpeg,'question')
        self.bridge.complete_capture(capture,'answer')
        self.assertLess(time.monotonic()-started,.1)
        self.assertTrue(capture);self.assertEqual(self.clients,[])
        self.assertFalse(self.bridge.snapshot()['configured'])
        self.assertEqual(self.bridge.snapshot()['cached_images'],0)
        self.assertEqual(self.bridge.snapshot(capture)['status'],'complete')

    def test_offline_capture_is_explicitly_dropped_and_not_resent(self):
        self.bridge.start();client=self.clients[0];client.disconnect()
        capture=self.bridge.begin_capture(self.jpeg,'question');self.bridge.complete_capture(capture,'answer')
        self.assertEqual(self.bridge.snapshot(capture)['result_status'],'offline')
        client.loop_start();time.sleep(.05)
        self.assertEqual(client.records,[])

    def test_queues_and_original_image_cache_are_bounded(self):
        self.bridge.start();client=self.clients[0];client.auto_ack=False
        first=self.bridge.begin_capture(self.jpeg,'first')
        self.assertTrue(wait_for(lambda:len(client.records)==1))
        for number in range(25):
            capture=self.bridge.begin_capture(self.jpeg,str(number))
            self.bridge.complete_capture(capture,'answer')
        state=self.bridge.snapshot()
        self.assertLessEqual(state['pending'],8);self.assertLessEqual(state['cached_images'],4)
        self.assertLessEqual(state['capture_count'],16);self.assertGreater(state['dropped'],0)
        self.assertEqual(client.queue_limit,8);self.assertEqual(client.inflight_limit,1)
        self.assertEqual(len(client.records),1)

    def test_oversized_jpeg_and_text_report_error_without_truncation(self):
        self.bridge.start()
        capture=self.bridge.begin_capture(b'\xff\xd8'+bytes(2*1024*1024)+b'\xff\xd9','question')
        self.assertEqual(self.bridge.snapshot(capture)['image_status'],'error')
        valid=self.bridge.begin_capture(self.jpeg,'question')
        self.assertTrue(wait_for(lambda:len(self.clients[0].records)==1))
        self.bridge.complete_capture(valid,'文'*(256*1024//3+1))
        self.assertEqual(self.bridge.snapshot(valid)['result_status'],'error')
        time.sleep(.05);self.assertEqual(len(self.clients[0].records),1)

    def test_invalid_jpeg_dimensions_are_checked_on_worker(self):
        self.bridge.start();capture=self.bridge.begin_capture(b'\xff\xd8not-a-jpeg\xff\xd9','question')
        self.assertTrue(wait_for(lambda:self.bridge.snapshot(capture)['image_status']=='error'))
        self.assertEqual(self.clients[0].records,[])

    def test_qos1_no_connection_race_remains_pending_until_ack(self):
        self.bridge.start();client=self.clients[0];client.result_rc=4
        capture=self.bridge.begin_capture(self.jpeg,'question');self.bridge.complete_capture(capture,'answer')
        self.assertTrue(wait_for(lambda:self.bridge.snapshot(capture)['result_status']=='pending_reconnect'))
        self.assertEqual(len(client.records),3)
        client.ack(client.records[-1][4])
        self.assertEqual(self.bridge.snapshot(capture)['result_status'],'broker_ack')
        self.bridge.complete_capture(capture,'answer')
        time.sleep(.05);self.assertEqual(len(client.records),3)

    def test_stop_restart_ignores_old_callbacks_and_clears_photos(self):
        self.bridge.start();self.bridge.start();self.assertEqual(len(self.clients),1)
        old=self.clients[0];old.auto_ack=False
        capture=self.bridge.begin_capture(self.jpeg,'question')
        self.assertTrue(wait_for(lambda:len(old.records)==1))
        started=time.monotonic();self.bridge.stop();self.assertLess(time.monotonic()-started,.2)
        self.assertEqual(self.bridge.snapshot()['cached_images'],0)
        self.bridge.start();old.disconnect();old.ack(1)
        self.assertTrue(self.bridge.snapshot()['connected'])
        newer=self.bridge.begin_capture(self.jpeg,'new')
        self.assertTrue(wait_for(lambda:self.bridge.snapshot(newer)['image_status']=='local_sent'))

    def test_invalid_topic_and_sensitive_start_error_are_sanitized(self):
        self.config['vision_image_topic']='bad/#';self.path.write_text(json.dumps(self.config))
        self.bridge.start();self.assertFalse(self.bridge.snapshot()['configured'])
        self.assertEqual(self.clients,[])
        bad=module.VisionSiotBridge(str(self.path),factory=lambda:(_ for _ in ()).throw(RuntimeError('SECRET-fixture')))
        self.addCleanup(bad.stop)
        del self.config['vision_image_topic'];self.path.write_text(json.dumps(self.config))
        bad.start();capture=bad.begin_capture(self.jpeg,'private question')
        bad.complete_capture(capture,'',error=RuntimeError('SECRET-fixture'))
        self.assertNotIn('SECRET',json.dumps(bad.snapshot(capture)))
        self.assertNotIn('SECRET',json.dumps(bad.snapshot()))

    def test_qos0_disconnect_before_publish_returns_does_not_stall_queue(self):
        self.bridge.start();client=self.clients[0];client.auto_ack=False
        publish=client.publish
        def raced_publish(*args,**kwargs):
            info=publish(*args,**kwargs)
            client.disconnect()
            return info
        client.publish=raced_publish
        capture=self.bridge.begin_capture(self.jpeg,'question')
        self.assertTrue(wait_for(lambda:len(client.records)==1))
        self.assertTrue(wait_for(lambda:self.bridge.snapshot(capture)['image_status']=='unconfirmed'))
        self.assertEqual(self.bridge.snapshot()['awaiting_ack'],0)
        client.publish=publish;client.auto_ack=True;client.loop_start()
        self.bridge.complete_capture(capture,'answer')
        self.assertTrue(wait_for(lambda:self.bridge.snapshot(capture)['result_status']=='broker_ack'))

    def test_early_qos1_ack_with_rc4_remains_final_state(self):
        self.bridge.start();client=self.clients[0];publish=client.publish
        def early_ack(*args,**kwargs):
            info=publish(*args,**kwargs)
            if kwargs['qos']==1:info.rc=4
            return info
        client.publish=early_ack
        capture=self.bridge.begin_capture(self.jpeg,'question');self.bridge.complete_capture(capture,'answer')
        self.assertTrue(wait_for(lambda:self.bridge.snapshot(capture)['result_status']=='broker_ack'))
        self.assertEqual(self.bridge.snapshot()['awaiting_ack'],0)

    def test_original_cache_expires_but_text_result_can_still_be_recorded(self):
        self.bridge.start();capture=self.bridge.begin_capture(self.jpeg,'question')
        self.assertTrue(wait_for(lambda:len(self.clients[0].records)==1))
        with self.bridge._condition:
            jpeg,created=self.bridge._images[capture]
            self.bridge._images[capture]=(jpeg,created-self.bridge.IMAGE_TTL-1)
        self.assertEqual(self.bridge.snapshot()['cached_images'],0)
        self.bridge.complete_capture(capture,'late answer')
        self.assertTrue(wait_for(lambda:self.bridge.snapshot(capture)['result_status']=='broker_ack'))
        self.assertEqual(len(self.clients[0].records),2)
        self.assertEqual(self.bridge.snapshot(capture)['image_status'],'expired')

    def test_json_expansion_over_envelope_limit_errors_without_truncation(self):
        self.bridge.start();capture=self.bridge.begin_capture(self.jpeg,'question')
        self.assertTrue(wait_for(lambda:len(self.clients[0].records)==1))
        # A JPEG with padding before EOI still opens to exactly the same pixels.
        padded=self.jpeg[:-2]+bytes(self.bridge.MAX_JPEG-len(self.jpeg))+self.jpeg[-2:]
        with self.bridge._condition:self.bridge._images[capture]=(padded,time.monotonic())
        unannotated=self.bridge._encode((capture,'image',0,None,'captured'))
        self.assertGreater(len(unannotated),2*1024*1024)
        self.assertLessEqual(len(unannotated),3*1024*1024)
        self.assertEqual(base64.b64decode(json.loads(unannotated)['data']),padded)
        self.bridge.complete_capture(capture,'\x00'*self.bridge.MAX_TEXT)
        self.assertTrue(wait_for(lambda:self.bridge.snapshot(capture)['result_status']=='broker_ack'))
        self.assertEqual(self.bridge.snapshot(capture)['image_status'],'error')
        self.assertEqual(self.clients[0].records[-1][1]['text'],'\x00'*self.bridge.MAX_TEXT)
        self.assertEqual(len(self.clients[0].records),2)

    def test_configured_specific_topic_overrides_are_used(self):
        self.config.update(vision_image_topic='lab/photo',vision_result_topic='lab/text')
        self.path.write_text(json.dumps(self.config));self.bridge.start()
        capture=self.bridge.begin_capture(self.jpeg,'question');self.bridge.complete_capture(capture,'answer')
        self.assertTrue(wait_for(lambda:self.bridge.snapshot(capture)['result_status']=='broker_ack'))
        self.assertEqual([r[0] for r in self.clients[0].records],['lab/photo','lab/photo','lab/text'])


if __name__=='__main__':unittest.main()
