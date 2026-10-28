"""Fixed MaixCAM2 peripherals. Importing/constructing performs no hardware I/O.

Buttons use a separate, read-only evdev reader; no grab, injection, or Maix Key
object is used. Linux aarch64 input_event has two 64-bit timeval fields.
"""
import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import select
import struct
import threading
import time
import uuid

LED_PATH = Path('/sys/class/leds/sys-heartbeat')
LED_MARKER = Path('/run/xiaozhi-user-led.json')
INPUT_CLASS = Path('/sys/class/input')
INPUT_DEV = Path('/dev/input')
POWER_KEY_NODE = Path('/dev/input/powerkey')
RTC_PATH = Path('/sys/class/rtc/rtc0')
ADC_PATH = Path('/sys/bus/iio/devices/iio:device0')
INPUT_EVENT = struct.Struct('<qqHHi')
KEY_BYTES = 96  # KEY_MAX=0x2ff, including board Func key code 431.
_LED_LOCK = threading.RLock()


def prepare_background_keys():
    """Main-thread SDK initialization, then detach this daemon's exit callback.

    Other processes keep their own listeners. The installed SDK's Key destructor
    can unlink a shared powerkey node, so refuse that unverified configuration.
    """
    from maix import key
    if POWER_KEY_NODE.exists():
        raise RuntimeError('Shared powerkey node exists; cannot safely remove SDK exit listener')
    key.rm_default_listener()


def _read_integer(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError) as error:
        raise RuntimeError('Cannot read integer from %s: %s' % (path, error)) from error


def _led_status():
    try:
        triggers = (LED_PATH / 'trigger').read_text().strip().split()
        active = [value[1:-1] for value in triggers if value.startswith('[') and value.endswith(']')]
        # A single unbracketed trigger is also useful for simple sysfs fixtures.
        trigger = active[0] if len(active) == 1 else (triggers[0] if len(triggers) == 1 else None)
        maximum = _read_integer(LED_PATH / 'max_brightness')
        brightness = _read_integer(LED_PATH / 'brightness')
        if not trigger or not re.fullmatch(r'[A-Za-z0-9_-]+', trigger):
            raise RuntimeError('Cannot identify active user LED trigger')
        if maximum <= 0 or not 0 <= brightness <= maximum:
            raise RuntimeError('Invalid user LED brightness range')
        return {'available': True, 'trigger': trigger, 'brightness': brightness,
                'max_brightness': maximum, 'controlled': LED_MARKER.exists()}
    except OSError as error:
        raise RuntimeError('User LED unavailable: %s' % error) from error


def _load_led_marker():
    try:
        if LED_MARKER.is_symlink():
            raise ValueError('symlink marker is not owned')
        with LED_MARKER.open() as stream:
            text = stream.read(4097)
        if len(text) > 4096:
            raise ValueError('oversized marker')
        marker = json.loads(text)
        if (not isinstance(marker, dict) or marker.get('owner') != 'xiaozhi-user-led'
                or marker.get('version') != 1 or marker.get('led') != str(LED_PATH)
                or not isinstance(marker.get('token'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', marker['token'])
                or not isinstance(marker.get('trigger'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]+', marker['trigger'])
                or type(marker.get('brightness')) is not int
                or marker['brightness'] < 0):
            raise ValueError('invalid owner, target, or saved state')
        return marker
    except (OSError, ValueError) as error:
        raise RuntimeError('Invalid user LED ownership marker; retained: %s' % error) from error


def restore_user_led(owner_token=None):
    """Restore our saved LED state. A token limits normal cleanup to its owner.

    The stop-hook CLI omits the token to recover this service's valid marker
    after a process exit. Invalid markers never authorize a hardware write.
    """
    with _LED_LOCK:
        if not LED_MARKER.exists() and not LED_MARKER.is_symlink():
            return {'restored': False, 'reason': 'not_owned'}
        marker = _load_led_marker()
        if owner_token is not None and marker['token'] != owner_token:
            raise RuntimeError('User LED marker belongs to another owner')
        try:
            maximum = _read_integer(LED_PATH / 'max_brightness')
            if not 0 <= marker['brightness'] <= maximum:
                raise RuntimeError('Saved LED brightness exceeds current maximum')
            (LED_PATH / 'trigger').write_text('none')
            (LED_PATH / 'brightness').write_text(str(marker['brightness']))
            (LED_PATH / 'trigger').write_text(marker['trigger'])
            status = _led_status()
            if (status['trigger'] != marker['trigger'] or
                    (marker['trigger'] == 'none' and status['brightness'] != marker['brightness'])):
                raise RuntimeError('User LED restore readback mismatch')
            # Trigger-driven brightness can change immediately; its trigger is
            # the restoration criterion, not an instantaneous activity sample.
            LED_MARKER.unlink()
            return {'restored': True, 'trigger': marker['trigger']}
        except (OSError, RuntimeError) as error:
            raise RuntimeError('User LED restore failed; marker retained: %s' % error) from error


def _ioctl(fd, request, buffer):
    import fcntl  # Lazy import permits offline Windows test/import workflows.
    return fcntl.ioctl(fd, request, buffer, True)


def _key_bitmap(fd, number):
    buffer = bytearray(KEY_BYTES)
    # Linux _IOR('E', number, byte[KEY_BYTES]); never EVIOCGRAB/EVIOCS*.
    _ioctl(fd, (2 << 30) | (KEY_BYTES << 16) | (ord('E') << 8) | number, buffer)
    return [code for code in range(KEY_BYTES * 8) if buffer[code // 8] & (1 << (code % 8))]


def _open_button_device():
    try:
        matches = []
        for entry in sorted(INPUT_CLASS.glob('event*')):
            name = entry / 'device' / 'name'
            if name.is_file() and name.read_text().strip() == 'gpio_keys':
                matches.append(INPUT_DEV / entry.name)
        if len(matches) != 1:
            raise RuntimeError('Expected one gpio_keys device; found %s' % len(matches))
        path = matches[0]
        fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
        try:
            supported = _key_bitmap(fd, 0x21)  # EVIOCGBIT(EV_KEY)
            if not supported:
                raise RuntimeError('gpio_keys exposes no supported key codes')
            return fd, str(path), supported
        except Exception:
            os.close(fd)
            raise
    except (OSError, ImportError) as error:
        raise RuntimeError('gpio_keys unavailable: %s' % error) from error


class BoardPeripherals:
    def __init__(self):
        self._led_token = None
        self._lifecycle_lock = threading.RLock()
        self._events_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._fd = None
        self._device = None
        self._supported = []
        self._button_error = 'Button listener has not started'
        self._events = deque(maxlen=64)
        self._sequence = 0
        self._dropped = 0
        self._sync_losses = 0
        self._desynchronized = False
        self._pending = b''

    def led_status(self):
        with _LED_LOCK:
            return _led_status()

    def set_led_power(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        with _LED_LOCK:
            status = _led_status()
            if LED_MARKER.exists() or LED_MARKER.is_symlink():
                saved = _load_led_marker()
                if saved['token'] != self._led_token:
                    raise RuntimeError('User LED already owned; restore saved state before taking control')
            else:
                token = uuid.uuid4().hex
                marker = {'owner': 'xiaozhi-user-led', 'version': 1, 'led': str(LED_PATH),
                          'token': token, 'trigger': status['trigger'], 'brightness': status['brightness']}
                try:
                    fd = os.open(str(LED_MARKER), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w') as stream:
                        json.dump(marker, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    self._led_token = token
                except OSError as error:
                    raise RuntimeError('Cannot save user LED ownership: %s' % error) from error
            try:
                expected = status['max_brightness'] if enabled else 0
                (LED_PATH / 'trigger').write_text('none')
                (LED_PATH / 'brightness').write_text(str(expected))
                result = _led_status()
                if result['trigger'] != 'none' or result['brightness'] != expected:
                    raise RuntimeError('User LED control readback mismatch')
                return result
            except (OSError, RuntimeError) as error:
                try:
                    self.restore_led()
                except RuntimeError as restore_error:
                    raise RuntimeError('%s; %s' % (error, restore_error)) from error
                raise RuntimeError('User LED control failed; original state restored: %s' % error) from error

    def restore_led(self):
        with _LED_LOCK:
            if self._led_token is None:
                return {'restored': False, 'reason': 'not_owned'}
            result = restore_user_led(self._led_token)
            self._led_token = None
            return result

    def rtc_time(self):
        epoch = _read_integer(RTC_PATH / 'since_epoch')
        system_epoch = time.time()
        try:
            return {'epoch': epoch, 'utc': datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'name': (RTC_PATH / 'name').read_text().strip(), 'system_epoch': system_epoch,
                    'rtc_minus_system_seconds': epoch - system_epoch}
        except (OSError, ValueError, OverflowError) as error:
            raise RuntimeError('RTC unavailable: %s' % error) from error

    def adc_read_raw(self, channel):
        if type(channel) is not int or not 0 <= channel <= 3:
            raise ValueError('channel must be an integer from 0 to 3')
        return {'channel': channel, 'raw': _read_integer(ADC_PATH / ('in_voltage%d_raw' % channel)), 'unit': 'count'}

    def button_state(self):
        fd, device, supported = _open_button_device()
        try:
            return {'available': True, 'device': device, 'supported_codes': supported,
                    'pressed_codes': [code for code in _key_bitmap(fd, 0x18) if code in supported]}
        except OSError as error:
            raise RuntimeError('Cannot read gpio_keys state: %s' % error) from error
        finally:
            os.close(fd)

    def start(self):
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return {'available': True, 'device': self._device}
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            try:
                self._fd, self._device, self._supported = _open_button_device()
            except RuntimeError as error:
                self._button_error = str(error)
                return {'available': False, 'error': self._button_error}
            self._stop.clear()
            self._pending = b''
            self._desynchronized = False
            self._button_error = None
            self._thread = threading.Thread(target=self._listen, name='xiaozhi-button-events', daemon=True)
            self._thread.start()
            return {'available': True, 'device': self._device}

    def _append_event(self, event):
        with self._events_lock:
            self._sequence += 1
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
            event['sequence'] = self._sequence
            self._events.append(event)

    def _consume_events(self, data):
        self._pending += data
        while len(self._pending) >= INPUT_EVENT.size:
            seconds, microseconds, kind, code, value = INPUT_EVENT.unpack_from(self._pending)
            self._pending = self._pending[INPUT_EVENT.size:]
            timestamp = seconds + microseconds / 1000000.0
            if kind == 0 and code == 3:  # SYN_DROPPED
                self._desynchronized = True
                self._sync_losses += 1
                self._append_event({'kind': 'sync_lost', 'timestamp': timestamp})
            elif self._desynchronized:
                if kind == 0 and code == 0:  # SYN_REPORT: discard through this boundary.
                    pressed = [key for key in _key_bitmap(self._fd, 0x18) if key in self._supported]
                    self._append_event({'kind': 'resync', 'timestamp': timestamp, 'pressed_codes': pressed})
                    self._desynchronized = False
            elif kind == 1 and code in self._supported and value in (0, 1, 2):
                self._append_event({'kind': 'key', 'timestamp': timestamp, 'code': code, 'value': value,
                                    'action': {0: 'release', 1: 'press', 2: 'repeat'}[value]})

    def _listen(self):
        try:
            while not self._stop.is_set():
                readable, _, _ = select.select([self._fd], [], [], 0.1)
                if not readable:
                    continue
                try:
                    data = os.read(self._fd, INPUT_EVENT.size * 64)
                except BlockingIOError:
                    continue
                if not data:
                    raise RuntimeError('gpio_keys disconnected')
                self._consume_events(data)
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            self._button_error = 'Button listener failed: %s' % error

    def button_events(self, limit=10):
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError('limit must be an integer from 1 to 64')
        with self._events_lock:
            if self._button_error:
                raise RuntimeError(self._button_error)
            if self._thread is None or not self._thread.is_alive():
                raise RuntimeError('Button listener is not running')
            return {'available': True, 'events': [dict(event) for event in list(self._events)[-limit:]],
                    'sequence': self._sequence, 'dropped_events': self._dropped,
                    'sync_losses': self._sync_losses, 'synchronized': not self._desynchronized}

    def close(self):
        with self._lifecycle_lock:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=1.0)
                if self._thread.is_alive():
                    # Do not close/reuse an fd while another thread may read it.
                    self.restore_led()
                    raise RuntimeError('Button listener did not stop in time')
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self._button_error = 'Button listener is closed'
            self.restore_led()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore-led', action='store_true', required=True)
    parser.parse_args(argv)
    try:
        print(json.dumps(restore_user_led()))
        return 0
    except RuntimeError as error:
        print(json.dumps({'error': str(error)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
