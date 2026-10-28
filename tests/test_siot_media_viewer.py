import base64
import importlib.util
import io
import json
from pathlib import Path
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from PIL import Image

PATH=Path(__file__).resolve().parents[1]/'apps/siot_monitor/media_viewer.py'
spec=importlib.util.spec_from_file_location('siot_media_viewer',PATH)
media=importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)


def picture(fmt='JPEG',size=(640,480),color='red'):
    out=io.BytesIO();Image.new('RGB',size,color).save(out,format=fmt)
    return out.getvalue()


def image_message(raw=None,**metadata):
    raw=picture() if raw is None else raw
    return json.dumps(dict(type='image',mime='image/jpeg',encoding='base64',
                           data=base64.b64encode(raw).decode(),**metadata),ensure_ascii=False).encode('utf-8')


def wait_for(viewer,predicate,timeout=8):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=viewer.snapshot()
        if predicate(result):return result
        time.sleep(.02)
    raise AssertionError('Media state did not arrive: '+repr(viewer.snapshot()))


class PayloadTests(unittest.TestCase):
    def test_raw_jpeg_and_png_fit_without_distortion(self):
        for fmt in ['JPEG','PNG']:
            result=media.decode_payload(picture(fmt))
            self.assertEqual(result.mode,'RGB')
            self.assertEqual(result.size,(373,280))

    def test_data_url_and_json_envelope(self):
        raw=picture('PNG',size=(80,40));encoded=base64.b64encode(raw).decode()
        for payload in [('data:image/png;base64,'+encoded).encode(),
                        json.dumps({'type':'image','mime':'image/png','encoding':'base64',
                                    'data':encoded,'sequence':3,'timestamp':123,'width':80,'height':40}).encode()]:
            self.assertEqual(media.decode_payload(payload).size,(80,40))

    def test_plain_base64_jpeg_and_png_allow_only_outer_whitespace(self):
        for fmt in ['JPEG','PNG']:
            raw=picture(fmt,size=(80,40));encoded=base64.b64encode(raw)
            for payload in [encoded,b' \t\r\n'+encoded+b'\r\n ']:
                result=media.decode_payload(payload)
                self.assertEqual(result.mode,'RGB');self.assertEqual(result.size,(80,40))
            with self.assertRaises(ValueError):media.decode_payload(encoded[:12]+b'\n'+encoded[12:])

    def test_plain_base64_rejects_text_bad_magic_truncation_and_size_limits(self):
        raw=picture()
        invalid=[b'1234',b'hello world',b'////!!!!',base64.b64encode(b'not an image'),
                 base64.b64encode(raw[:-10]),
                 base64.b64encode(raw+b'\0'*(2*1024*1024+1-len(raw))),
                 b' '* (3*1024*1024)+base64.b64encode(raw)]
        for payload in invalid:
            with self.subTest(size=len(payload)):
                with self.assertRaises(ValueError):media.decode_payload(payload)

    def test_plain_base64_png_crc_is_verified(self):
        raw=bytearray(picture('PNG',size=(80,40)))
        at=raw.index(b'IDAT');length=int.from_bytes(raw[at-4:at],'big')
        raw[at+4+length]^=1
        with self.assertRaises(ValueError):media.decode_payload(base64.b64encode(raw))

    def test_malformed_unsupported_oversized_and_pixel_bombs_rejected(self):
        invalid=[b'not an image',b'GIF89a',b'x'*(media.MAX_IMAGE_BYTES+1),
                 b'data:text/html;base64,AAAA',b'data:image/png;base64,!!!',
                 json.dumps({'type':'video','mime':'image/jpeg','data':'AAAA'}).encode(),
                 picture('PNG',(2100,2000))]
        for value in invalid:
            with self.subTest(size=len(value)):
                with self.assertRaises(ValueError):media.decode_payload(value)

    def test_declared_mime_must_match_decoded_format(self):
        raw=base64.b64encode(picture('PNG')).decode()
        with self.assertRaises(ValueError):media.decode_payload(('data:image/jpeg;base64,'+raw).encode())

    def test_url_validation_only_accepts_supported_network_schemes(self):
        for url in ['http://127.0.0.1:8080/frame.jpg','https://camera.local/live','rtsp://user:pass@192.168.1.2/live']:
            self.assertEqual(media.validate_url(url),url)
        for url in ['file:///etc/passwd','ftp://host/file','http://','http://host:abc/a','http://host/a\nheader',
                    'rtsp://host/\x00','http://host/'+'x'*4096]:
            with self.assertRaises(ValueError):media.validate_url(url)

    def test_jpeg_parser_handles_split_markers_and_rejects_unbounded_frame(self):
        raw=picture();parser=media.JpegBuffer()
        frames=[]
        for offset in range(0,len(raw),7):frames.extend(parser.feed(raw[offset:offset+7]))
        self.assertEqual(frames,[raw])
        with self.assertRaises(ValueError):media.JpegBuffer().feed(b'\xff\xd8'+b'x'*media.MAX_IMAGE_BYTES)

    def test_redirect_rejects_https_downgrade_and_strips_cross_origin_auth(self):
        handler=media._SafeRedirect()
        request=urllib.request.Request('https://camera.example/private',headers={'Authorization':'Basic secret'})
        with self.assertRaises(ValueError):
            handler.redirect_request(request,None,302,'Found',{},'http://camera.example/private')
        for url in ['https://other.example/private','https://camera.example:444/private']:
            redirected=handler.redirect_request(request,None,302,'Found',{},url)
            self.assertIsNone(redirected.get_header('Authorization'))
        same=handler.redirect_request(request,None,302,'Found',{},'https://camera.example:443/new')
        self.assertEqual(same.get_header('Authorization'),'Basic secret')

    def test_two_mib_original_image_fits_three_mib_envelope_without_reencoding(self):
        original=picture();original+=b'\0'*(2*1024*1024-len(original))
        payload=image_message(original,capture_id='capture-1',recognition='x'*(256*1024))
        self.assertGreater(len(payload),2*1024*1024)
        self.assertLessEqual(len(payload),3*1024*1024)
        self.assertEqual(media.decode_payload(payload).size,(373,280))
        self.assertEqual(base64.b64decode(json.loads(payload)['data']),original)
        with self.assertRaises(ValueError):media.decode_payload(image_message(original+b'\0'))
        with self.assertRaises(ValueError):media.decode_payload(payload+b' '*(3*1024*1024+1-len(payload)))

    def test_recognition_and_optional_identifier_fields_have_utf8_limits(self):
        bad=[{'recognition':['not text']},{'recognition':'中'*87382},
             {'capture_id':12},{'capture_id':'x'*129},{'capture_id':'bad\nline'},
             {'question':False},{'question':'x'*8193}]
        for metadata in bad:
            with self.subTest(fields=list(metadata)):
                with self.assertRaises(ValueError):media.decode_payload(image_message(**metadata))
        self.assertIsInstance(media.decode_payload(image_message(capture_id='相片-1',question='这是什么？',recognition='中'*87381)),Image.Image)


class TopicTests(unittest.TestCase):
    def setUp(self):
        self.viewer=media.MediaViewer(stale_after=.2)
        self.addCleanup(self.viewer.stop)

    def test_topic_selection_ignores_other_topic_and_stop_clears_frame(self):
        self.viewer.start_topic('alice/image')
        self.viewer.submit_topic('other/image',picture())
        self.assertIsNone(self.viewer.snapshot()['frame'])
        self.viewer.submit_topic('alice/image',picture())
        state=wait_for(self.viewer,lambda s:s['frame'] is not None)
        self.assertEqual(state['status'],'image');self.assertEqual(state['fps'],0)
        self.assertGreater(state['received_at'],0)
        self.viewer.stop()
        self.assertEqual(self.viewer.snapshot()['status'],'stopped')
        self.assertIsNone(self.viewer.snapshot()['frame'])
        self.assertFalse(any(t.is_alive() for t in self.viewer._threads))

    def test_continuous_topic_stales_instead_of_claiming_live(self):
        # Allow ARM scheduling/decoding time; inspect the returned fresh state
        # before deliberately waiting for the independently checked expiry.
        self.viewer.stale_after=1.
        frames=[picture(color=color) for color in ['red','blue']]
        self.viewer.start_topic('alice/image')
        for index,frame in enumerate(frames):
            before=self.viewer.snapshot()['sequence']
            self.viewer.submit_topic('alice/image',frame)
            fresh=wait_for(self.viewer,lambda s:s['sequence']>before and s['frame'] is not None)
            if index==0:time.sleep(.12)
        self.assertGreater(fresh['fps'],0)
        state=wait_for(self.viewer,lambda s:s['status']=='stale')
        self.assertEqual(state['fps'],0);self.assertIsNotNone(state['frame'])

    def test_single_topic_image_also_ages_without_a_second_frame(self):
        self.viewer.start_topic('alice/image');self.viewer.submit_topic('alice/image',picture())
        first=wait_for(self.viewer,lambda s:s['frame'] is not None)
        stale=wait_for(self.viewer,lambda s:s['status']=='stale')
        self.assertEqual(stale['received_at'],first['received_at'])
        self.assertEqual(stale['fps'],0)

    def test_invalid_payload_returns_safe_error(self):
        self.viewer.start_topic('alice/image');self.viewer.submit_topic('alice/image',b'secret-invalid-data')
        state=wait_for(self.viewer,lambda s:s['status']=='error')
        self.assertNotIn('secret',state['error']);self.assertIsNone(state['frame'])

    def test_switch_generation_does_not_accept_previous_decoder_result(self):
        entered=threading.Event();release=threading.Event();original=media._decode_frame
        def delayed(payload):
            entered.set();release.wait(2);return original(payload)
        with patch.object(media,'_decode_frame',side_effect=delayed):
            self.viewer.start_topic('old');self.viewer.submit_topic('old',picture())
            self.assertTrue(entered.wait(1))
            stopper=threading.Thread(target=self.viewer.stop);stopper.start()
            time.sleep(.03);release.set();stopper.join(2)
        self.viewer.start_topic('new');time.sleep(.05)
        self.assertIsNone(self.viewer.snapshot()['frame'])

    def test_latest_only_queue_does_not_grow_and_stop_releases_pending_bytes(self):
        self.viewer.start_topic('alice/image')
        raw=picture()
        for _ in range(100):self.viewer.submit_topic('alice/image',raw)
        self.assertLessEqual(self.viewer._queue.qsize(),1)
        self.viewer.stop();self.assertEqual(self.viewer._queue.qsize(),0)

    def test_old_cached_picture_keeps_receive_time_and_is_stale_immediately(self):
        received=time.time()-60
        self.viewer.start_topic('alice/image')
        self.viewer.submit_topic('alice/image',picture(),received_at=received)
        state=wait_for(self.viewer,lambda s:s['frame'] is not None)
        self.assertEqual(state['received_at'],received)
        self.assertEqual(state['status'],'stale');self.assertEqual(state['fps'],0)
        self.assertGreater(time.monotonic()-self.viewer._last_frame,59)
        self.viewer.stop();self.viewer.start_topic('alice/image')
        self.viewer.submit_topic('alice/image',picture(),received_at=received)
        state=wait_for(self.viewer,lambda s:s['frame'] is not None)
        self.assertEqual(state['received_at'],received);self.assertEqual(state['status'],'stale')

    def test_invalid_receive_times_are_rejected_and_future_is_clamped(self):
        self.viewer.start_topic('alice/image')
        for received in [True,'123',-1,float('inf'),float('nan'),10**10000]:
            self.viewer.submit_topic('alice/image',picture(),received_at=received)
            self.assertEqual(self.viewer.snapshot()['status'],'error')
            self.assertIsNone(self.viewer.snapshot()['frame'])
        before=time.time()
        self.viewer.submit_topic('alice/image',picture(),received_at=before+10000)
        state=wait_for(self.viewer,lambda s:s['frame'] is not None)
        self.assertGreaterEqual(state['received_at'],before)
        self.assertLessEqual(state['received_at'],time.time())

    def test_decoding_delay_does_not_make_queued_topic_frame_fresh(self):
        original=media._decode_frame
        def slow(raw):time.sleep(.25);return original(raw)
        self.viewer.start_topic('alice/image');received=time.time()
        with patch.object(media,'_decode_frame',side_effect=slow):
            self.viewer.submit_topic('alice/image',picture(),received_at=received)
            state=wait_for(self.viewer,lambda s:s['frame'] is not None)
        self.assertEqual(state['status'],'stale');self.assertEqual(state['received_at'],received)

    def test_capture_metadata_updates_same_image_and_never_leaks_to_next_frame(self):
        self.viewer.start_topic('alice/image')
        self.assertIsNone(self.viewer.snapshot()['recognition'])
        for payload,identifier,text in [
            (image_message(capture_id='one',question='private question'),'one',None),
            (image_message(capture_id='one',recognition='识别结果'),'one','识别结果'),
            (image_message(capture_id='two'),'two',None),
            (image_message(capture_id='two',recognition='第二帧'),'two','第二帧'),
            (picture(),None,None),
            (image_message(recognition='无编号结果'),None,'无编号结果'),
            (image_message(),None,None)]:
            sequence=self.viewer.snapshot()['sequence']
            self.viewer.submit_topic('alice/image',payload)
            state=wait_for(self.viewer,lambda s:s['sequence']>sequence and s['frame'] is not None)
            self.assertEqual(state['capture_id'],identifier)
            self.assertEqual(state['recognition'],text)
            self.assertNotIn('question',state)
        self.viewer.stop()
        self.assertIsNone(self.viewer.snapshot()['recognition']);self.assertIsNone(self.viewer.snapshot()['capture_id'])

    def test_topic_queue_accepts_envelope_larger_than_raw_image_limit(self):
        original=picture();original+=b'\0'*(2*1024*1024-len(original))
        self.viewer.start_topic('alice/image')
        self.viewer.submit_topic('alice/image',image_message(original,capture_id='large',recognition='ok'))
        state=wait_for(self.viewer,lambda s:s['frame'] is not None)
        self.assertEqual(state['capture_id'],'large');self.assertEqual(state['recognition'],'ok')

    def test_plain_base64_clears_previous_json_capture_and_recognition(self):
        self.viewer.start_topic('alice/image')
        self.viewer.submit_topic('alice/image',image_message(capture_id='old',recognition='旧识别'))
        before=wait_for(self.viewer,lambda s:s.get('recognition')=='旧识别')
        self.viewer.submit_topic('alice/image',base64.b64encode(picture('PNG',size=(80,40))))
        current=wait_for(self.viewer,lambda s:s['sequence']>before['sequence'] and s['frame'] is not None)
        self.assertIsNone(current['capture_id']);self.assertIsNone(current['recognition'])
        self.assertEqual(current['frame'].size,(80,40))


class Handler(BaseHTTPRequestHandler):
    jpeg=picture();png=picture('PNG',size=(80,40))
    def log_message(self,*args):pass
    def do_GET(self):
        try:
            if self.path=='/slow':time.sleep(3)
            if self.path=='/stream':
                self.send_response(200);self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame');self.end_headers()
                for _ in range(3):
                    self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'+self.jpeg+b'\r\n');self.wfile.flush();time.sleep(.15)
                return
            value=self.png if self.path=='/image' else b'not-media'
            self.send_response(200);self.send_header('Content-Type','image/png' if self.path=='/image' else 'text/plain')
            self.send_header('Content-Length',str(len(value)));self.end_headers();self.wfile.write(value)
        except (BrokenPipeError,ConnectionResetError):pass


class UrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.server.daemon_threads=True
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(2)
    def setUp(self):
        self.viewer=media.MediaViewer(timeout=5)
        self.addCleanup(self.viewer.stop)
    def test_http_static_png_has_no_fake_stream_fps(self):
        self.viewer.start_url(self.base+'/image')
        state=wait_for(self.viewer,lambda s:s['status']=='image')
        self.assertEqual(state['fps'],0);self.assertEqual(state['frame'].size,(80,40))
    def test_http_mjpeg_decodes_frames_and_reports_eof(self):
        self.viewer.start_url(self.base+'/stream')
        state=wait_for(self.viewer,lambda s:s['status']=='ended')
        self.assertIsNotNone(state['frame']);self.assertEqual(state['fps'],0)
    def test_timeout_does_not_leave_process_or_reader_threads(self):
        self.viewer.stop();self.viewer=media.MediaViewer(timeout=.5);self.addCleanup(self.viewer.stop)
        self.viewer.start_url(self.base+'/slow')
        wait_for(self.viewer,lambda s:s['status'] in ('timeout','error'))
        self.assertTrue(self.viewer._network_ready,'The timeout must test network work, not interpreter startup')
        started=time.monotonic();self.viewer.stop()
        self.assertLess(time.monotonic()-started,2.5)
        self.assertFalse(any(t.is_alive() for t in self.viewer._threads))
    def test_non_image_http_response_is_safe_error(self):
        self.viewer.start_url(self.base+'/bad?password=secret')
        state=wait_for(self.viewer,lambda s:s['status']=='error')
        self.assertNotIn('secret',state['error'])

    def test_url_source_can_restart_same_object_after_stop(self):
        self.viewer.start_url(self.base+'/image')
        wait_for(self.viewer,lambda s:s['status']=='image');self.viewer.stop()
        self.viewer.start_url(self.base+'/image')
        self.assertIsNotNone(wait_for(self.viewer,lambda s:s['status']=='image')['frame'])


class FakeProcess:
    def __init__(self,data):self.stdout=io.BytesIO(data);self.stdin=RecordingInput();self.terminated=False
    def poll(self):return 0
    def wait(self,timeout=None):return 0
    def terminate(self):self.terminated=True
    def kill(self):self.terminated=True


class RecordingInput(io.BytesIO):
    def close(self):
        self.saved=self.getvalue()
        super().close()


class RtspTests(unittest.TestCase):
    def test_http_credentials_are_sent_over_stdin_not_child_argv(self):
        viewer=media.MediaViewer();self.addCleanup(viewer.stop)
        raw=picture('PNG',size=(80,40))
        process=FakeProcess(b'S'+len(raw).to_bytes(4,'big')+raw)
        with patch.object(media.subprocess,'Popen',return_value=process) as popen:
            viewer.start_url('http://user:secret@127.0.0.1/image')
            wait_for(viewer,lambda s:s['status']=='image')
        self.assertNotIn('secret',repr(popen.call_args.args[0]))
        self.assertEqual(json.loads(process.stdin.saved)['url'],'http://user:secret@127.0.0.1/image')

    def test_python_startup_has_separate_bounded_window_before_network_timeout(self):
        viewer=media.MediaViewer(timeout=.2);self.addCleanup(viewer.stop)
        raw=picture('PNG',size=(80,40));stopped=threading.Event();ended=threading.Event()
        class DelayedPipe(io.BytesIO):
            def __init__(self,data):super().__init__(data);self.first=True
            def read(self,count=-1):
                if self.first:
                    self.first=False
                    if stopped.wait(.45):return b''
                result=super().read(count)
                if not result:ended.set()
                return result
        process=FakeProcess(b'')
        process.stdout=DelayedPipe(b'R'+bytes(4)+b'S'+len(raw).to_bytes(4,'big')+raw)
        process.poll=lambda:0 if ended.is_set() or stopped.is_set() else None
        process.terminate=stopped.set
        with patch.object(media.subprocess,'Popen',return_value=process):
            viewer.start_url('http://127.0.0.1/image')
            state=wait_for(viewer,lambda s:s['status']=='image')
        self.assertFalse(stopped.is_set())
        self.assertIsNotNone(state['frame'])
        self.assertLessEqual(viewer.startup_timeout,15)

    def test_ffmpeg_command_is_argv_only_and_stream_eof_is_not_live(self):
        viewer=media.MediaViewer();self.addCleanup(viewer.stop)
        process=FakeProcess(picture())
        with patch.object(media.shutil,'which',return_value='ffmpeg'), patch.object(media.subprocess,'Popen',return_value=process) as popen:
            viewer.start_url('rtsp://user:secret@127.0.0.1/live')
            state=wait_for(viewer,lambda s:s['status']=='ended')
        self.assertIsNotNone(state['frame']);self.assertEqual(state['fps'],0)
        self.assertNotIn('secret',state['error'])
        argv=popen.call_args.args[0]
        self.assertIn('-rtsp_transport',argv);self.assertIn('tcp',argv)
        self.assertNotIn('-rw_timeout',argv)
        self.assertEqual(argv[argv.index('-i')+1],'rtsp://user:secret@127.0.0.1/live')
        self.assertNotIn('shell',popen.call_args.kwargs)
        self.assertEqual(popen.call_args.kwargs['stderr'],media.subprocess.DEVNULL)

    def test_failed_ffmpeg_launch_is_reported_without_url_or_credentials(self):
        viewer=media.MediaViewer();self.addCleanup(viewer.stop)
        with patch.object(media.shutil,'which',return_value='ffmpeg'), patch.object(media.subprocess,'Popen',side_effect=OSError('secret')):
            viewer.start_url('rtsp://user:secret@127.0.0.1/live')
        state=viewer.snapshot();self.assertEqual(state['status'],'error')
        self.assertNotIn('secret',state['error'])


if __name__=='__main__':unittest.main()
