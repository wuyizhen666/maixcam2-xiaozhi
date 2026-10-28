"""Isolated Maix camera producer. Never opens display, microphone or NPU."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

MAX_JPEG_BYTES=512*1024
WIDTH,HEIGHT=320,240
TARGET_FPS=10


def atomic_bytes(path,data):
    temporary=path.with_name(path.name+'.new')
    fd=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as handle:handle.write(data)
    os.replace(temporary,path)


def write_state(directory,**fields):
    atomic_bytes(directory/'state.json',json.dumps(fields,allow_nan=False).encode('utf-8'))


def publish_frame(directory,jpeg,sequence,received_at,fps):
    if not isinstance(jpeg,bytes) or len(jpeg)>MAX_JPEG_BYTES or not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):
        raise ValueError('Camera JPEG is invalid or oversized')
    name='frame-%d.jpg'%(sequence%2)
    atomic_bytes(directory/name,jpeg)
    write_state(directory,status='live',sequence=sequence,received_at=received_at,
                fps=fps,width=WIDTH,height=HEIGHT,frame_file=name,bytes=len(jpeg),
                sha256=hashlib.sha256(jpeg).hexdigest())


def parent_death_signal(expected_pid):
    import ctypes
    # SIGKILL also covers a parent dying while native camera init/read is stuck.
    if ctypes.CDLL(None,use_errno=True).prctl(1,signal.SIGKILL,0,0,0)!=0:
        raise OSError('Cannot set camera parent-death signal')
    if os.getppid()!=expected_pid:raise RuntimeError('Camera parent already exited')


def read_available(cam,stop,deadline):
    """Retry only transient empty buffers within a caller-owned deadline."""
    while not stop.is_set():
        remaining=deadline-time.monotonic()
        if remaining<=0:raise RuntimeError('Camera frame timeout')
        try:
            frame=cam.read(block_ms=min(1000,max(1,int(remaining*1000))))
        except RuntimeError as exc:
            message=str(exc).strip().lstrip(':').strip()
            if message!='Buffer empty' and not message.startswith('Buffer empty:'):
                raise
            frame=None
        if stop.is_set():return None
        if time.monotonic()>deadline:raise RuntimeError('Camera frame timeout')
        if frame is not None:return frame
        # Some backends return None immediately. Avoid a busy spin while still
        # allowing a stop request to interrupt the retry delay.
        if stop.wait(min(.02,max(0.,deadline-time.monotonic()))):return None
    return None


def warmup_camera(cam,stop):
    started=time.monotonic();deadline=started+8.;frames=0
    while frames<30 or time.monotonic()-started<2.:
        if read_available(cam,stop,deadline) is None:return False
        frames+=1
    return True


def drain_ready(cam,first,stop,max_frames=4,budget_s=.008):
    """Choose the last immediately available frame within bounded read attempts.

    The budget gates starting another nonblocking SDK call, not its native
    execution time. This is a queue-draining experiment, not a sensor-age claim.
    """
    deadline=time.monotonic()+budget_s
    latest=first
    first=None
    for _ in range(max_frames):
        if stop.is_set():return None
        if time.monotonic()>=deadline:break
        try:
            candidate=cam.read(block=False)
        except RuntimeError as exc:
            message=str(exc).strip().lstrip(':').strip()
            if message!='Buffer empty' and not message.startswith('Buffer empty:'):
                latest=None
                raise
            break
        if stop.is_set():return None
        if candidate is None:break
        latest=candidate
        candidate=None
    return None if stop.is_set() else latest


def run(directory,parent_pid):
    parent_death_signal(parent_pid)
    directory=Path(directory)
    if directory.is_symlink():raise ValueError('Invalid camera runtime directory')
    directory=directory.resolve(strict=True)
    if directory.parent not in (Path('/run'),Path('/tmp')) or not directory.name.startswith('siot-camera-'):
        raise ValueError('Camera frames must remain in the volatile runtime directory')
    state=directory.stat()
    if state.st_uid!=os.geteuid() or state.st_mode & 0o077:
        raise ValueError('Camera runtime directory is not private')
    stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *unused:stop.set())
    signal.signal(signal.SIGINT,lambda *unused:stop.set())
    sys.path.extend(['/root/xiaozhi',str(Path(__file__).resolve().parents[2]/'xiaozhi/service')])
    from camera_ownership import CameraLease
    write_state(directory,status='starting',sequence=0)
    try:
        with CameraLease():
            from maix import camera,image
            cam=frame=encoded=None
            try:
                if stop.is_set():return 0
                cam=camera.Camera(WIDTH,HEIGHT,image.Format.FMT_RGB888)
                cam.exp_mode(camera.AeMode.Auto)
                cam.awb_mode(camera.AwbMode.Auto)
                write_state(directory,status='warming',sequence=0)
                if not warmup_camera(cam,stop):return 0
                sequence=0;window=time.monotonic();count=0;fps=0.;next_frame=window
                last_frame=window
                while not stop.is_set():
                    if os.getppid()!=parent_pid:break
                    if stop.wait(max(0.,next_frame-time.monotonic())):break
                    frame=read_available(cam,stop,last_frame+3.)
                    if frame is None:break
                    frame=drain_ready(cam,frame,stop)
                    if frame is None:break
                    last_frame=time.monotonic()
                    # Host extraction completion, not sensor exposure/optical age.
                    captured=time.time()
                    # Official MaixPy to_jpeg returns an Image; to_bytes(copy=True)
                    # gives independent storage before the native image is released.
                    encoded=frame.to_jpeg(quality=75)
                    jpeg=bytes(encoded.to_bytes(copy=True))
                    frame=encoded=None
                    sequence+=1;count+=1;now=time.monotonic()
                    if now-window>=1:
                        fps=count/(now-window);window=now;count=0
                    publish_frame(directory,jpeg,sequence,captured,fps)
                    next_frame=max(next_frame+1/TARGET_FPS,now)
            finally:
                frame=encoded=None
                if cam is not None:cam.close()
        write_state(directory,status='stopped',sequence=0)
        return 0
    except Exception as exc:
        # Native/log exceptions can contain paths; expose only a bounded code.
        code='busy' if isinstance(exc,RuntimeError) and str(exc).startswith('Camera is busy') else 'camera_failed'
        write_state(directory,status='error',sequence=0,error=code)
        return 1


def main():
    if any(key=='LD_PRELOAD' or key.startswith('MAIXCAM2_') for key in os.environ):
        clean={key:value for key,value in os.environ.items() if key!='LD_PRELOAD' and not key.startswith('MAIXCAM2_')}
        os.execve(sys.executable,[sys.executable,os.path.abspath(__file__)]+sys.argv[1:],clean)
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',required=True)
    parser.add_argument('--parent-pid',required=True,type=int)
    args=parser.parse_args()
    return run(args.directory,args.parent_pid)


if __name__=='__main__':raise SystemExit(main())
