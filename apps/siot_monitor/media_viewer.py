"""Bounded, asynchronous image/MJPEG/RTSP viewing without Maix hardware.

HTTP runs in a disposable Python process so slow DNS/read calls are cancellable.
RTSP uses the device's existing ffmpeg. Topic images use a one-item decode queue.
"""
import base64
import io
import json
import math
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit, unquote
import urllib.request
import warnings

from PIL import Image, ImageOps

MAX_IMAGE_BYTES=2*1024*1024
MAX_PAYLOAD_BYTES=3*1024*1024
MAX_RECOGNITION_BYTES=256*1024
MAX_CAPTURE_ID_BYTES=128
MAX_QUESTION_BYTES=8192
MAX_PIXELS=4_000_000
MAX_URL=2048
MIMES={'image/jpeg':'JPEG','image/png':'PNG'}


def validate_url(url):
    if not isinstance(url,str) or not url or len(url)>MAX_URL or any(ord(c)<33 or ord(c)==127 for c in url):
        raise ValueError('媒体地址不合法')
    try:
        parsed=urlsplit(url)
        if parsed.scheme.lower() not in ('http','https','rtsp') or not parsed.hostname:
            raise ValueError()
        if parsed.port is not None and not 1<=parsed.port<=65535:raise ValueError()
    except ValueError:
        raise ValueError('仅支持 HTTP、HTTPS 或 RTSP 媒体地址') from None
    return url


def decode_payload(payload):
    """Return a display thumbnail; transport bytes are never re-encoded here."""
    return _decode_frame(payload)[0]


def _decode_frame(payload):
    if not isinstance(payload,(bytes,bytearray)) or not 0<len(payload)<=MAX_PAYLOAD_BYTES:
        raise ValueError('图片消息为空或超过 3 MiB')
    raw=bytes(payload);declared=None
    metadata={'capture_id':None,'recognition':None}
    if raw.startswith(b'data:'):
        header,separator,encoded=raw.partition(b',')
        mime=header[5:-7].decode('ascii','ignore') if header.endswith(b';base64') else ''
        if not separator or mime not in MIMES:raise ValueError('不支持的图片数据格式')
        declared=MIMES[mime]
        try:raw=base64.b64decode(encoded,validate=True)
        except (ValueError,base64.binascii.Error):raise ValueError('图片编码损坏') from None
    elif raw.lstrip().startswith(b'{'):
        try:
            value=json.loads(raw)
            if not isinstance(value,dict) or value.get('type')!='image' or value.get('mime') not in MIMES:
                raise ValueError()
            if value.get('encoding','base64')!='base64' or not isinstance(value.get('data'),str):raise ValueError()
            for field,limit in [('capture_id',MAX_CAPTURE_ID_BYTES),('question',MAX_QUESTION_BYTES),
                                ('recognition',MAX_RECOGNITION_BYTES)]:
                if field not in value:continue
                text=value[field]
                if not isinstance(text,str) or len(text.encode('utf-8'))>limit:raise ValueError()
                if field=='capture_id' and (not text or any(ord(c)<32 or ord(c)==127 for c in text)):raise ValueError()
                if field in metadata:metadata[field]=text
            declared=MIMES[value['mime']]
            raw=base64.b64decode(value['data'],validate=True)
        except (ValueError,TypeError,UnicodeError,base64.binascii.Error):
            raise ValueError('图片消息格式无效') from None
    elif not raw.startswith((b'\xff\xd8\xff',b'\x89PNG\r\n\x1a\n')):
        # Mind+ and similar MQTT clients can send an unwrapped standard Base64
        # string. Only surrounding whitespace is allowed, never inner newlines.
        try:raw=base64.b64decode(raw.strip(),validate=True)
        except (ValueError,base64.binascii.Error):raise ValueError('图片编码损坏') from None
    if not raw or len(raw)>MAX_IMAGE_BYTES:raise ValueError('图片为空或超过 2 MiB')
    if not raw.startswith((b'\xff\xd8\xff',b'\x89PNG\r\n\x1a\n')):
        raise ValueError('图片格式不是 JPEG 或 PNG')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in ('JPEG','PNG') or (declared and source.format!=declared):raise ValueError()
                if source.width<1 or source.height<1 or source.width*source.height>MAX_PIXELS:raise ValueError()
                if getattr(source,'n_frames',1)!=1:raise ValueError()
                source.verify()
            with Image.open(io.BytesIO(raw)) as source:
                source.load()
                result=ImageOps.exif_transpose(source).convert('RGB')
                result.thumbnail((400,280),Image.Resampling.LANCZOS)
                return result.copy(),metadata
    except (OSError,ValueError,SyntaxError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise ValueError('图片损坏、尺寸超限或格式不支持') from None


class JpegBuffer:
    """Incremental, bounded JPEG extraction from MJPEG or ffmpeg image2pipe."""
    def __init__(self):self.buffer=bytearray()
    def feed(self,chunk):
        self.buffer.extend(chunk);frames=[]
        while True:
            start=self.buffer.find(b'\xff\xd8')
            if start<0:
                self.buffer[:]=self.buffer[-1:]
                break
            if start:del self.buffer[:start]
            end=self.buffer.find(b'\xff\xd9',2)
            if end<0:
                if len(self.buffer)>MAX_IMAGE_BYTES:raise ValueError('视频帧超过 2 MiB')
                break
            end+=2
            if end>MAX_IMAGE_BYTES:raise ValueError('视频帧超过 2 MiB')
            frames.append(bytes(self.buffer[:end]));del self.buffer[:end]
        return frames


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        validate_url(newurl)
        if urlsplit(newurl).scheme not in ('http','https'):raise ValueError('媒体重定向无效')
        original=urlsplit(request.full_url);target=urlsplit(newurl)
        if original.scheme=='https' and target.scheme!='https':raise ValueError('不允许降低媒体连接安全性')
        redirected=super().redirect_request(request,fp,code,msg,headers,newurl)
        def origin(parsed):
            return parsed.scheme,parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80)
        if origin(original)!=origin(target):
            redirected.remove_header('Authorization')
        return redirected


def _http_worker(url,timeout):
    """Private subprocess protocol: kind byte, uint32 size, then raw image."""
    validate_url(url);parsed=urlsplit(url)
    headers={'User-Agent':'SIoT-Monitor/1','Accept':'image/jpeg,image/png,multipart/x-mixed-replace'}
    if parsed.username is not None:
        user=unquote(parsed.username);password=unquote(parsed.password or '')
        headers['Authorization']='Basic '+base64.b64encode((user+':'+password).encode()).decode()
        host=parsed.hostname
        if ':' in host:host='['+host+']'
        if parsed.port:host+=':'+str(parsed.port)
        url=urlunsplit((parsed.scheme,host,parsed.path,parsed.query,''))
    def emit(kind,raw):
        sys.stdout.buffer.write(kind+struct.pack('!I',len(raw))+raw);sys.stdout.buffer.flush()
    # Separate Python/Pillow process startup from the actual network deadline.
    emit(b'R',b'')
    opener=urllib.request.build_opener(_SafeRedirect())
    with opener.open(urllib.request.Request(url,headers=headers),timeout=timeout) as response:
        content_type=response.headers.get_content_type()
        if content_type.startswith('multipart/') or content_type in ('video/x-motion-jpeg','video/mjpeg','image/x-mjpeg'):
            parser=JpegBuffer();last=0.
            while True:
                chunk=response.read1(8192)
                if not chunk:break
                for raw in parser.feed(chunk):
                    now=time.monotonic()
                    if now-last>=.1:emit(b'M',raw);last=now
        else:
            length=response.headers.get('Content-Length')
            if length and int(length)>MAX_IMAGE_BYTES:raise ValueError('Image too large')
            raw=response.read(MAX_IMAGE_BYTES+1)
            decode_payload(raw)
            emit(b'S',raw)


class MediaViewer:
    def __init__(self,timeout=5.,max_fps=10.,stale_after=3.):
        self.timeout=min(30.,max(.2,float(timeout)));self.period=1/min(10.,max(1.,float(max_fps)))
        self.startup_timeout=min(15.,max(5.,self.timeout+5.))
        self.stale_after=max(.1,float(stale_after))
        self._lock=threading.RLock();self._generation=0;self._threads=[]
        self._stop=threading.Event();self._queue=queue.Queue(maxsize=1);self._process=None
        self._topic=None;self._mode=None;self._last_progress=0.;self._last_frame=0.;self._frame_count=0
        self._network_ready=False
        self._state={'sequence':0,'frame':None,'status':'stopped','error':None,'received_at':None,'fps':0.,
                     'capture_id':None,'recognition':None}

    def snapshot(self):
        # Published images are never mutated; callers should treat them read-only.
        with self._lock:return dict(self._state)

    def _status(self,generation,status,error=None):
        with self._lock:
            if generation!=self._generation or self._stop.is_set():return
            if self._state['status']!=status or self._state['error']!=error:
                self._state.update(status=status,error=error,fps=0.,sequence=self._state['sequence']+1)

    def _begin(self,mode):
        self.stop()
        with self._lock:
            self._generation+=1;generation=self._generation;self._mode=mode
            self._stop=threading.Event();self._queue=queue.Queue(maxsize=1)
            self._last_progress=time.monotonic();self._last_frame=0.;self._frame_count=0
            self._network_ready=False
            self._state.update(frame=None,status='waiting' if mode=='topic' else 'loading',error=None,
                               received_at=None,fps=0.,capture_id=None,recognition=None,sequence=self._state['sequence']+1)
        return generation,self._stop

    def start_topic(self,topic):
        if not isinstance(topic,str) or not topic:raise ValueError('请选择主题')
        generation,stop=self._begin('topic');self._topic=topic
        thread=threading.Thread(target=self._topic_loop,args=(generation,stop,self._queue),daemon=True,name='siot-media-topic')
        self._threads=[thread];thread.start()

    def submit_topic(self,topic,payload,received_at=None):
        with self._lock:
            if self._mode!='topic' or topic!=self._topic or self._stop.is_set():return
            if not isinstance(payload,bytes) or not 0<len(payload)<=MAX_PAYLOAD_BYTES:
                self._status(self._generation,'error','图片消息为空或超过 3 MiB');return
            now=time.time()
            if received_at is None:received_at=now
            try:
                valid=type(received_at) in (int,float) and math.isfinite(float(received_at)) and received_at>=0
            except (OverflowError,ValueError):valid=False
            if not valid:
                self._status(self._generation,'error','图片接收时间无效');return
            received_at=min(now,float(received_at))
            target=self._queue;item=(payload,received_at)
            try:target.put_nowait(item)
            except queue.Full:
                try:target.get_nowait()
                except queue.Empty:pass
                try:target.put_nowait(item)
                except queue.Full:pass

    def _accept(self,generation,payload,static=False,received_at=None):
        frame,metadata=_decode_frame(payload);now=time.monotonic();wall_now=time.time()
        stamp=wall_now if received_at is None else received_at
        age=max(0.,wall_now-stamp);frame_time=now-age
        with self._lock:
            if generation!=self._generation or self._stop.is_set():return
            previous_stamp=self._state['received_at']
            if received_at is not None and previous_stamp is not None and stamp<previous_stamp:return
            delta=frame_time-self._last_frame if self._last_frame else 0.
            self._frame_count+=1;self._last_frame=frame_time;self._last_progress=now
            self._network_ready=True
            status='image' if static or self._frame_count==1 else 'streaming'
            fps=0. if static or self._frame_count==1 or delta<=0 else min(10.,1/delta)
            error=None
            if received_at is not None and age>self.stale_after:
                status='stale';fps=0.;error='缓存画面，等待新的画面';self._frame_count=0
            self._state.update(frame=frame,status=status,error=error,received_at=stamp,fps=fps,
                               capture_id=metadata['capture_id'],recognition=metadata['recognition'],
                               sequence=self._state['sequence']+1)

    def _topic_loop(self,generation,stop,pending):
        next_frame=0.
        while not stop.is_set():
            wait=max(0.,next_frame-time.monotonic())
            if stop.wait(wait):break
            try:payload,received_at=pending.get(timeout=.05)
            except queue.Empty:
                if self._state['frame'] is not None and time.monotonic()-self._last_frame>self.stale_after:
                    self._status(generation,'stale','等待新的画面')
                continue
            try:self._accept(generation,payload,received_at=received_at)
            except Exception:self._status(generation,'error','无法解码图片，请检查消息格式或大小')
            next_frame=time.monotonic()+self.period

    def start_url(self,url):
        validate_url(url);scheme=urlsplit(url).scheme.lower()
        if scheme=='rtsp':
            executable='/usr/local/bin/ffmpeg' if Path('/usr/local/bin/ffmpeg').is_file() else shutil.which('ffmpeg')
            if not executable:raise ValueError('系统没有可用的视频解码器')
            command=[executable,'-nostdin','-hide_banner','-loglevel','error','-threads','1','-filter_threads','1','-rtsp_transport','tcp',
                     # This device's RTSP demuxer rejects rw_timeout. The parent
                     # watchdog bounds startup/read stalls and kills the child.
                     '-i',url,'-an','-sn','-dn',
                     '-vf','scale=400:280:force_original_aspect_ratio=decrease,fps=10',
                     '-f','image2pipe','-vcodec','mjpeg','-threads','1','pipe:1']
        else:
            command=[sys.executable,str(Path(__file__).resolve()),'--http-worker']
        generation,stop=self._begin('url')
        try:
            flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            process=subprocess.Popen(command,stdin=subprocess.DEVNULL if scheme=='rtsp' else subprocess.PIPE,stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,bufsize=0,**flags)
        except Exception:
            self._status(generation,'error','无法启动媒体连接');return
        self._process=process
        if scheme!='rtsp':
            try:
                process.stdin.write(json.dumps({'url':url,'timeout':self.timeout},ensure_ascii=False).encode('utf-8'))
                process.stdin.close()
            except (OSError,ValueError):
                self._terminate(process)
                for pipe in (process.stdin,process.stdout):
                    try:pipe.close()
                    except (OSError,ValueError):pass
                self._status(generation,'error','无法启动媒体连接');return
        reader=threading.Thread(target=self._url_loop,args=(generation,stop,process,scheme),daemon=True,name='siot-media-reader')
        watchdog=threading.Thread(target=self._watch,args=(generation,stop,process),daemon=True,name='siot-media-watch')
        self._threads=[reader,watchdog];reader.start();watchdog.start()

    @staticmethod
    def _terminate(process):
        if process is None:return
        if process.poll() is None:
            try:process.terminate()
            except OSError:pass
            try:process.wait(timeout=.4)
            except subprocess.TimeoutExpired:
                try:process.kill()
                except OSError:pass
                try:process.wait(timeout=.6)
                except subprocess.TimeoutExpired:pass

    def _watch(self,generation,stop,process):
        while not stop.wait(.05):
            if process.poll() is not None:return
            with self._lock:
                elapsed=time.monotonic()-self._last_progress
                limit=self.timeout if self._network_ready else self.startup_timeout
            if elapsed>limit:
                self._status(generation,'timeout','媒体连接超时或视频流已中断')
                self._terminate(process);return

    @staticmethod
    def _read_exact(pipe,count):
        result=bytearray()
        while len(result)<count:
            chunk=pipe.read(count-len(result))
            if not chunk:
                if not result:return b''
                raise ValueError('Incomplete media packet')
            result.extend(chunk)
        return bytes(result)

    def _url_loop(self,generation,stop,process,scheme):
        static=False;next_frame=0.;parser=JpegBuffer()
        try:
            while not stop.is_set():
                if scheme=='rtsp':
                    chunk=process.stdout.read(8192)
                    if not chunk:break
                    packets=[(False,raw) for raw in parser.feed(chunk)]
                else:
                    header=self._read_exact(process.stdout,5)
                    if not header:break
                    size=struct.unpack('!I',header[1:])[0]
                    if header[:1]==b'R' and size==0:
                        with self._lock:
                            if generation==self._generation:
                                self._network_ready=True;self._last_progress=time.monotonic()
                        continue
                    if header[:1] not in (b'S',b'M') or not 0<size<=MAX_IMAGE_BYTES:raise ValueError()
                    packets=[(header[:1]==b'S',self._read_exact(process.stdout,size))]
                for single,payload in packets:
                    now=time.monotonic()
                    if not single and now<next_frame:continue
                    self._accept(generation,payload,static=single);static=single
                    next_frame=time.monotonic()+self.period
            with self._lock:status=self._state['status'];has_frame=self._state['frame'] is not None
            if not stop.is_set() and status!='timeout':
                if not has_frame:self._status(generation,'error','未收到可显示的图片或视频')
                elif not static:self._status(generation,'ended','视频流已结束')
        except Exception:
            with self._lock:status=self._state['status']
            if not stop.is_set() and status!='timeout':self._status(generation,'error','媒体连接或解码失败')
        finally:
            self._terminate(process)
            try:process.stdout.close()
            except (OSError,AttributeError):pass

    def stop(self):
        with self._lock:
            self._generation+=1;self._stop.set();self._mode=None;self._topic=None
            self._state.update(frame=None,status='stopped',error=None,received_at=None,fps=0.,
                               capture_id=None,recognition=None,sequence=self._state['sequence']+1)
            process=self._process
        self._terminate(process)
        for thread in self._threads:
            if thread is not threading.current_thread():thread.join(timeout=1.)
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError('媒体处理尚未停止，请稍后重试')
        self._threads=[];self._process=None;self._queue=queue.Queue(maxsize=1)


if __name__=='__main__':
    if len(sys.argv)!=2 or sys.argv[1]!='--http-worker':raise SystemExit(2)
    try:
        raw=sys.stdin.buffer.read(16385)
        if len(raw)>16384:raise ValueError()
        settings=json.loads(raw)
        timeout=settings['timeout']
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not .2<=timeout<=30:raise ValueError()
        _http_worker(settings['url'],timeout)
    except Exception:raise SystemExit(2)
