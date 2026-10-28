import importlib
import io
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'xiaozhi/service'))
try:cleanup=importlib.import_module('service_cleanup')
except ModuleNotFoundError:cleanup=None

class CleanupTests(unittest.TestCase):
    def setUp(self):self.assertIsNotNone(cleanup,'combined stop cleanup missing')
    def test_light_failure_does_not_skip_user_led_restore(self):
        calls=[]
        def light():calls.append('light');raise OSError('driver unavailable')
        def led():calls.append('led');return {'restored':True}
        with patch.object(cleanup,'force_light_off',side_effect=light),patch.object(cleanup,'restore_user_led',side_effect=led),redirect_stdout(io.StringIO()):
            self.assertEqual(cleanup.main(),1)
        self.assertEqual(calls,['light','led'])
    def test_successful_cleanup_returns_zero(self):
        with patch.object(cleanup,'force_light_off',return_value=None),patch.object(cleanup,'restore_user_led',return_value={'restored':False}),redirect_stdout(io.StringIO()):
            self.assertEqual(cleanup.main(),0)

if __name__=='__main__':unittest.main()
