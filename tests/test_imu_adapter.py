import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'xiaozhi/service'))
try:
    module=importlib.import_module('imu_adapter')
except ModuleNotFoundError:
    module=None

def sample(acc=(0,9.80665,0),gyro=(1,2,3)):
    return types.SimpleNamespace(acc=types.SimpleNamespace(x=acc[0],y=acc[1],z=acc[2]),
                                 gyro=types.SimpleNamespace(x=gyro[0],y=gyro[1],z=gyro[2]),temp=0)

class Sensor:
    def __init__(self,values):self.values=iter(values);self.calls=[]
    def read_all(self,**kwargs):self.calls.append(kwargs);return next(self.values)

class ImuTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module,'IMU adapter missing')
        import tempfile
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'calibration.json'

    def test_read_preserves_physical_units_and_does_not_invent_temperature(self):
        sensor=Sensor([sample()])
        with patch.object(module,'_open_sensor',return_value=sensor),patch.object(module.time,'sleep'):
            value=module.ImuReader(calibration_path=self.path).read()
        self.assertEqual(value['acceleration_m_s2'],{'x':0.,'y':9.80665,'z':0.})
        self.assertEqual(value['angular_velocity_deg_s'],{'x':1.,'y':2.,'z':3.})
        self.assertIsNone(value['temperature_c'])
        self.assertFalse(value['gyro_calibrated'])
        self.assertEqual(sensor.calls,[{'calib_gryo':False,'radian':False}])

    def test_zero_no_data_samples_are_retried_not_reported(self):
        sensor=Sensor([sample((0,0,0),(0,0,0)),sample()])
        with patch.object(module,'_open_sensor',return_value=sensor),patch.object(module.time,'sleep'):
            result=module.ImuReader(calibration_path=self.path).read()
        self.assertGreater(result['acceleration_m_s2']['y'],9)
        self.assertEqual(len(sensor.calls),2)

    def test_missing_fresh_data_has_finite_retry_budget(self):
        sensor=Sensor([sample((0,0,0),(0,0,0))]*100)
        with patch.object(module,'_open_sensor',return_value=sensor),patch.object(module.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'fresh|新'):module.ImuReader(calibration_path=self.path).read()
        self.assertLessEqual(len(sensor.calls),25)

    def test_nonfinite_data_is_an_error(self):
        sensor=Sensor([sample((float('nan'),1,0))])
        with patch.object(module,'_open_sensor',return_value=sensor),patch.object(module.time,'sleep'):
            with self.assertRaises(RuntimeError):module.ImuReader(calibration_path=self.path).read()

    def test_sensor_is_lazy_and_reused(self):
        sensor=Sensor([sample(),sample()])
        with patch.object(module,'_open_sensor',return_value=sensor) as factory,patch.object(module.time,'sleep'):
            reader=module.ImuReader(calibration_path=self.path);factory.assert_not_called()
            reader.read();reader.read();self.assertEqual(factory.call_count,1)
            reader.close()

    def test_constructor_uses_explicit_chip_and_supported_ranges(self):
        expected={}
        def build(driver,**kwargs):expected.update(driver=driver,**kwargs);return object()
        imu=types.SimpleNamespace(IMU=build,Mode=types.SimpleNamespace(DUAL='dual'),
            AccScale=types.SimpleNamespace(ACC_SCALE_2G='2g'),AccOdr=types.SimpleNamespace(ACC_ODR_104='104a'),
            GyroScale=types.SimpleNamespace(GYRO_SCALE_250DPS='250dps'),GyroOdr=types.SimpleNamespace(GYRO_ODR_104='104g'))
        parent=types.ModuleType('maix');ext=types.ModuleType('maix.ext_dev');ext.imu=imu;parent.ext_dev=ext
        with patch.dict(sys.modules,{'maix':parent,'maix.ext_dev':ext}):module._open_sensor()
        self.assertEqual(expected,{'driver':'lsm6dsowtr','i2c_bus':1,'addr':0x6b,'mode':'dual',
            'acc_scale':'2g','acc_odr':'104a','gyro_scale':'250dps','gyro_odr':'104g','block':True})



class Clock:
    def __init__(self): self.now = 0.
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds

class CalibrationTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'calibration.json'
        self.clock = Clock()
        for name in ('monotonic', 'sleep'):
            p = patch.object(module.time, name, getattr(self.clock, name))
            p.start(); self.addCleanup(p.stop)
        self.reader = module.ImuReader(calibration_path=self.path, device_id='board-a')
        self.sensor = Sensor([sample(gyro=(.7,-.04,-2.3))] * 1000)
        p = patch.object(module, '_open_sensor', return_value=self.sensor)
        p.start(); self.addCleanup(p.stop)

    def test_stable_bias_persists_reloads_and_applies_exactly_once(self):
        result = self.reader.calibrate_gyro()
        self.assertTrue(result['gyro_calibrated'])
        self.assertGreaterEqual(result['sample_count'], 100)
        self.assertLessEqual(result['sample_count'], 400)
        self.assertGreaterEqual(self.clock.now, 3.)
        self.assertLess(self.clock.now, 4.)
        self.assertTrue(self.path.exists())
        other = module.ImuReader(calibration_path=self.path, device_id='board-a')
        value = other.read()
        self.assertTrue(value['gyro_calibrated'])
        self.assertIsNone(value['temperature_c'])
        for axis in 'xyz': self.assertAlmostEqual(value['angular_velocity_deg_s'][axis], 0.)
        self.assertEqual(value['raw_angular_velocity_deg_s']['z'], -2.3)
        self.assertTrue(all(call == {'calib_gryo':False,'radian':False} for call in self.sensor.calls))

    def test_motion_rejected_preserving_previous_calibration(self):
        previous = self.reader.calibrate_gyro()
        content = self.path.read_bytes()
        for values in ([sample(gyro=(12,0,0))] * 400,
                       [sample(gyro=(i%2,0,0)) for i in range(400)],
                       [sample(acc=(0,2,0))] * 400,
                       [sample(acc=(i%2,9.80665,0)) for i in range(400)]):
            self.reader._sensor = Sensor(values)
            with self.assertRaisesRegex(RuntimeError, 'motion|stationary'):
                self.reader.calibrate_gyro()
            self.assertEqual(self.reader.get_calibration(), previous)
            self.assertEqual(self.path.read_bytes(), content)

    def test_bad_files_and_device_mismatch_are_not_loaded(self):
        import json
        self.reader.calibrate_gyro()
        valid = json.loads(self.path.read_text())
        for content in ('{broken', '[]', json.dumps(dict(valid,device_id='other')),
                        json.dumps(dict(valid,version=999)), json.dumps(dict(valid,model='other')),
                        json.dumps(dict(valid,bias_deg_s={'x':float('nan'),'y':0,'z':0}))):
            self.path.write_text(content)
            other = module.ImuReader(calibration_path=self.path,device_id='board-a')
            self.assertFalse(other.get_calibration()['gyro_calibrated'])

    def test_zero_samples_and_nonfinite_have_finite_budget(self):
        for value in (sample((0,0,0),(0,0,0)), sample(gyro=(float('nan'),0,0))):
            self.reader._sensor = Sensor([value] * 1000)
            before = self.clock.now
            with self.assertRaises(RuntimeError): self.reader.calibrate_gyro()
            self.assertLessEqual(len(self.reader._sensor.calls), 400)
            self.assertLess(self.clock.now-before, 4.)
            self.assertFalse(self.reader.get_calibration()['gyro_calibrated'])

    def test_busy_rejects_immediately(self):
        self.reader._lock.acquire()
        try:
            for operation in (self.reader.read,self.reader.calibrate_gyro,
                              self.reader.clear_calibration,self.reader.get_calibration):
                with self.assertRaisesRegex(RuntimeError,'busy'): operation()
        finally: self.reader._lock.release()

    def test_write_failure_keeps_old_state_and_file(self):
        previous = self.reader.calibrate_gyro()
        content = self.path.read_bytes()
        self.reader._sensor = Sensor([sample(gyro=(1,2,3))]*400)
        with patch.object(module.os, 'replace', side_effect=OSError('disk failed')):
            with self.assertRaises(OSError): self.reader.calibrate_gyro()
        self.assertEqual(previous, self.reader.get_calibration())
        self.assertEqual(content, self.path.read_bytes())
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_clear_removes_only_owned_file_and_is_idempotent(self):
        self.reader.calibrate_gyro()
        sdk_file = self.path.parent/'sdk_calibration'
        sdk_file.write_text('untouched')
        self.assertFalse(self.reader.clear_calibration()['gyro_calibrated'])
        self.assertFalse(self.path.exists())
        self.assertTrue(sdk_file.exists())
        self.assertFalse(self.reader.clear_calibration()['gyro_calibrated'])
        self.assertFalse(self.reader.read()['gyro_calibrated'])

    def test_calibration_discards_invalid_samples_but_requires_enough_fresh_data(self):
        invalid = [sample((0,0,0),(0,0,0)), sample(gyro=(float('inf'),0,0))]
        self.reader._sensor = Sensor((invalid + [sample(gyro=(.7,-.04,-2.3))]*3)*100)
        result = self.reader.calibrate_gyro()
        self.assertTrue(result['gyro_calibrated'])
        self.assertGreaterEqual(result['sample_count'], 100)
        previous = self.reader.get_calibration()
        self.reader._sensor = Sensor([sample()]*20 + [invalid[0]]*400)
        with self.assertRaisesRegex(RuntimeError,'fresh'): self.reader.calibrate_gyro()
        self.assertEqual(previous,self.reader.get_calibration())

    def test_malformed_metadata_and_huge_numbers_are_not_trusted(self):
        import json
        self.reader.calibrate_gyro()
        valid = json.loads(self.path.read_text())
        for changes in ({'calibrated_at_unix':10**1000}, {'calibrated_at_unix':True},
                        {'bias_deg_s':{'x':10**1000,'y':0,'z':0}},
                        {'bias_deg_s':{'x':True,'y':0,'z':0}},
                        {'bias_deg_s':{'x':'0','y':0,'z':0}},
                        {'sample_count':True}, {'sample_count':0}, {'schema':'unknown'}):
            with self.subTest(changes=changes):
                self.path.write_text(json.dumps(dict(valid,**changes)))
                other = module.ImuReader(calibration_path=self.path,device_id='board-a')
                self.assertFalse(other.get_calibration()['gyro_calibrated'])
        self.path.write_text('['*2000 + ']'*2000)
        other = module.ImuReader(calibration_path=self.path,device_id='board-a')
        self.assertFalse(other.get_calibration()['gyro_calibrated'])

    def test_clear_failure_keeps_calibration(self):
        previous = self.reader.calibrate_gyro()
        with patch.object(Path,'unlink',side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError): self.reader.clear_calibration()
        self.assertEqual(previous,self.reader.get_calibration())

    def test_status_is_detached_and_never_opens_hardware(self):
        with patch.object(module,'_open_sensor') as factory:
            self.assertFalse(self.reader.get_calibration()['gyro_calibrated'])
            factory.assert_not_called()
        result = self.reader.calibrate_gyro()
        result['bias_deg_s']['x'] = 999
        self.assertAlmostEqual(self.reader.get_calibration()['bias_deg_s']['x'],.7)
        self.assertNotIn('device_id',result)

if __name__ == '__main__':
    unittest.main()
