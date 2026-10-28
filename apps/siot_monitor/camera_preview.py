"""Nonblocking snapshots from an isolated, bounded Maix camera worker."""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

MAX_JPEG_BYTES=512*1024
STALE_SECONDS=3.


def clean_environment(environment):
    return {key:value for key,value in environment.items()
            if key!='LD_PRELOAD' and not key.startswith('MAIXCAM2_')}


def empty_snapshot(status='stopped',error=None):
    return dict(sequence=0,frame=None,jpeg=None,status=status,error=error,
                received_at=None,fps=0.,width=320,height=240)


class CameraPreview:
    def __init__(self,worker_path=None,runtime_root=None):
        self.worker_path=Path(worker_path) if worker_path else Path(__file__).with_name('camera_worker.py')
        self.runtime_root=Path(runtime_root) if runtime_root else (Path('/run') if os.name=='posix' else Path(tempfile.gettempdir()))
        self._lock=threading.Lock()
        self._operation_lock=threading.RLock()
        self._value=empty_snapshot()
        self._process=None
        self._thread=None
        self._stop=threading.Event()
        self._directory=None
        self._generation=0

    def snapshot(self):
        with self._lock:return dict(self._value)

    def _set(self,generation,value):
        with self._lock:
            if generation==self._generation:self._value=value

    def start(self):
        with self._operation_lock:
            if self._process is not None and self._process.poll() is None and self.snapshot()['status'] in ('starting','warming','live'):
                return
            self.stop()
            if self._process is not None:raise RuntimeError('Camera worker is still stopping')
            self._generation+=1;generation=self._generation
            self._stop=threading.Event()
            self._directory=Path(tempfile.mkdtemp(prefix='siot-camera-',dir=str(self.runtime_root)))
            os.chmod(self._directory,0o700)
            self._set(generation,empty_snapshot('starting'))
            try:
                self._process=subprocess.Popen([sys.executable,'-u',str(self.worker_path),
                    '--directory',str(self._directory),'--parent-pid',str(os.getpid())],
                    env=clean_environment(os.environ),stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            except Exception:
                self._set(generation,empty_snapshot('error','无法启动摄像头工作进程'))
                self._cleanup_directory()
                return
            self._thread=threading.Thread(target=self._watch,args=(generation,self._process,self._directory,self._stop),daemon=True)
            self._thread.start()

    def _read_state(self,directory):
        with (directory/'state.json').open('rb') as handle:raw=handle.read(4097)
        if len(raw)>4096:raise ValueError('Invalid camera metadata size')
        state=json.loads(raw)
        if not isinstance(state,dict):raise ValueError('Invalid camera metadata')
        return state

    def _watch(self,generation,process,directory,stop):
        last_sequence=0;started=time.monotonic()
        while not stop.wait(.04):
            try:
                state=self._read_state(directory)
            except FileNotFoundError:
                state=None
            except Exception:
                self._set(generation,empty_snapshot('error','摄像头状态数据无效'))
                self._terminate_worker(process)
                return
            if process.poll() is not None:
                message='摄像头正被其他功能使用' if state and state.get('error')=='busy' else '摄像头工作进程已停止'
                self._set(generation,empty_snapshot('error',message))
                return
            if state is None or state.get('status') in ('starting','warming'):
                if time.monotonic()-started>18:
                    self._set(generation,empty_snapshot('error','摄像头启动超时'))
                    self._terminate_worker(process)
                    return
                elif state is not None:self._set(generation,empty_snapshot(state['status']))
                continue
            if state.get('status')=='error':
                message='摄像头正被其他功能使用' if state.get('error')=='busy' else '摄像头预览失败'
                self._set(generation,empty_snapshot('error',message))
                self._terminate_worker(process)
                return
            try:
                sequence=state.get('sequence');stamp=state.get('received_at');fps=state.get('fps')
                if (state.get('status')!='live' or type(sequence) is not int or sequence<1 or
                    type(stamp) not in (int,float) or not math.isfinite(stamp) or
                    not -1<=time.time()-stamp<=STALE_SECONDS or
                    type(fps) not in (int,float) or not math.isfinite(fps) or not 0<=fps<=100 or
                    state.get('frame_file') not in ('frame-0.jpg','frame-1.jpg') or
                    state.get('width')!=320 or state.get('height')!=240 or
                    type(state.get('bytes')) is not int or not 0<state['bytes']<=MAX_JPEG_BYTES):
                    raise ValueError('Invalid or stale camera metadata')
                if sequence==last_sequence:continue
                with (directory/state['frame_file']).open('rb') as handle:jpeg=handle.read(MAX_JPEG_BYTES+1)
                if len(jpeg)!=state['bytes'] or hashlib.sha256(jpeg).hexdigest()!=state.get('sha256'):
                    # The double slot can advance while a slow reader decodes.
                    continue
                with Image.open(io.BytesIO(jpeg)) as source:
                    if source.format!='JPEG' or source.size!=(320,240):raise ValueError('Invalid camera dimensions')
                    source.load();frame=source.convert('RGB')
                value=dict(sequence=sequence,frame=frame,jpeg=jpeg,status='live',error=None,
                           received_at=float(stamp),fps=float(fps),width=320,height=240)
                self._set(generation,value);last_sequence=sequence
            except Exception:
                self._set(generation,empty_snapshot('error','摄像头画面无效或已超时'))
                self._terminate_worker(process)
                return

    @staticmethod
    def _terminate_worker(process):
        if process.poll() is not None:return True
        try:process.terminate()
        except ProcessLookupError:return True
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:process.kill()
            except ProcessLookupError:return True
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:return False
        return True

    def _cleanup_directory(self):
        if self._directory is None:return
        directory=self._directory.resolve()
        # The only recursively removed path is the exact private directory this
        # controller created, directly under its explicitly configured root.
        if directory.parent!=self.runtime_root.resolve() or not directory.name.startswith('siot-camera-'):
            raise RuntimeError('Unsafe camera runtime cleanup path')
        shutil.rmtree(directory)
        self._directory=None

    def stop(self):
        with self._operation_lock:
            self._generation+=1;self._stop.set()
            process=self._process
            if process is not None and not self._terminate_worker(process):
                with self._lock:self._value=empty_snapshot('error','摄像头尚未释放，请稍后重试停止')
                return
            if self._thread is not None:
                self._thread.join(timeout=1)
                if self._thread.is_alive():
                    with self._lock:self._value=empty_snapshot('error','摄像头读取线程尚未退出')
                    return
            self._process=None;self._thread=None
            self._cleanup_directory()
            with self._lock:self._value=empty_snapshot()
