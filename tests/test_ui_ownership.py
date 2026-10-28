import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'xiaozhi/service'))
import ui_ownership


class UiOwnershipTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'owner.json'
        self.now = 0.
        self.processes = {101: '50', 202: '70'}
        self.reader = ui_ownership.UiLeaseReader(
            self.path, process_start=self.processes.get, clock=lambda: self.now)

    def record(self, **changes):
        value = {'version': 1, 'pid': 101, 'start_ticks': '50',
                 'lease_id': 'a' * 32, 'scopes': ['popup', 'touch_wake']}
        value.update(changes)
        self.path.write_text(json.dumps(value), encoding='utf-8')

    def test_live_owner_suppresses_only_requested_scopes(self):
        self.record(scopes=['popup'])
        self.assertTrue(self.reader.active('popup'))
        self.assertFalse(self.reader.active('touch_wake'))
        self.assertFalse(self.reader.active('voice'))

    def test_dead_owner_and_reused_pid_do_not_suppress_ui(self):
        self.record()
        self.assertTrue(self.reader.active('popup'))
        self.processes[101] = '51'
        self.now += .21
        self.assertFalse(self.reader.active('popup'))
        del self.processes[101]
        self.now += .21
        self.assertFalse(self.reader.active('popup'))

    def test_cache_expires_within_two_hundred_milliseconds(self):
        self.record()
        self.assertTrue(self.reader.active('popup'))
        self.path.unlink()
        self.now = .19
        self.assertTrue(self.reader.active('popup'))
        self.now = .20
        self.assertFalse(self.reader.active('popup'))

    def test_invalid_oversized_and_unknown_scope_records_are_ignored(self):
        invalid = [{'pid': True}, {'version': True}, {'start_ticks': '../x'},
                   {'lease_id': 'x'}, {'scopes': ['voice']}, {'scopes': 'popup'}]
        for changes in invalid:
            self.record(**changes)
            self.now += .21
            self.assertFalse(self.reader.active('popup'), changes)
        for text in ['{broken', ' ' * 4097]:
            self.path.write_text(text)
            self.now += .21
            self.assertFalse(self.reader.active('popup'))

    def lease(self, pid=101):
        return ui_ownership.UiLease(self.path, pid=pid, process_start=self.processes.get)

    def test_acquire_and_release_only_its_own_marker(self):
        with self.lease() as owner:
            self.assertTrue(self.reader.active('popup'))
            data = json.loads(self.path.read_text())
            self.assertEqual(data['lease_id'], owner.lease_id)
            if os.name == 'posix':
                self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.path.exists())

    def test_active_owner_is_not_replaced(self):
        self.record()
        with self.assertRaisesRegex(RuntimeError, 'owned'):
            self.lease(202).acquire()
        self.assertEqual(json.loads(self.path.read_text())['pid'], 101)

    def test_stale_owner_is_replaced_and_lock_blocks_second_writer(self):
        self.record(start_ticks='49')
        with self.lease() as first:
            with self.assertRaises(RuntimeError):
                self.lease(202).acquire()
            self.assertEqual(json.loads(self.path.read_text())['lease_id'], first.lease_id)

    def test_release_does_not_delete_another_owners_replacement(self):
        owner = self.lease().acquire()
        self.record(pid=202, start_ticks='70', lease_id='b' * 32)
        owner.release()
        self.assertEqual(json.loads(self.path.read_text())['pid'], 202)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux /proc process identity integration')
    def test_crashed_process_leaves_no_active_lease_and_lock_can_be_reused(self):
        environment = os.environ.copy()
        environment['PYTHONPATH'] = str(Path(ui_ownership.__file__).parent)
        code = ('from ui_ownership import UiLease; import sys,time; '
                'owner=UiLease(sys.argv[1]).acquire(); '
                'print("ready",flush=True); time.sleep(30)')
        child = subprocess.Popen([sys.executable, '-u', '-c', code, str(self.path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=environment)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            reader = ui_ownership.UiLeaseReader(self.path, clock=lambda: self.now)
            self.assertTrue(reader.active('popup'))
            child.kill()
            child.wait(timeout=3)
            self.now += .21
            self.assertFalse(reader.active('popup'))
            self.assertTrue(self.path.exists(), 'Crash should leave the marker for stale-owner recovery')
            with ui_ownership.UiLease(self.path):
                self.now += .21
                self.assertTrue(reader.active('popup'))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            child.stdout.close()
            child.stderr.close()


if __name__ == '__main__':
    unittest.main()
