"""Cooperative camera exclusivity for XiaoZhi and the SIoT preview worker.

The kernel releases this lock when a process exits. Third-party native Apps do
not participate; this is not a system-wide camera multiplexer.
"""
import os
from pathlib import Path
import tempfile

DEFAULT_PATH = Path('/run/maixcam2-camera.lock') if os.name == 'posix' else Path(tempfile.gettempdir())/'maixcam2-camera.lock'


class CameraLease:
    def __init__(self, path=DEFAULT_PATH):
        self.path=Path(path)
        self.fd=None

    def acquire(self):
        if self.fd is not None:raise RuntimeError('Camera lease is already held')
        fd=os.open(str(self.path),os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            if os.name=='nt':
                import msvcrt
                if os.fstat(fd).st_size==0:os.write(fd,b'0')
                os.lseek(fd,0,os.SEEK_SET)
                msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.fd=fd
            return self
        except OSError:
            os.close(fd)
            raise RuntimeError('Camera is busy; stop the other camera operation first') from None

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd=None

    def __enter__(self):return self.acquire()
    def __exit__(self,*unused):self.release()


def camera_is_busy(path=DEFAULT_PATH):
    try:
        with CameraLease(path):return False
    except (OSError,RuntimeError):
        # An unreadable lock must not be advertised as an available camera.
        return True
