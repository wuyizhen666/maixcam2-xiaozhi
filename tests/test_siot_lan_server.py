import http.client
import importlib.util
import io
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

PATH=Path(__file__).resolve().parents[1]/'apps/siot_monitor/lan_camera_server.py'
spec=importlib.util.spec_from_file_location('siot_lan_server',PATH)
lan=importlib.util.module_from_spec(spec);spec.loader.exec_module(lan)


def jpeg(color='red'):
    output=io.BytesIO();Image.new('RGB',(32,24),color).save(output,format='JPEG')
    return output.getvalue()


class Provider:
    def __init__(self):
        self.lock=threading.Lock();self.value=dict(status='live',sequence=1,jpeg=jpeg(),
            received_at=time.time(),width=32,height=24)
    def __call__(self):
        with self.lock:return dict(self.value)
    def update(self,**changes):
        with self.lock:self.value.update(changes)


def part(response):
    boundary=response.readline()
    while boundary in (b'\r\n',b'\n'):boundary=response.readline()
    if not boundary or boundary.startswith(b'--frame--'):return None
    if boundary!=b'--frame\r\n':raise AssertionError('Invalid multipart boundary')
    headers={}
    while True:
        line=response.readline()
        if line==b'\r\n':break
        key,value=line.decode('ascii').split(':',1);headers[key.lower()]=value.strip()
    raw=response.read(int(headers['content-length']))
    return headers,raw


class LanServerTests(unittest.TestCase):
    def setUp(self):
        self.provider=Provider();self.server=lan.LanCameraServer(self.provider,port=0,host='127.0.0.1')
        self.server.start();self.addCleanup(self.server.stop);self.connections=[];self.responses=[]
        self.addCleanup(lambda:[c.close() for c in self.connections])
        self.addCleanup(lambda:[response.close() for response in self.responses])
    def request(self,path,method='GET'):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.snapshot()['port'],timeout=2)
        self.connections.append(connection);connection.request(method,path)
        response=connection.getresponse();self.responses.append(response);return response
    def test_jpeg_is_identical_and_head_has_no_body(self):
        response=self.request('/snapshot.jpg');self.assertEqual(response.status,200)
        self.assertEqual(response.getheader('Content-Type'),'image/jpeg')
        self.assertEqual(response.read(),self.provider()['jpeg'])
        response=self.request('/snapshot.jpg','HEAD')
        self.assertEqual(response.status,200);self.assertEqual(response.read(),b'')
        self.assertEqual(int(response.getheader('Content-Length')),len(self.provider()['jpeg']))
    def test_multipart_uses_new_sequence_without_reencoding_or_duplicate_frames(self):
        response=self.request('/stream.mjpg');self.assertEqual(response.status,200)
        first=part(response);self.assertEqual(first[1],self.provider()['jpeg'])
        self.assertEqual(first[0]['x-sequence'],'1')
        raw=jpeg('blue')
        timer=threading.Timer(.25,lambda:self.provider.update(sequence=2,jpeg=raw,received_at=time.time()))
        timer.start();self.addCleanup(timer.cancel)
        started=time.monotonic();second=part(response)
        self.assertGreaterEqual(time.monotonic()-started,.2)
        self.assertEqual(second[0]['x-sequence'],'2');self.assertEqual(second[1],raw)
        self.provider.update(status='warming')
        remaining=response.read()
        self.assertNotIn(b'Content-Type: image/jpeg',remaining)
    def test_four_media_clients_are_allowed_and_fifth_is_rejected(self):
        for _ in range(4):self.assertEqual(self.request('/stream.mjpg').status,200)
        self.assertEqual(self.server.snapshot()['clients'],4)
        fifth=self.request('/stream.mjpg');self.assertEqual(fifth.status,503);fifth.read()
        self.assertEqual(self.server.snapshot()['clients'],4)
    def test_unready_stale_and_invalid_source_are_not_served(self):
        for changes in [dict(status='warming'),dict(status='live',received_at=time.time()-4),
                        dict(status='live',received_at=float('nan')),
                        dict(status='live',received_at=time.time()+10)]:
            self.provider.update(**changes)
            for endpoint in ['/snapshot.jpg','/stream.mjpg']:
                response=self.request(endpoint);self.assertEqual(response.status,503);response.read()
    def test_stale_stream_ends_instead_of_repeating_last_jpeg(self):
        response=self.request('/stream.mjpg');self.assertEqual(response.status,200);part(response)
        self.provider.update(received_at=time.time()-4)
        remainder=response.read()
        self.assertNotIn(b'Content-Type: image/jpeg',remainder)
    def test_only_fixed_read_only_paths_are_available(self):
        for path in ['/../secret','/snapshot.jpg?file=/etc/passwd','/anything','http://example.com/']:
            response=self.request(path);self.assertEqual(response.status,404);response.read()
        response=self.request('/','POST');self.assertEqual(response.status,405);response.read()
        response=self.request('/');page=response.read().decode()
        self.assertIn('/stream.mjpg',page);self.assertNotIn('https://',page)
        response=self.request('/status.json');state=json.loads(response.read())
        self.assertNotIn('jpeg',state);self.assertNotIn('frame',state)
        self.assertTrue(state['running']);self.assertTrue(state['ready'])
    def test_stop_closes_streams_and_port_can_be_reused(self):
        response=self.request('/stream.mjpg');part(response)
        port=self.server.snapshot()['port'];started=time.monotonic();self.server.stop()
        self.assertLess(time.monotonic()-started,2.5)
        self.assertFalse(self.server.snapshot()['running']);self.assertEqual(self.server.snapshot()['clients'],0)
        again=lan.LanCameraServer(self.provider,port=port,host='127.0.0.1');self.addCleanup(again.stop)
        again.start();self.assertTrue(again.snapshot()['running'])
    def test_start_is_idempotent_and_same_object_can_restart(self):
        first=self.server.snapshot()['port'];self.server.start()
        self.assertEqual(self.server.snapshot()['port'],first)
        self.server.stop();self.server.start();self.assertTrue(self.server.snapshot()['running'])
    def test_occupied_port_does_not_stop_existing_server(self):
        other=lan.LanCameraServer(self.provider,port=self.server.snapshot()['port'],host='127.0.0.1')
        self.addCleanup(other.stop);other.start()
        self.assertFalse(other.snapshot()['running']);self.assertIsNotNone(other.snapshot()['error'])
        response=self.request('/status.json');self.assertEqual(response.status,200);response.read()
    def test_original_frame_rate_is_capped_at_ten_per_second(self):
        count=0
        def continuous():
            nonlocal count
            count+=1
            return dict(self.provider(),sequence=count,received_at=time.time())
        self.server.stop();self.server=lan.LanCameraServer(continuous,port=0,host='127.0.0.1');self.addCleanup(self.server.stop)
        self.server.start();response=self.request('/stream.mjpg')
        started=time.monotonic()
        for _ in range(5):self.assertIsNotNone(part(response))
        self.assertGreaterEqual(time.monotonic()-started,.35)

    def test_slow_media_consumer_times_out_without_a_frame_queue(self):
        raw=b'\xff\xd8'+b'x'*(512*1024)+b'\xff\xd9'
        count=0
        def continuous():
            nonlocal count
            count+=1
            return dict(self.provider(),sequence=count,received_at=time.time(),jpeg=raw)
        self.server.stop();self.server=lan.LanCameraServer(continuous,port=0,host='127.0.0.1');self.addCleanup(self.server.stop)
        self.server.start()
        connection=http.client.HTTPConnection('127.0.0.1',self.server.snapshot()['port'],timeout=3)
        self.connections.append(connection);connection.connect()
        connection.sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024)
        connection.request('GET','/stream.mjpg');response=connection.getresponse();self.responses.append(response)
        self.assertEqual(response.status,200)
        deadline=time.monotonic()+3.3
        while self.server.snapshot()['clients'] and time.monotonic()<deadline:time.sleep(.05)
        self.assertEqual(self.server.snapshot()['clients'],0)
        self.assertLessEqual(count,3,'A stalled socket must not gather many provider frames')


class AddressTests(unittest.TestCase):
    def test_binding_does_not_perform_reverse_dns(self):
        server=lan.LanCameraServer(Provider(),port=0,host='127.0.0.1');self.addCleanup(server.stop)
        with patch.object(lan.socket,'getfqdn',side_effect=AssertionError('DNS must not be needed')):
            server.start()
        self.assertTrue(server.snapshot()['running'])

    def test_prefers_existing_wlan_address_and_never_probes_the_internet(self):
        interfaces=[{'ifname':'lo','addr_info':[{'family':'inet','local':'127.0.0.1'}]},
                    {'ifname':'eth0','addr_info':[{'family':'inet','local':'192.168.20.2'}]},
                    {'ifname':'wlan0','addr_info':[{'family':'inet','local':'192.168.10.2'}]}]
        result=type('Result',(),{'returncode':0,'stdout':json.dumps(interfaces)})()
        server=lan.LanCameraServer(Provider(),port=0);self.addCleanup(server.stop)
        with patch.object(lan.subprocess,'run',return_value=result) as run:
            server.start()
        state=server.snapshot();self.assertFalse(state['local_only'])
        self.assertTrue(state['urls']['mjpeg'].startswith('http://192.168.10.2:'))
        self.assertEqual(run.call_args.args[0],['ip','-j','-4','addr','show'])
        self.assertFalse(run.call_args.kwargs.get('shell',False))
    def test_missing_interface_information_is_explicitly_local_only(self):
        server=lan.LanCameraServer(Provider(),port=0);self.addCleanup(server.stop)
        with patch.object(lan.subprocess,'run',side_effect=OSError()):server.start()
        state=server.snapshot();self.assertTrue(state['local_only'])
        self.assertTrue(state['urls']['page'].startswith('http://127.0.0.1:'))


if __name__=='__main__':unittest.main()
