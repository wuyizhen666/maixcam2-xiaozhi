import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps/siot_monitor'))
sys.path.insert(0, str(ROOT / 'xiaozhi/service'))
from camera_preview import CameraPreview, clean_environment
import camera_worker


class CameraPreviewTests(unittest.TestCase):
    def test_drain_uses_last_ready_frame_and_stops_at_empty(self):
        first,second,last=object(),object(),object()
        cam=Mock();cam.read.side_effect=[second,last,None]
        # Functional ordering must not depend on host/ARM mock execution speed.
        # The separate budget test advances its own clock across the 8ms limit.
        with patch.object(camera_worker.time,'monotonic',return_value=1.):
            result=camera_worker.drain_ready(cam,first,__import__('threading').Event())
        self.assertIs(result,last)
        self.assertEqual(cam.read.call_count,3)
        for call in cam.read.call_args_list:self.assertEqual(call.kwargs,{'block':False})

    def test_drain_treats_explicit_buffer_empty_as_end(self):
        first=object();cam=Mock()
        cam.read.side_effect=RuntimeError(': Buffer empty: read camera failed')
        with patch.object(camera_worker.time,'monotonic',return_value=1.):
            self.assertIs(camera_worker.drain_ready(cam,first,__import__('threading').Event()),first)
        cam.read.assert_called_once_with(block=False)

    def test_drain_enforces_frame_limit(self):
        first,last=object(),object();cam=Mock();cam.read.return_value=last
        with patch.object(camera_worker.time,'monotonic',return_value=1.):
            result=camera_worker.drain_ready(cam,first,__import__('threading').Event(),max_frames=4)
        self.assertIs(result,last)
        self.assertEqual(cam.read.call_count,4)

    def test_drain_checks_time_budget_before_starting_another_read(self):
        clock=[0.];frames=[object(),object()];cam=Mock()
        def read(**kwargs):
            clock[0]+=.005
            return frames[cam.read.call_count-1]
        cam.read.side_effect=read
        with patch.object(camera_worker.time,'monotonic',side_effect=lambda:clock[0]):
            result=camera_worker.drain_ready(cam,object(),__import__('threading').Event(),budget_s=.008)
        self.assertIs(result,frames[-1])
        self.assertEqual(cam.read.call_count,2)

    def test_drain_stop_drops_result_and_never_starts_new_reads(self):
        stop=__import__('threading').Event();stop.set();cam=Mock()
        with patch.object(camera_worker.time,'monotonic',return_value=1.):
            self.assertIsNone(camera_worker.drain_ready(cam,object(),stop))
        cam.read.assert_not_called()
        stop.clear()
        def read(**kwargs):stop.set();return object()
        cam.read.side_effect=read
        with patch.object(camera_worker.time,'monotonic',return_value=1.):
            self.assertIsNone(camera_worker.drain_ready(cam,object(),stop))
        cam.read.assert_called_once_with(block=False)

    def test_drain_propagates_other_native_errors(self):
        cam=Mock();cam.read.side_effect=RuntimeError('camera disconnected')
        with patch.object(camera_worker.time,'monotonic',return_value=1.),self.assertRaisesRegex(RuntimeError,'disconnected'):
            camera_worker.drain_ready(cam,object(),__import__('threading').Event())
        cam.read.assert_called_once_with(block=False)

    def test_warmup_retries_initial_buffer_empty_then_counts_valid_frames(self):
        clock=[0.]
        frame=object()
        calls=[]
        def read(**kwargs):
            calls.append(kwargs['block_ms'])
            if len(calls)==1:
                clock[0]+=1.
                raise RuntimeError(': Buffer empty: read camera failed')
            clock[0]+=.04
            return frame
        cam=Mock();cam.read.side_effect=read
        with patch.object(camera_worker.time,'monotonic',side_effect=lambda:clock[0]):
            self.assertTrue(camera_worker.warmup_camera(cam,__import__('threading').Event()))
        self.assertEqual(len(calls),31)
        self.assertGreaterEqual(clock[0],2.)
        self.assertTrue(all(1<=value<=1000 for value in calls))

    def test_continuous_empty_reaches_warmup_deadline(self):
        clock=[0.]
        def read(**kwargs):
            clock[0]+=kwargs['block_ms']/1000
            return None
        cam=Mock();cam.read.side_effect=read
        with patch.object(camera_worker.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(RuntimeError,'timeout'):
                camera_worker.warmup_camera(cam,__import__('threading').Event())
        self.assertEqual(clock[0],8.)
        self.assertEqual(cam.read.call_count,8)

    def test_stop_after_empty_does_not_start_another_read(self):
        stop=__import__('threading').Event()
        def read(**kwargs):
            stop.set()
            raise RuntimeError(': Buffer empty: read camera failed')
        cam=Mock();cam.read.side_effect=read
        self.assertIsNone(camera_worker.read_available(cam,stop,time.monotonic()+8))
        cam.read.assert_called_once()
        self.assertIsNone(camera_worker.read_available(cam,stop,time.monotonic()+8))
        cam.read.assert_called_once()

    def test_other_camera_exception_is_not_retried(self):
        cam=Mock();cam.read.side_effect=RuntimeError('Camera device lost')
        with self.assertRaisesRegex(RuntimeError,'device lost'):
            camera_worker.read_available(cam,__import__('threading').Event(),time.monotonic()+8)
        cam.read.assert_called_once()

    def test_steady_empty_is_bounded_by_three_second_deadline(self):
        clock=[10.]
        def read(**kwargs):
            clock[0]+=kwargs['block_ms']/1000
            raise RuntimeError(': Buffer empty: read camera failed')
        cam=Mock();cam.read.side_effect=read
        with patch.object(camera_worker.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(RuntimeError,'timeout'):
                camera_worker.read_available(cam,__import__('threading').Event(),13.)
        self.assertEqual(cam.read.call_count,3)
        self.assertEqual(clock[0],13.)

    def test_clean_environment_removes_injection_only(self):
        clean = clean_environment({'LD_PRELOAD':'private.so','MAIXCAM2_OVL':'1','PATH':'path','HOME':'home'})
        self.assertEqual(clean, {'PATH':'path','HOME':'home'})

    def fixture(self, source):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        worker = root / 'fixture.py'
        worker.write_text(source, encoding='utf-8')
        preview = CameraPreview(worker_path=worker, runtime_root=root)
        self.addCleanup(preview.stop)
        return preview, root

    def wait_for(self, preview, status, timeout=5):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            value=preview.snapshot()
            if value['status']==status:return value
            time.sleep(.02)
        self.fail('Status was '+str(preview.snapshot()))

    def test_latest_frame_raw_jpeg_and_stop_cleanup(self):
        preview, root = self.fixture('''import argparse,json,time,hashlib,io
from pathlib import Path
from PIL import Image
p=argparse.ArgumentParser();p.add_argument('--directory');p.add_argument('--parent-pid');a=p.parse_args();d=Path(a.directory)
b=io.BytesIO();Image.new('RGB',(320,240),'red').save(b,format='JPEG');raw=b.getvalue()
(d/'frame-1.jpg').write_bytes(raw)
(d/'state.json').write_text(json.dumps(dict(status='live',sequence=1,received_at=time.time(),fps=10,width=320,height=240,frame_file='frame-1.jpg',bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())))
time.sleep(30)
''')
        preview.start()
        value=self.wait_for(preview,'live')
        self.assertEqual(value['frame'].size,(320,240))
        self.assertEqual((value['width'],value['height']),(320,240))
        self.assertTrue(value['jpeg'].startswith(b'\xff\xd8'))
        self.assertEqual(value['sequence'],1)
        self.assertEqual(value['fps'],10)
        first_pid=preview._process.pid
        preview.start()
        self.assertEqual(preview._process.pid,first_pid)
        preview.stop()
        self.assertEqual(preview.snapshot()['status'],'stopped')
        self.assertEqual(list(root.glob('siot-camera-*')),[])
        preview.start()
        self.wait_for(preview,'live')
        self.assertNotEqual(preview._process.pid,first_pid)

    def test_process_exit_is_error_not_frozen_live_image(self):
        preview,_=self.fixture('raise SystemExit(3)')
        preview.start()
        value=self.wait_for(preview,'error')
        self.assertIsNone(value['frame'])
        self.assertIsNone(value['jpeg'])
        self.assertNotIn('3',value['error'])

    def test_invalid_metadata_never_decodes_arbitrary_path(self):
        preview,_=self.fixture('''import sys,json,time
from pathlib import Path
d=Path(sys.argv[sys.argv.index('--directory')+1]);(d/'state.json').write_text(json.dumps(dict(status='live',sequence=1,received_at=time.time(),fps=10,width=320,height=240,frame_file='../private.jpg',bytes=100,sha256='x'*64)));time.sleep(30)
''')
        preview.start()
        value=self.wait_for(preview,'error')
        self.assertIsNone(value['frame'])

    def test_stale_worker_frame_is_not_reported_live(self):
        preview,_=self.fixture('''import sys,json,time
from pathlib import Path
d=Path(sys.argv[sys.argv.index('--directory')+1]);(d/'state.json').write_text(json.dumps(dict(status='live',sequence=1,received_at=time.time()-20,fps=10,width=320,height=240,frame_file='frame-1.jpg',bytes=100,sha256='x'*64)));time.sleep(30)
''')
        preview.start()
        value=self.wait_for(preview,'error')
        self.assertIsNone(value['frame'])

    def test_worker_atomic_output_and_frame_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            camera_worker.publish_frame(root,b'\xff\xd8test\xff\xd9',1,10.,7.5)
            state=json.loads((root/'state.json').read_text())
            self.assertEqual(state['frame_file'],'frame-1.jpg')
            self.assertEqual(state['sequence'],1)
            self.assertEqual(state['sha256'],hashlib.sha256((root/'frame-1.jpg').read_bytes()).hexdigest())
            with self.assertRaises(ValueError):camera_worker.publish_frame(root,b'x'*(512*1024+1),2,11.,8.)

    def test_stop_kills_stuck_worker_and_reaps(self):
        preview,_=self.fixture('import time; time.sleep(30)')
        fake=Mock()
        fake.poll.return_value=None
        fake.wait.side_effect=[__import__('subprocess').TimeoutExpired('fixture',2),0]
        self.assertTrue(preview._terminate_worker(fake))
        fake.terminate.assert_called_once()
        fake.kill.assert_called_once()
        self.assertEqual(fake.wait.call_count,2)

    def test_generation_rejects_late_frame_after_stop(self):
        preview,_=self.fixture('import time; time.sleep(30)')
        generation=preview._generation
        preview.stop()
        preview._set(generation,{'status':'live'})
        self.assertEqual(preview.snapshot()['status'],'stopped')

    def test_worker_close_precedes_shared_lock_release_even_on_read_failure(self):
        events=[]
        class Lease:
            def __enter__(self):events.append('acquire')
            def __exit__(self,*unused):events.append('release')
        class Camera:
            def exp_mode(self,*args):pass
            def awb_mode(self,*args):pass
            def read(self,**kwargs):raise RuntimeError('fixture read failure')
            def close(self):events.append('close')
        import types
        fake=types.SimpleNamespace(camera=types.SimpleNamespace(Camera=lambda *args:Camera(),AeMode=types.SimpleNamespace(Auto=0),AwbMode=types.SimpleNamespace(Auto=0)),
                                   image=types.SimpleNamespace(Format=types.SimpleNamespace(FMT_RGB888=0)))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            original_resolve=Path.resolve
            # run() must enforce Linux volatile paths in production; only the
            # resolver/stat are substituted here, never opening Maix hardware.
            fake_dir=Path('/tmp/siot-camera-fixture')
            fake_state=types.SimpleNamespace(st_uid=123,st_mode=0o40700)
            with patch.object(camera_worker,'parent_death_signal'),patch.object(camera_worker.os,'geteuid',return_value=123,create=True),\
                 patch.object(Path,'resolve',autospec=True,side_effect=lambda p,*a,**kw:fake_dir if p==path else original_resolve(p,*a,**kw)),patch.object(Path,'stat',return_value=fake_state),\
                 patch.object(Path,'is_symlink',return_value=False),patch.object(camera_worker,'write_state'),\
                 patch.object(camera_worker.signal,'signal'),patch.dict(sys.modules,{'maix':fake}),\
                 patch('camera_ownership.CameraLease',return_value=Lease()):
                self.assertEqual(camera_worker.run(path,os.getpid()),1)
        self.assertEqual(events,['acquire','close','release'])


if __name__ == '__main__':unittest.main()
