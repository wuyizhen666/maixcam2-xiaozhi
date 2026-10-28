"""Process-bound, volatile ownership of XiaoZhi popup/touch surfaces.

No persistent configuration is changed. A dead owner or recycled PID cannot
keep the UI suppressed. This module opens no Maix hardware.
"""
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

DEFAULT_PATH = '/run/xiaozhi-ui-owner.json'
SCOPES = frozenset(('popup', 'touch_wake'))


def process_start_ticks(pid):
    try:
        text = Path('/proc', str(pid), 'stat').read_text()
        fields = text[text.rfind(')') + 2:].split()
        if fields[0] in ('Z', 'X'):
            return None
        return fields[19]  # Linux proc stat field 22, after pid and comm.
    except (OSError, IndexError, ValueError):
        return None


def _read_record(path):
    try:
        with Path(path).open('r', encoding='utf-8') as handle:
            text = handle.read(4097)
        if len(text.encode('utf-8')) > 4096:
            return None
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        if type(data.get('version')) is not int or data['version'] != 1:
            return None
        if type(data.get('pid')) is not int or data['pid'] <= 1:
            return None
        ticks = data.get('start_ticks')
        identity = data.get('lease_id')
        scopes = data.get('scopes')
        if not isinstance(ticks, str) or not ticks.isascii() or not ticks.isdigit() or len(ticks) > 32:
            return None
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
            return None
        if not isinstance(scopes, list) or not scopes or len(scopes) > 2:
            return None
        if any(not isinstance(scope, str) or scope not in SCOPES for scope in scopes):
            return None
        return data
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def _is_live(data, process_start):
    return data is not None and process_start(data['pid']) == data['start_ticks']


class UiLeaseReader:
    def __init__(self, path=DEFAULT_PATH, *, process_start=process_start_ticks, clock=time.monotonic):
        self.path = Path(path)
        self.process_start = process_start
        self.clock = clock
        self._cache = (float('-inf'), frozenset())

    def active(self, scope='popup'):
        if scope not in SCOPES:
            return False
        now = self.clock()
        deadline, scopes = self._cache
        if now >= deadline:
            data = _read_record(self.path)
            scopes = frozenset(data['scopes']) if _is_live(data, self.process_start) else frozenset()
            self._cache = (now + .2, scopes)
        return scope in scopes


def _lock(path):
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b'0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise RuntimeError('UI is already owned by another app') from exc


class UiLease:
    def __init__(self, path=DEFAULT_PATH, *, pid=None, process_start=process_start_ticks):
        self.path = Path(path)
        self.pid = os.getpid() if pid is None else pid
        self.process_start = process_start
        self.lease_id = secrets.token_hex(16)
        self._lock_fd = None

    def acquire(self):
        if self._lock_fd is not None:
            raise RuntimeError('This lease is already acquired')
        ticks = self.process_start(self.pid)
        if ticks is None:
            raise RuntimeError('Cannot identify UI owner process')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fd = _lock(self.path.with_name(self.path.name + '.lock'))
        temporary = None
        try:
            if _is_live(_read_record(self.path), self.process_start):
                raise RuntimeError('UI is already owned by another app')
            data = {'version': 1, 'pid': self.pid, 'start_ticks': ticks,
                    'lease_id': self.lease_id, 'scopes': sorted(SCOPES)}
            descriptor, temporary = tempfile.mkstemp(prefix=self.path.name + '.', dir=str(self.path.parent))
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                os.chmod(temporary, 0o600)
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            return self
        except BaseException:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def release(self):
        if self._lock_fd is None:
            return
        try:
            data = _read_record(self.path)
            if data and data['pid'] == self.pid and data['lease_id'] == self.lease_id:
                self.path.unlink()
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *unused):
        self.release()
