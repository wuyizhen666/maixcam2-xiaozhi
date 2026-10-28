import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]/'apps/siot_monitor'
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('siot_media_session',ROOT/'media_session.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class MediaSessionTests(unittest.TestCase):
    def setUp(self):
        self.viewer=Mock();self.camera=Mock();self.publisher=Mock()
        self.now=10.
        self.camera.snapshot.return_value={'sequence':1,'jpeg':b'jpeg','frame':None,'received_at':100.,'width':320,'height':240}
        self.publisher.snapshot.return_value={'connected':True,'sent':0,'dropped':0}
        self.session=module.MediaSession(self.viewer,self.camera,self.publisher,lambda:self.now)

    def test_source_switch_stops_camera_and_outgoing_frames(self):
        self.session.start_camera();self.session.set_continuous('alice/camera',True)
        self.session.start_topic('alice/receive',('alice/receive',b'png'))
        self.assertIsNone(self.session.continuous_topic)
        self.camera.stop.assert_called()
        self.publisher.stop.assert_called()
        self.viewer.submit_topic.assert_called_once_with('alice/receive',b'png')

    def test_continuous_feeds_each_snapshot_once_without_gui_rate_quantization(self):
        self.session.start_camera();self.session.set_continuous('alice/camera',True)
        self.session.poll();self.session.poll()
        self.publisher.submit.assert_called_once()
        self.camera.snapshot.return_value['sequence']=2
        self.now+=.1;self.session.poll();self.assertEqual(self.publisher.submit.call_count,2)
        self.now+=.11;self.session.poll();self.assertEqual(self.publisher.submit.call_count,2)
        self.now+=.3;self.session.poll();self.assertEqual(self.publisher.submit.call_count,2)

    def test_disconnected_camera_does_not_queue_old_video(self):
        self.session.start_camera();self.session.set_continuous('alice/camera',True)
        self.publisher.snapshot.return_value={'connected':False}
        self.session.poll();self.publisher.submit.assert_not_called()

    def test_invalid_topic_cannot_start_camera_sending(self):
        self.session.start_camera()
        with self.assertRaises(ValueError):self.session.set_continuous('alice/#',True)
        self.assertIsNone(self.session.continuous_topic)

    def test_single_uses_original_jpeg_and_metadata(self):
        self.session.start_camera();self.session.send_single('alice/camera')
        self.publisher.submit.assert_called_once_with('alice/camera',b'jpeg',1,100.,320,240)

    def test_camera_format_is_selected_while_publisher_is_stopped(self):
        self.session.start_camera('base64')
        self.publisher.set_format.assert_called_once_with('base64')
        self.assertEqual(self.session.wire_format,'base64')
        with self.assertRaises(ValueError):self.session.start_camera('invalid')

    def test_base64_session_leaves_wire_rate_to_publisher(self):
        self.session.start_camera('base64',target_fps=2);self.session.set_continuous('alice/camera',True)
        self.publisher.set_rate.assert_called_once_with(2)
        self.session.poll();self.camera.snapshot.return_value['sequence']=2
        self.now+=.3;self.session.poll();self.assertEqual(self.publisher.submit.call_count,2)
        self.now+=.21;self.session.poll();self.assertEqual(self.publisher.submit.call_count,2)

    def test_stop_disables_future_submit(self):
        self.session.start_camera();self.session.set_continuous('alice/camera',True)
        self.session.stop();self.session.poll()
        self.publisher.submit.assert_not_called()

    def test_cached_topic_frame_preserves_original_receive_time(self):
        self.session.start_topic('alice/camera',('alice/camera',b'image',100.))
        self.viewer.submit_topic.assert_called_once_with('alice/camera',b'image',received_at=100.)

    def test_lan_sender_starts_server_and_camera_without_mqtt_publishing(self):
        server=Mock();server.snapshot.return_value={'running':True,'clients':1}
        self.session.lan_server=server
        self.session.start_lan_sender()
        self.camera.start.assert_called_once();server.start.assert_called_once()
        self.publisher.start.assert_not_called()
        self.assertTrue(self.session.poll()['lan']['running'])
        self.session.stop();self.assertEqual(server.stop.call_count,2)

    def test_lan_bind_failure_releases_camera(self):
        server=Mock();server.start.side_effect=OSError('address in use')
        self.session.lan_server=server
        with self.assertRaises(OSError):self.session.start_lan_sender()
        self.assertIsNone(self.session.mode)
        self.assertEqual(self.camera.stop.call_count,2)

    def test_reported_lan_bind_failure_releases_camera(self):
        server=Mock();server.snapshot.return_value={'running':False,'error':'port busy'}
        self.session.lan_server=server
        with self.assertRaisesRegex(RuntimeError,'port busy'):self.session.start_lan_sender()
        self.assertIsNone(self.session.mode)
        self.assertEqual(self.camera.stop.call_count,2)

    def test_cleanup_error_still_stops_other_sources(self):
        self.publisher.stop.side_effect=RuntimeError('failure')
        with self.assertRaises(RuntimeError):self.session.stop()
        self.viewer.stop.assert_called_once()
        self.camera.stop.assert_called_once()


if __name__=='__main__':unittest.main()
