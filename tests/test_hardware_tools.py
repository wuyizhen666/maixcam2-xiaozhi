import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'xiaozhi' / 'service'))
try:
    hw = importlib.import_module('hardware_tools')
except ModuleNotFoundError:
    hw = None


class Response:
    status_code = 200
    def __init__(self, body=b'{"text":"a red cup"}'):
        self.body = body
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, chunk_size): yield self.body


class PWM:
    def __init__(self): self.value = 0; self.on = True
    def duty(self, value=-1):
        if value >= 0: self.value = value
        return self.value
    def is_enabled(self): return self.on
    def enable(self): self.on = True; return 0
    def disable(self): self.on = False; return 0


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(hw, 'camera/light hardware implementation is missing')
        self.h = hw.HardwareTools(lambda: 'device-id', lambda: 'client-id')

    def configure(self):
        self.h.configure_vision({'url':'https://vision.example/analyze','token':'secret-token'})

    def test_no_upload_yet_is_unknown_instead_of_failure(self):
        self.assertIsNone(self.h.camera_status()['last_upload_ok'])

    def test_missing_vision_does_not_capture(self):
        with patch.object(hw, '_capture_jpeg') as capture:
            with self.assertRaisesRegex(RuntimeError, 'vision|视觉'):
                self.h.take_photo('What is here?')
            capture.assert_not_called()

    def test_multipart_upload_and_text_result(self):
        self.configure()
        def post(url, **kwargs):
            self.assertEqual(url, 'https://vision.example/analyze')
            self.assertEqual(kwargs['data'], {'question':'What is here?'})
            self.assertEqual(kwargs['files'], {'file':('camera.jpg', b'\xff\xd8photo\xff\xd9','image/jpeg')})
            self.assertEqual(kwargs['headers']['Authorization'], 'Bearer secret-token')
            self.assertEqual(kwargs['headers']['Device-Id'], 'device-id')
            self.assertEqual(kwargs['headers']['Client-Id'], 'client-id')
            self.assertFalse(kwargs['allow_redirects'])
            self.assertNotEqual(kwargs.get('verify'), False)
            self.assertTrue(kwargs['timeout'])
            return Response()
        with patch.object(hw, '_capture_jpeg', return_value=b'\xff\xd8photo\xff\xd9'), patch.object(hw.requests, 'post', side_effect=post):
            self.assertEqual(self.h.take_photo('What is here?'), '{"text":"a red cup"}')
        status = self.h.camera_status()
        self.assertTrue(status['vision_configured'])
        self.assertEqual(status['last_capture_bytes'], 9)
        self.assertNotIn('secret-token', json.dumps(status))

    def test_camera_failure_does_not_upload(self):
        self.configure()
        with patch.object(hw, '_capture_jpeg', side_effect=RuntimeError('camera unavailable')), patch.object(hw.requests, 'post') as post:
            with self.assertRaises(RuntimeError): self.h.take_photo('question')
            post.assert_not_called()

    def test_photo_callback_gets_uploaded_jpeg_before_network_reply(self):
        self.configure(); events = []
        jpeg = b'\xff\xd8photo\xff\xd9'
        def post(url, **kwargs):
            self.assertEqual(events, [('preview', jpeg)])
            self.assertEqual(kwargs['files']['file'][1], jpeg)
            return Response()
        with patch.object(hw, '_capture_jpeg', return_value=jpeg), patch.object(hw.requests, 'post', side_effect=post):
            self.h.take_photo('question', on_capture=lambda image: events.append(('preview', image)))

    def test_session_reset_clears_credentials_before_capture(self):
        self.configure(); self.h.configure_vision(None)
        self.assertFalse(self.h.camera_status()['vision_configured'])
        with self.assertRaises(RuntimeError): self.h.take_photo('question')

    def test_reset_during_capture_prevents_old_upload(self):
        self.configure()
        def capture():
            self.h.configure_vision(None)
            return b'\xff\xd8photo\xff\xd9'
        with patch.object(hw, '_capture_jpeg', side_effect=capture), patch.object(hw.requests, 'post') as post:
            with self.assertRaises(RuntimeError): self.h.take_photo('question')
            post.assert_not_called()

    def test_invalid_or_oversized_image_is_not_uploaded(self):
        self.configure()
        for data in [b'not jpeg', b'\xff\xd8'+b'x'*(2*1024*1024)+b'\xff\xd9']:
            with self.subTest(size=len(data)), patch.object(hw, '_capture_jpeg', return_value=data), patch.object(hw.requests, 'post') as post:
                with self.assertRaises(RuntimeError): self.h.take_photo('question')
                post.assert_not_called()

    def test_http_failures_do_not_return_success_or_credentials(self):
        self.configure()
        response=Response();response.status_code=403
        with patch.object(hw, '_capture_jpeg', return_value=b'\xff\xd8photo\xff\xd9'), patch.object(hw.requests,'post',return_value=response):
            with self.assertRaisesRegex(RuntimeError,'403'):self.h.take_photo('question')
        with patch.object(hw, '_capture_jpeg', return_value=b'\xff\xd8photo\xff\xd9'), patch.object(hw.requests,'post',side_effect=hw.requests.Timeout('secret-token https://private')):
            with self.assertRaises(RuntimeError) as exc:self.h.take_photo('question')
            self.assertNotIn('secret-token',str(exc.exception))

    def test_empty_and_oversized_vision_answers_fail(self):
        self.configure()
        for body in [b'',b'x'*(256*1024+1)]:
            with self.subTest(size=len(body)),patch.object(hw,'_capture_jpeg',return_value=b'\xff\xd8photo\xff\xd9'),patch.object(hw.requests,'post',return_value=Response(body)):
                with self.assertRaises(RuntimeError):self.h.take_photo('question')

    def test_camera_is_closed_when_a_frame_read_fails(self):
        class Camera:
            closed=False
            def exp_mode(self, value): return value
            def awb_mode(self, value): return value
            def read(self,**kwargs):raise RuntimeError('sensor failed')
            def close(self):self.closed=True
        cam=Camera()
        maix=types.ModuleType('maix');maix.camera=types.SimpleNamespace(
            Camera=lambda *args:cam, AeMode=types.SimpleNamespace(Auto=0),
            AwbMode=types.SimpleNamespace(Auto=0))
        with patch.dict(sys.modules,{'maix':maix}):
            with self.assertRaises(RuntimeError):hw._capture_jpeg()
        self.assertTrue(cam.closed)

    def capture_with_clock(self, step):
        clock = types.SimpleNamespace(now=0.)
        class Frame:
            def __init__(self, number): self.number = number
            def save(self, path, quality):
                data = json.dumps({'frame':self.number,'elapsed':clock.now}).encode()
                Path(path).write_bytes(b'\xff\xd8'+data+b'\xff\xd9')
                return 0
        class Camera:
            def __init__(self): self.closed=False;self.count=0;self.modes={};self.timeouts=[]
            def exp_mode(self, value): self.modes['ae']=value;return value
            def awb_mode(self, value): self.modes['awb']=value;return value
            def read(self, **kwargs):
                self.count+=1;self.timeouts.append(kwargs['block_ms']);clock.now+=step
                return Frame(self.count)
            def close(self): self.closed=True
        cam=Camera();maix=types.ModuleType('maix')
        maix.camera=types.SimpleNamespace(Camera=lambda *args:cam,
            AeMode=types.SimpleNamespace(Auto='auto-ae'),AwbMode=types.SimpleNamespace(Auto='auto-awb'))
        return clock,cam,maix

    def test_camera_explicitly_enables_auto_exposure_and_white_balance(self):
        clock,cam,maix=self.capture_with_clock(.02)
        with patch.dict(sys.modules,{'maix':maix}),patch.object(time,'monotonic',side_effect=lambda:clock.now):
            hw._capture_jpeg()
        self.assertEqual(cam.modes,{'ae':'auto-ae','awb':'auto-awb'})

    def test_fast_camera_waits_two_seconds_before_saving(self):
        clock,cam,maix=self.capture_with_clock(.005)
        with patch.dict(sys.modules,{'maix':maix}),patch.object(time,'monotonic',side_effect=lambda:clock.now):
            data=hw._capture_jpeg()
        saved=json.loads(data[2:-2])
        self.assertGreaterEqual(saved['elapsed'],2.0)
        self.assertGreaterEqual(saved['frame'],30)
        self.assertTrue(cam.closed)

    def test_slow_camera_still_requires_thirty_frames(self):
        clock,cam,maix=self.capture_with_clock(.1)
        with patch.dict(sys.modules,{'maix':maix}),patch.object(time,'monotonic',side_effect=lambda:clock.now):
            data=hw._capture_jpeg()
        saved=json.loads(data[2:-2])
        self.assertGreaterEqual(saved['frame'],30)
        self.assertTrue(all(0 < value <= 3000 for value in cam.timeouts))

    def test_warmup_budget_expires_and_closes_camera(self):
        clock,cam,maix=self.capture_with_clock(1.)
        with patch.dict(sys.modules,{'maix':maix}),patch.object(time,'monotonic',side_effect=lambda:clock.now):
            with self.assertRaisesRegex(RuntimeError,'warm-up|预热'):hw._capture_jpeg()
        self.assertLessEqual(clock.now,5.1)
        self.assertTrue(cam.closed)

    def test_concurrent_photo_is_rejected_without_second_capture(self):
        self.configure();entered=threading.Event();release=threading.Event();errors=[]
        def capture():entered.set();release.wait(2);return b'\xff\xd8photo\xff\xd9'
        def first():
            try:self.h.take_photo('first')
            except Exception as exc:errors.append(exc)
        with patch.object(hw,'_capture_jpeg',side_effect=capture),patch.object(hw.requests,'post',return_value=Response()):
            worker=threading.Thread(target=first);worker.start()
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaisesRegex(RuntimeError,'busy'):self.h.take_photo('second')
            finally:release.set();worker.join(2)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[])

    def test_rejects_bad_vision_urls_and_questions(self):
        for url in ['http://insecure.example','file:///tmp/picture','https://user:pass@example.org']:
            self.h.configure_vision({'url':url})
            self.assertFalse(self.h.camera_status()['vision_configured'])
        self.configure()
        for question in ['', ' '*10, 'x'*2001, True, 12]:
            with self.subTest(question=repr(question)[:20]), self.assertRaises(ValueError):self.h.take_photo(question)

    def test_verified_official_http_endpoint_is_upgraded_without_changing_path(self):
        self.h.configure_vision({'url':'http://api.xiaozhi.me:80/provided-path?q=1','token':'secret'})
        self.assertTrue(self.h.camera_status()['vision_configured'])
        def post(url,**kwargs):
            self.assertEqual(url,'https://api.xiaozhi.me/provided-path?q=1')
            self.assertEqual(kwargs['headers']['Authorization'],'Bearer secret')
            self.assertNotEqual(kwargs.get('verify'),False)
            return Response()
        with patch.object(hw,'_capture_jpeg',return_value=b'\xff\xd8photo\xff\xd9'),patch.object(hw.requests,'post',side_effect=post):
            self.h.take_photo('question')

    def test_http_200_with_api_failure_is_an_error(self):
        self.configure()
        with patch.object(hw,'_capture_jpeg',return_value=b'\xff\xd8photo\xff\xd9'),patch.object(hw.requests,'post',return_value=Response(b'{"success":false,"error":"unavailable"}')):
            with self.assertRaises(RuntimeError):self.h.take_photo('question')

    def test_light_power_restores_last_brightness_and_shutdown_is_off(self):
        pwm=PWM()
        with patch.object(hw,'_create_pwm',return_value=pwm), patch.object(hw,'force_light_off'):
            self.assertEqual(self.h.set_light_power(True)['brightness'],25)
            self.assertEqual(self.h.set_light_brightness(60)['brightness'],60)
            self.assertFalse(self.h.set_light_power(False)['enabled'])
            self.assertEqual(self.h.set_light_power(True)['brightness'],60)
            self.h.close()
            self.assertEqual(pwm.value,0)
            self.assertFalse(pwm.on)

    def test_invalid_brightness_cannot_create_hardware(self):
        with patch.object(hw,'_create_pwm') as create:
            for value in [-1,101,3.5,'20',True,None]:
                with self.subTest(value=value),self.assertRaises(ValueError):self.h.set_light_brightness(value)
            for value in ['false',0,1,None]:
                with self.subTest(value=value),self.assertRaises(ValueError):self.h.set_light_power(value)
            create.assert_not_called()

    def test_light_readback_failure_is_not_reported_as_success(self):
        pwm=PWM()
        def duty(value=-1):return -1
        pwm.duty=duty
        with patch.object(hw,'_create_pwm',return_value=pwm),patch.object(hw,'force_light_off'):
            with self.assertRaises(RuntimeError):self.h.set_light_brightness(30)

    def test_stop_cleanup_respects_ownership_and_turns_off_pwm(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); marker=root/'owned'; channel=root/'pwm2';channel.mkdir()
            for name,value in [('duty_cycle','2500'),('enable','1')]: (channel/name).write_text(value)
            original_write=Path.write_text
            def kernel_write(path,text,*args,**kwargs):
                result=original_write(path,text,*args,**kwargs)
                if path==root/'unexport':
                    self.assertEqual(text,'2')
                    self.assertEqual((channel/'duty_cycle').read_text(),'0')
                    self.assertEqual((channel/'enable').read_text(),'0')
                    for child in channel.iterdir():child.unlink()
                    channel.rmdir()
                return result
            with patch.object(hw,'LIGHT_OWNER',marker),patch.object(hw,'PWM_CHANNEL',channel):
                hw.force_light_off()
                self.assertEqual((channel/'enable').read_text(),'1')
                marker.write_text('test')
                with patch.object(Path,'write_text',kernel_write):hw.force_light_off()
                self.assertFalse(channel.exists(),'crash cleanup must release the exported channel')
                self.assertFalse(marker.exists())
                maix=types.ModuleType('maix')
                maix.pinmap=types.SimpleNamespace(set_pin_function=lambda *args:0)
                maix.pwm=types.SimpleNamespace(PWM=lambda *args,**kwargs:PWM())
                with patch.dict(sys.modules,{'maix':maix}):self.assertIsInstance(hw._create_pwm(),PWM)

    def test_failed_unexport_keeps_ownership_for_retry(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);marker=root/'owned';marker.write_text('test')
            channel=root/'pwm2';channel.mkdir()
            for name in ['duty_cycle','enable']:(channel/name).write_text('1')
            original_write=Path.write_text
            def fail_unexport(path,text,*args,**kwargs):
                if path==root/'unexport':raise OSError('kernel busy')
                return original_write(path,text,*args,**kwargs)
            with patch.object(hw,'LIGHT_OWNER',marker),patch.object(hw,'PWM_CHANNEL',channel),patch.object(Path,'write_text',fail_unexport):
                with self.assertRaises(OSError):hw.force_light_off()
                self.assertTrue(marker.exists())


if __name__ == '__main__': unittest.main()
