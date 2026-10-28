"""Opt-in LAN JPEG/MJPEG preview; consumes existing CameraPreview snapshots.

No camera initialization, recording, remote controls, files, or RTSP server.
"""
import ipaddress
import json
import math
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer

MAX_JPEG_BYTES=2*1024*1024
MAX_MEDIA_CLIENTS=4
MAX_CONNECTIONS=8
SOCKET_TIMEOUT=2.
STALE_SECONDS=3.
FRAME_INTERVAL=.1

PAGE='''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MaixCAM2 局域网预览</title><style>
body{background:#101b29;color:#e8f2fa;font:16px sans-serif;margin:24px}
img{display:block;max-width:100%;height:auto;border:1px solid #30465a}
a{color:#46ddb0}p{line-height:1.6}</style>
<h1>MaixCAM2 局域网预览</h1><p id="status">正在连接摄像头…</p><img id="preview" src="/stream.mjpg" alt="摄像头实时画面">
<p>画面来自当前摄像头，无录像。摄像头停止或断流后请刷新页面。</p>
<p><a href="/snapshot.jpg">查看最新 JPG</a> · <a href="/status.json">查看状态</a></p>
<script>
const picture=document.getElementById('preview'), status=document.getElementById('status');
let active=true, retry;
picture.onerror=()=>{clearTimeout(retry);retry=setTimeout(()=>{if(active)picture.src='/stream.mjpg';},1000);};
async function updateStatus(){
  try{const response=await fetch('/status.json',{cache:'no-store',signal:AbortSignal.timeout(1500)});
    if(!response.ok)throw new Error(); const state=await response.json();
    status.textContent=state.ready?'摄像头就绪 · '+state.width+'×'+state.height+' · 最近画面 '+state.age_seconds.toFixed(1)+' 秒前':'等待摄像头画面…';
  }catch(_){status.textContent='共享连接已断开，当前画面可能已停止更新。';}
  if(active)setTimeout(updateStatus,1000);
}
addEventListener('pagehide',()=>{active=false;clearTimeout(retry);});updateStatus();
</script></html>'''.encode('utf-8')


def _address(host):
    if host!='0.0.0.0':
        return host,ipaddress.ip_address(host).is_loopback
    candidates=[]
    try:
        result=subprocess.run(['ip','-j','-4','addr','show'],capture_output=True,text=True,timeout=2)
        if result.returncode!=0:raise ValueError()
        interfaces=json.loads(result.stdout)
        if not isinstance(interfaces,list):raise ValueError()
        for interface in interfaces:
            if not isinstance(interface,dict) or interface.get('operstate')=='DOWN':continue
            name=interface.get('ifname','')
            if not isinstance(name,str):continue
            rank=0 if name.startswith(('wlan','wlp')) else (1 if name.startswith(('eth','en')) else 2)
            for info in interface.get('addr_info',[]):
                if not isinstance(info,dict) or info.get('family')!='inet':continue
                address=ipaddress.ip_address(info.get('local',''))
                if address.version!=4 or address.is_loopback or address.is_unspecified or address.is_multicast:continue
                candidates.append((rank,address.is_link_local,str(address)))
    except (OSError,ValueError,TypeError,subprocess.TimeoutExpired):pass
    if not candidates:return '127.0.0.1',True
    return min(candidates)[2],False


class _CameraServer(HTTPServer):
    allow_reuse_address=True
    request_queue_size=8

    def __init__(self,address,owner):
        self.owner=owner;self.stop_event=threading.Event();self.guard=threading.Lock()
        self.connections=set();self.workers=set();self.media_clients=0
        super().__init__(address,_Handler)

    def server_bind(self):
        if os.name=='nt' and hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            self.allow_reuse_address=False
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        # HTTPServer.server_bind performs reverse DNS; local sharing needs none.
        TCPServer.server_bind(self)
        self.server_name=self.server_address[0];self.server_port=self.server_address[1]

    def process_request(self,request,address):
        with self.guard:
            accepted=not self.stop_event.is_set() and len(self.connections)<MAX_CONNECTIONS
            if accepted:
                self.connections.add(request)
                worker=threading.Thread(target=self._handle,args=(request,address),daemon=True,name='siot-lan-client')
                self.workers.add(worker);worker.start()
        if not accepted:
            try:
                request.settimeout(.05)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:pass
            self.shutdown_request(request)

    def _handle(self,request,address):
        try:self.finish_request(request,address)
        except Exception:pass  # Do not log frame bytes, URLs, or private client details.
        finally:
            try:self.shutdown_request(request)
            except OSError:pass
            with self.guard:
                self.connections.discard(request);self.workers.discard(threading.current_thread())

    def acquire_media(self):
        with self.guard:
            if self.stop_event.is_set() or self.media_clients>=MAX_MEDIA_CLIENTS:return False
            self.media_clients+=1;return True

    def release_media(self):
        with self.guard:self.media_clients=max(0,self.media_clients-1)


class _Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    server_version='SIoT-Camera'
    sys_version=''

    def setup(self):
        self.request.settimeout(SOCKET_TIMEOUT)
        # Bound kernel buffering as well as avoiding an application frame queue.
        try:self.request.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,16*1024)
        except OSError:pass
        super().setup()

    def log_message(self,*args):pass

    def _headers(self,code,content_type,length=None):
        self.send_response(code)
        self.send_header('Content-Type',content_type)
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Connection','close')
        if length is not None:self.send_header('Content-Length',str(length))
        self.end_headers();self.close_connection=True

    def _reply(self,code,body=b'',content_type='text/plain; charset=utf-8',head=False):
        self._headers(code,content_type,len(body))
        if not head:self.wfile.write(body)

    def send_error(self,code,message=None,explain=None):
        self._reply(code,b'Request rejected',head=getattr(self,'command',None)=='HEAD')

    def do_GET(self):self._serve(False)
    def do_HEAD(self):self._serve(True)
    def do_POST(self):self._reply(405,b'Read-only preview')
    do_PUT=do_DELETE=do_PATCH=do_OPTIONS=do_CONNECT=do_TRACE=do_POST

    def _serve(self,head):
        if self.path not in ('/','/snapshot.jpg','/stream.mjpg','/status.json'):
            self._reply(404,b'Not found',head=head);return
        if self.path=='/':self._reply(200,PAGE,'text/html; charset=utf-8',head);return
        if self.path=='/status.json':
            status=self.server.owner.snapshot();frame=self.server.owner._frame()
            safe=dict(running=status['running'],clients=status['clients'],port=status['port'],
                      local_only=status['local_only'],ready=frame is not None,
                      source_status='live' if frame else 'unavailable')
            if frame:
                safe.update(sequence=frame['sequence'],received_at=frame['received_at'],
                            age_seconds=max(0.,time.time()-frame['received_at']),
                            width=frame['width'],height=frame['height'])
            self._reply(200,json.dumps(safe).encode(),'application/json; charset=utf-8',head);return
        frame=self.server.owner._frame()
        if frame is None:self._reply(503,b'Camera not ready or frame stale',head=head);return
        if head:
            if self.path=='/snapshot.jpg':self._headers(200,'image/jpeg',len(frame['jpeg']))
            else:self._headers(200,'multipart/x-mixed-replace; boundary=frame')
            return
        if not self.server.acquire_media():self._reply(503,b'Too many media clients');return
        try:
            if self.path=='/snapshot.jpg':
                self._reply(200,frame['jpeg'],'image/jpeg');return
            self._headers(200,'multipart/x-mixed-replace; boundary=frame')
            last_sequence=None;next_frame=0.
            while not self.server.stop_event.is_set():
                wait=max(0.,next_frame-time.monotonic())
                if self.server.stop_event.wait(wait):break
                current=self.server.owner._frame()
                if current is None:
                    self.wfile.write(b'--frame--\r\n');break
                if current['sequence']==last_sequence:
                    if self.server.stop_event.wait(.04):break
                    continue
                raw=current['jpeg']
                header=('--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(raw))+
                        '\r\nX-Sequence: '+str(current['sequence'])+'\r\n\r\n').encode('ascii')
                self.wfile.write(header);self.wfile.write(raw);self.wfile.write(b'\r\n')
                last_sequence=current['sequence'];next_frame=time.monotonic()+FRAME_INTERVAL
        except (OSError,ValueError):pass
        finally:self.server.release_media()


class LanCameraServer:
    def __init__(self,provider,port=8090,host='0.0.0.0'):
        if not callable(provider):raise ValueError('Camera provider is required')
        if type(port) is not int or not 0<=port<=65535:raise ValueError('Invalid preview port')
        try:
            if not isinstance(host,str):raise ValueError()
            address=ipaddress.ip_address(host)
            if address.version!=4 or address.is_multicast:raise ValueError()
        except ValueError:raise ValueError('Preview host must be an IPv4 address') from None
        self._provider=provider;self._configured_port=port;self._host=host
        self._operation_lock=threading.RLock();self._lock=threading.Lock()
        self._server=None;self._thread=None;self._port=port;self._urls={}
        self._error=None;self._local_only=address.is_loopback;self._lingering=[]

    def _frame(self):
        try:
            value=self._provider()
            if not isinstance(value,dict) or value.get('status')!='live':return None
            sequence=value.get('sequence');stamp=value.get('received_at');raw=value.get('jpeg')
            width=value.get('width');height=value.get('height')
            if type(sequence) is not int or sequence<1 or type(stamp) not in (int,float):return None
            if not math.isfinite(stamp) or not -1<=time.time()-stamp<=STALE_SECONDS:return None
            if not isinstance(raw,bytes) or not 4<=len(raw)<=MAX_JPEG_BYTES:return None
            if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):return None
            if type(width) is not int or type(height) is not int or width<1 or height<1 or width*height>4_000_000:return None
            return dict(sequence=sequence,received_at=float(stamp),jpeg=raw,width=width,height=height)
        except Exception:return None

    def snapshot(self):
        with self._lock:
            server=self._server;running=server is not None and not server.stop_event.is_set()
            clients=0
            if server:
                with server.guard:clients=server.media_clients
            return dict(running=running,clients=clients,port=self._port,urls=dict(self._urls),
                        error=self._error,local_only=self._local_only)

    def start(self):
        with self._operation_lock:
            if self.snapshot()['running']:return
            if any(thread.is_alive() for thread in self._lingering):
                with self._lock:self._error='上一连接尚未停止，请稍后重试'
                return
            self._lingering=[]
            try:server=_CameraServer((self._host,self._configured_port),self)
            except OSError:
                with self._lock:self._error='预览端口已被占用或无法监听'
                return
            address,local_only=_address(self._host);port=server.server_port
            base='http://'+address+':'+str(port)
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},
                                    daemon=True,name='siot-lan-server')
            with self._lock:
                self._server=server;self._thread=thread;self._port=port;self._error=None;self._local_only=local_only
                self._urls={'page':base+'/','snapshot':base+'/snapshot.jpg','mjpeg':base+'/stream.mjpg'}
            thread.start()

    def stop(self):
        with self._operation_lock:
            with self._lock:server=self._server;thread=self._thread
            if server is None:return
            server.stop_event.set()
            with server.guard:connections=list(server.connections);workers=list(server.workers)
            for connection in connections:
                try:connection.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                try:connection.close()
                except OSError:pass
            server.shutdown();server.server_close()
            deadline=time.monotonic()+1.5
            for worker in [thread]+workers:
                if worker is not None and worker is not threading.current_thread():
                    worker.join(timeout=max(0.,deadline-time.monotonic()))
            self._lingering=[worker for worker in workers if worker.is_alive()]
            with self._lock:
                self._server=None;self._thread=None
                if self._lingering:self._error='预览连接关闭超时，请稍后重试'
