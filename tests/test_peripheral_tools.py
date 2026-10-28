import importlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'xiaozhi' / 'service'))
try:
    peripheral = importlib.import_module('peripheral_tools')
except ModuleNotFoundError:
    peripheral = None


class BackgroundKeyTests(unittest.TestCase):
    def test_detaches_only_sdk_default_exit_listener(self):
        import sys, types
        import peripheral_tools as module
        self.assertTrue(hasattr(module, 'prepare_background_keys'))
        calls=[];maix=types.ModuleType('maix')
        maix.key=types.SimpleNamespace(rm_default_listener=lambda:calls.append('removed'))
        with patch.dict(sys.modules,{'maix':maix}),patch.object(module,'POWER_KEY_NODE',Path('/nonexistent-test-power-key')):
            module.prepare_background_keys()
        self.assertEqual(calls,['removed'])

    def test_shared_power_key_node_prevents_destructive_sdk_cleanup(self):
        import sys, types
        import peripheral_tools as module
        self.assertTrue(hasattr(module, 'prepare_background_keys'))
        calls=[];maix=types.ModuleType('maix')
        maix.key=types.SimpleNamespace(rm_default_listener=lambda:calls.append('removed'))
        with tempfile.TemporaryDirectory() as directory:
            node=Path(directory)/'powerkey';node.touch()
            with patch.dict(sys.modules,{'maix':maix}),patch.object(module,'POWER_KEY_NODE',node):
                with self.assertRaises(RuntimeError):module.prepare_background_keys()
        self.assertEqual(calls,[])


class PeripheralTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(peripheral, 'BoardPeripherals implementation is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for constant, relative in [('LED_PATH', 'led'), ('LED_MARKER', 'owned.json'),
                                   ('INPUT_CLASS', 'input'), ('INPUT_DEV', 'dev'),
                                   ('RTC_PATH', 'rtc'), ('ADC_PATH', 'adc')]:
            target = self.root / relative
            if constant != 'LED_MARKER':
                target.mkdir()
            patcher = patch.object(peripheral, constant, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write('led/trigger', 'none [activity] timer')
        self.write('led/brightness', '17')
        self.write('led/max_brightness', '255')
        self.board = peripheral.BoardPeripherals()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_constructor_and_import_do_not_access_hardware(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('hardware read')), \
                patch.object(os, 'open', side_effect=AssertionError('hardware open')):
            importlib.reload(peripheral)
            peripheral.BoardPeripherals()

    def test_led_uses_max_and_preserves_original_across_repeated_control(self):
        on = self.board.set_led_power(True)
        self.assertEqual(on['brightness'], 255)
        self.assertEqual(on['trigger'], 'none')
        marker = (self.root / 'owned.json').read_text()
        self.board.set_led_power(False)
        self.assertEqual((self.root / 'led/brightness').read_text(), '0')
        self.assertEqual((self.root / 'owned.json').read_text(), marker)
        self.board.restore_led()
        self.assertEqual((self.root / 'led/brightness').read_text(), '17')
        self.assertEqual((self.root / 'led/trigger').read_text(), 'activity')
        self.assertFalse((self.root / 'owned.json').exists())

    def test_led_marker_is_private_on_posix(self):
        self.board.set_led_power(True)
        if os.name == 'posix':
            self.assertEqual((self.root / 'owned.json').stat().st_mode & 0o777, 0o600)
        self.board.close()
        self.assertFalse((self.root / 'owned.json').exists())

    def test_led_validates_exact_boolean_without_writes(self):
        for enabled in (1, 0, None, 'true'):
            with self.assertRaises(ValueError):
                self.board.set_led_power(enabled)
        self.assertFalse((self.root / 'owned.json').exists())
        self.assertEqual(self.board.led_status()['trigger'], 'activity')

    def test_led_missing_node_is_an_error(self):
        (self.root / 'led/max_brightness').unlink()
        with self.assertRaises(RuntimeError):
            self.board.set_led_power(True)
        self.assertFalse((self.root / 'owned.json').exists())

    def test_other_instance_cannot_restore_or_replace_ownership(self):
        self.board.set_led_power(True)
        other = peripheral.BoardPeripherals()
        with self.assertRaises(RuntimeError):
            other.set_led_power(False)
        other.close()
        self.assertEqual(self.board.led_status()['brightness'], 255)
        self.assertTrue((self.root / 'owned.json').exists())
        self.board.close()

    def test_cli_restore_accepts_our_valid_marker_after_restart(self):
        self.board.set_led_power(True)
        self.assertEqual(peripheral.main(['--restore-led']), 0)
        self.assertFalse((self.root / 'owned.json').exists())
        self.assertEqual(self.board.led_status()['trigger'], 'activity')

    def test_malformed_or_foreign_markers_never_touch_led(self):
        for text in ('garbage', '{}', '{"owner":"other"}',
                     '{"owner":"xiaozhi-user-led","version":1,"trigger":"../../evil"}'):
            self.write('owned.json', text)
            with self.assertRaises(RuntimeError):
                peripheral.restore_user_led()
            with self.assertRaises(RuntimeError):
                self.board.set_led_power(False)
            self.assertEqual(self.board.led_status()['brightness'], 17)
            self.assertEqual((self.root / 'owned.json').read_text(), text)

    def test_failed_restore_preserves_marker_for_retry(self):
        self.board.set_led_power(True)
        (self.root / 'led/brightness').unlink()
        (self.root / 'led/brightness').mkdir()
        with self.assertRaises(RuntimeError):
            self.board.restore_led()
        self.assertTrue((self.root / 'owned.json').exists())
        (self.root / 'led/brightness').rmdir()
        self.write('led/brightness', '255')
        self.board.restore_led()
        self.assertFalse((self.root / 'owned.json').exists())

    def test_failed_led_write_attempts_original_state_restore(self):
        real_write = Path.write_text
        def fail_on_power(path, text, *args, **kwargs):
            if path == self.root / 'led/brightness' and text == '255':
                raise OSError('write failed')
            return real_write(path, text, *args, **kwargs)
        with patch.object(Path, 'write_text', fail_on_power):
            with self.assertRaises(RuntimeError):
                self.board.set_led_power(True)
        self.assertEqual(self.board.led_status()['trigger'], 'activity')
        self.assertEqual(self.board.led_status()['brightness'], 17)
        self.assertFalse((self.root / 'owned.json').exists())

    def test_rtc_epoch_utc_and_delta(self):
        self.write('rtc/since_epoch', '1704067200\n')
        self.write('rtc/name', 'rtc-test\n')
        with patch.object(peripheral.time, 'time', return_value=1704067202.5):
            result = self.board.rtc_time()
        self.assertEqual(result['utc'], '2024-01-01T00:00:00Z')
        self.assertEqual(result['name'], 'rtc-test')
        self.assertEqual(result['system_epoch'], 1704067202.5)
        self.assertEqual(result['rtc_minus_system_seconds'], -2.5)

    def test_adc_returns_raw_counts_and_rejects_invalid_channel(self):
        self.write('adc/in_voltage3_raw', '32767\n')
        self.assertEqual(self.board.adc_read_raw(3), {'channel': 3, 'raw': 32767, 'unit': 'count'})
        for channel in (-1, 4, True, '0', 0.5):
            with self.assertRaises(ValueError):
                self.board.adc_read_raw(channel)
        with self.assertRaises(RuntimeError):
            self.board.adc_read_raw(0)

    def test_missing_rtc_and_buttons_are_unavailable(self):
        with self.assertRaises(RuntimeError):
            self.board.rtc_time()
        self.assertFalse(self.board.start()['available'])
        with self.assertRaises(RuntimeError):
            self.board.button_state()
        with self.assertRaises(RuntimeError):
            self.board.button_events()
        self.board.close()

    def test_button_limit_validation(self):
        for value in (0, 65, True, '1', 1.2):
            with self.assertRaises(ValueError):
                self.board.button_events(value)

    def button_io(self):
        self.write('input/event0/device/name', 'unrelated\n')
        self.write('input/event2/device/name', 'gpio_keys\n')
        left, right = socket.socketpair()
        left.setblocking(False)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        original_open, original_close, original_read = os.open, os.close, os.read
        flags_seen = []
        self.closed_fds = []
        def open_input(path, flags, *args):
            if Path(path) == self.root / 'dev/event2':
                flags_seen.append(flags)
                return left.fileno()
            return original_open(path, flags, *args)
        def ioctl(fd, request, buffer):
            self.assertEqual(fd, left.fileno())
            number = request & 255
            self.assertIn(number, (0x21, 0x18))
            buffer[431 // 8] |= 1 << (431 % 8)
            return 0
        def close_input(fd):
            self.closed_fds.append(fd)
            if fd != left.fileno():
                original_close(fd)
        def read_input(fd, count):
            return left.recv(count) if fd == left.fileno() else original_read(fd, count)
        for patcher in (patch.object(os, 'open', open_input), patch.object(os, 'close', close_input),
                        patch.object(os, 'read', read_input), patch.object(peripheral, '_ioctl', ioctl)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return left, right, flags_seen

    def wait_events(self, count):
        deadline = time.monotonic() + 2
        result = None
        while time.monotonic() < deadline:
            result = self.board.button_events(64)
            if result['sequence'] >= count:
                return result
            time.sleep(0.01)
        self.fail('listener did not consume events: %r' % result)

    def test_current_button_state_uses_kernel_bitmap_and_nonexclusive_read(self):
        left, right, flags = self.button_io()
        result = self.board.button_state()
        self.assertEqual(result['supported_codes'], [431])
        self.assertEqual(result['pressed_codes'], [431])
        self.assertEqual(result['device'], str(self.root / 'dev/event2'))
        self.assertTrue(flags)
        self.assertTrue(all(value & 3 == os.O_RDONLY for value in flags))
        self.assertEqual(self.closed_fds, [left.fileno()])

    def test_listener_parses_partial_aarch64_events_and_closes_thread(self):
        left, right, flags = self.button_io()
        self.assertTrue(self.board.start()['available'])
        event = struct.pack('<qqHHi', 123, 456000, 1, 431, 1)
        right.sendall(event[:7])
        right.sendall(event[7:] + struct.pack('<qqHHi', 124, 0, 1, 431, 0))
        result = self.wait_events(2)
        self.assertEqual([e['value'] for e in result['events']], [1, 0])
        self.assertEqual(result['events'][0]['timestamp'], 123.456)
        self.assertEqual(result['events'][0]['code'], 431)
        self.assertEqual(result['events'][1]['sequence'], 2)
        started = time.monotonic()
        self.board.close()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(self.board._thread.is_alive())
        self.assertEqual(self.closed_fds, [left.fileno()])
        self.board.close()
        self.assertEqual(self.closed_fds, [left.fileno()])

    def test_disconnected_reader_reports_failure_and_closes_owned_fd(self):
        left, right, _ = self.button_io()
        self.assertTrue(self.board.start()['available'])
        right.close()
        self.board._thread.join(timeout=2)
        with self.assertRaisesRegex(RuntimeError, 'disconnected'):
            self.board.button_events()
        self.board.close()
        self.assertEqual(self.closed_fds, [left.fileno()])

    def test_event_overflow_and_syn_dropped_are_reported(self):
        left, right, _ = self.button_io()
        self.assertTrue(self.board.start()['available'])
        try:
            right.sendall(b''.join(struct.pack('<qqHHi', index, 0, 1, 431, 1) for index in range(70)))
            result = self.wait_events(70)
            self.assertEqual(len(result['events']), 64)
            self.assertEqual(result['dropped_events'], 6)
            right.sendall(struct.pack('<qqHHi', 71, 0, 0, 3, 0)
                          + struct.pack('<qqHHi', 72, 0, 1, 431, 0)
                          + struct.pack('<qqHHi', 73, 0, 0, 0, 0))
            self.wait_events(72)
            result = self.board.button_events(2)
            self.assertEqual([e['kind'] for e in result['events']], ['sync_lost', 'resync'])
            self.assertEqual(result['events'][-1]['pressed_codes'], [431])
            self.assertEqual(result['sync_losses'], 1)
        finally:
            self.board.close()


if __name__ == '__main__':
    unittest.main()
