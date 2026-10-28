import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SERVICE = Path(__file__).resolve().parents[1] / 'xiaozhi/service'
sys.path.insert(0, str(SERVICE))
from camera_ownership import CameraLease, camera_is_busy


class CameraOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'camera.lock'

    def test_exclusive_lease_and_release(self):
        self.assertFalse(camera_is_busy(self.path))
        with CameraLease(self.path):
            self.assertTrue(camera_is_busy(self.path))
            with self.assertRaises(RuntimeError):CameraLease(self.path).acquire()
        self.assertFalse(camera_is_busy(self.path))

    def test_exception_releases_lease(self):
        with self.assertRaises(ValueError):
            with CameraLease(self.path):raise ValueError('test')
        self.assertFalse(camera_is_busy(self.path))

    def test_dead_process_releases_without_stale_owner_cleanup(self):
        environment = os.environ.copy()
        environment['PYTHONPATH'] = str(SERVICE)
        source = 'from camera_ownership import CameraLease; import sys,time; lease=CameraLease(sys.argv[1]).acquire(); print("ready",flush=True); time.sleep(30)'
        child = subprocess.Popen([sys.executable, '-u', '-c', source, str(self.path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            self.assertTrue(camera_is_busy(self.path))
        finally:
            child.kill();child.wait(timeout=5)
            child.stdout.close();child.stderr.close()
        self.assertFalse(camera_is_busy(self.path))

    @unittest.skipUnless(os.name == 'posix', 'POSIX no-follow and permissions')
    def test_symlink_is_rejected_and_private_mode(self):
        with CameraLease(self.path):self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        link = self.path.with_name('link')
        link.symlink_to(self.path)
        with self.assertRaises(OSError):CameraLease(link).acquire()


if __name__ == '__main__':unittest.main()
