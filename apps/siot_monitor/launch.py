#!/usr/bin/env python3
"""Own only the foreground UI while supervising the SIoT monitor process."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def _parent_death_signal(parent_pid):
    # Avoid leaving the GUI behind if the lightweight supervisor crashes.
    import ctypes
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot set parent-death signal')
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


def main():
    if any(key == 'LD_PRELOAD' or key.startswith('MAIXCAM2_') for key in os.environ):
        clean = {key: value for key, value in os.environ.items()
                 if key != 'LD_PRELOAD' and not key.startswith('MAIXCAM2_')}
        os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:], clean)
    directory = Path(__file__).resolve().parent
    # Installer can place the pure helper beside this launcher. The deployed
    # XiaoZhi service directory and repository layout are fallback locations.
    sys.path.extend(['/root/xiaozhi', str(directory.parents[1] / 'xiaozhi/service')])
    from ui_ownership import UiLease

    child = None
    stop_at = None

    def stop(signum, frame):
        nonlocal stop_at
        if stop_at is None:
            stop_at = time.monotonic()
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with UiLease():
        try:
            if stop_at is not None:
                return 0
            parent_pid = os.getpid()
            child = subprocess.Popen([sys.executable, '-u', str(directory / 'main.py')],
                                     cwd=str(directory),
                                     preexec_fn=lambda: _parent_death_signal(parent_pid))
            if stop_at is not None:
                child.terminate()
            while child.poll() is None:
                if stop_at is not None and time.monotonic() - stop_at > 10:
                    child.kill()
                time.sleep(.1)
            return child.returncode
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == '__main__':
    # The native App runner needs successful script completion; SystemExit(0)
    # produced its code:-1 error dialog on the tested MaixCAM2 launcher.
    result = main()
    if result:
        raise RuntimeError('SIoT monitor exited with status {}'.format(result))
