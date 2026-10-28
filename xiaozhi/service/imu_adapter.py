"""MaixCAM2 IMU reader with application-owned, stationary gyro calibration."""
import contextlib
import json
import math
import os
from pathlib import Path
import socket
import tempfile
import threading
import time


MODEL = 'LSM6DSOWTR'
CALIBRATION_SCHEMA = 'xiaozhi.imu.gyro-calibration'
CALIBRATION_VERSION = 1
CALIBRATION_SECONDS = 3.0
MIN_SAMPLES = 100
MAX_SAMPLES = 400


def _open_sensor():
    from maix.ext_dev import imu
    return imu.IMU('lsm6dsowtr', i2c_bus=1, addr=0x6B, mode=imu.Mode.DUAL,
                   acc_scale=imu.AccScale.ACC_SCALE_2G, acc_odr=imu.AccOdr.ACC_ODR_104,
                   gyro_scale=imu.GyroScale.GYRO_SCALE_250DPS,
                   gyro_odr=imu.GyroOdr.GYRO_ODR_104, block=True)


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


class ImuReader:
    def __init__(self, calibration_path='/root/xiaozhi/imu_gyro_calibration.json', device_id=None):
        self._sensor = None
        self._lock = threading.Lock()
        self._calibration_path = Path(calibration_path)
        self._device_id = device_id if device_id is not None else socket.gethostname()
        self._calibration = None
        self._calibration_loaded = False

    @contextlib.contextmanager
    def _exclusive(self, wait=False):
        if not (self._lock.acquire(timeout=.5) if wait else self._lock.acquire(blocking=False)):
            raise RuntimeError('IMU is busy; wait for the current operation')
        try:
            yield
        finally:
            self._lock.release()

    def _initialize(self):
        if self._sensor is None:
            self._sensor = _open_sensor()
            time.sleep(.12)

    def _sample(self):
        # The SDK's own bias correction is deliberately disabled, including
        # during calibration: this module subtracts its own bias exactly once.
        data = self._sensor.read_all(calib_gryo=False, radian=False)
        acc = [float(data.acc.x), float(data.acc.y), float(data.acc.z)]
        gyro = [float(data.gyro.x), float(data.gyro.y), float(data.gyro.z)]
        if not all(math.isfinite(v) for v in acc + gyro):
            raise ValueError('IMU returned non-finite data')
        # This SDK driver uses all-zero output for "no new sample".
        return (acc, gyro) if any(v != 0 for v in acc + gyro) else None

    def _load_calibration(self):
        if self._calibration_loaded:
            return
        self._calibration_loaded = True
        try:
            with self._calibration_path.open('r', encoding='utf-8') as handle:
                raw = handle.read(8193)
            if len(raw) > 8192:
                return
            data = json.loads(raw)
            if not isinstance(data, dict):
                return
            bias = data.get('bias_deg_s')
            if (data.get('schema') != CALIBRATION_SCHEMA or
                    type(data.get('version')) is not int or
                    data['version'] != CALIBRATION_VERSION or
                    data.get('model') != MODEL or
                    data.get('device_id') != self._device_id or
                    not isinstance(bias, dict) or set(bias) != set('xyz') or
                    not all(_finite_number(v) and abs(v) <= 10 for v in bias.values()) or
                    not _finite_number(data.get('calibrated_at_unix')) or
                    data['calibrated_at_unix'] <= 0 or
                    type(data.get('sample_count')) is not int or
                    not MIN_SAMPLES <= data['sample_count'] <= MAX_SAMPLES):
                return
            # Retain only validated fields; unknown file content is never exposed.
            self._calibration = {
                'bias_deg_s': {axis: float(bias[axis]) for axis in 'xyz'},
                'calibrated_at_unix': data['calibrated_at_unix'],
                'sample_count': data['sample_count'],
            }
        except (OSError, ValueError, UnicodeError, OverflowError, RecursionError):
            pass

    def _status(self):
        result = {'model': MODEL, 'gyro_calibrated': self._calibration is not None}
        if self._calibration is not None:
            result.update(self._calibration)
            result['bias_deg_s'] = dict(self._calibration['bias_deg_s'])
        return result

    def get_calibration(self):
        """Return safe metadata without opening the hardware."""
        with self._exclusive(wait=True):
            self._load_calibration()
            return self._status()

    def _save_calibration(self, calibration):
        path = self._calibration_path
        path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(calibration, schema=CALIBRATION_SCHEMA,
                      version=CALIBRATION_VERSION, model=MODEL, device_id=self._device_id)
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                os.chmod(temporary, 0o600)
                json.dump(record, handle, allow_nan=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def calibrate_gyro(self):
        """Measure stationary raw bias over 3 seconds and persist atomically.

        Keep the device on a stable surface. These checks reject obvious motion,
        but cannot distinguish steady rotation about gravity from a gyro bias.
        No SDK calibration APIs or SDK calibration files are used.
        """
        with self._exclusive():
            self._load_calibration()
            self._initialize()
            started = time.monotonic()
            samples = []
            for _ in range(MAX_SAMPLES):
                if time.monotonic() - started >= CALIBRATION_SECONDS:
                    break
                try:
                    value = self._sample()
                except ValueError:
                    value = None
                if value is not None:
                    samples.append(value)
                remaining = CALIBRATION_SECONDS - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(min(.012, remaining))
            if len(samples) < MIN_SAMPLES or time.monotonic() - started < CALIBRATION_SECONDS:
                raise RuntimeError('IMU did not provide enough fresh samples during calibration')
            acc_axes = list(zip(*(acc for acc, _ in samples)))
            gyro_axes = list(zip(*(gyro for _, gyro in samples)))
            means = [sum(axis) / len(axis) for axis in gyro_axes]
            def std(axis):
                mean = sum(axis) / len(axis)
                return math.sqrt(sum((v - mean) ** 2 for v in axis) / len(axis))
            if (any(abs(math.sqrt(sum(v*v for v in acc)) - 9.80665) > 1.0 for acc, _ in samples) or
                    any(std(axis) > .2 for axis in acc_axes) or
                    any(std(axis) > .3 for axis in gyro_axes) or
                    any(abs(mean) > 10 for mean in means)):
                raise RuntimeError('IMU motion detected; keep the device stationary on a stable surface')
            calibration = {'bias_deg_s': dict(zip('xyz', means)),
                           'calibrated_at_unix': time.time(), 'sample_count': len(samples)}
            self._save_calibration(calibration)
            self._calibration = calibration
            return self._status()

    def clear_calibration(self):
        """Delete only the application-owned file; a deletion failure retains bias."""
        with self._exclusive():
            try:
                self._calibration_path.unlink()
            except FileNotFoundError:
                pass
            self._calibration = None
            self._calibration_loaded = True
            return self._status()

    def read(self):
        with self._exclusive():
            self._load_calibration()
            self._initialize()
            for _ in range(25):
                try:
                    value = self._sample()
                except ValueError as error:
                    raise RuntimeError(str(error)) from error
                if value is not None:
                    acc, raw_gyro = value
                    bias = self._calibration['bias_deg_s'] if self._calibration else dict.fromkeys('xyz', 0.)
                    gyro = [v - bias[axis] for axis, v in zip('xyz', raw_gyro)]
                    return {'model': MODEL, 'acceleration_m_s2': dict(zip('xyz', acc)),
                            'angular_velocity_deg_s': dict(zip('xyz', gyro)),
                            'raw_angular_velocity_deg_s': dict(zip('xyz', raw_gyro)),
                            'temperature_c': None, 'gyro_calibrated': self._calibration is not None,
                            'timestamp_unix': time.time()}
                time.sleep(.012)
            raise RuntimeError('IMU did not provide a fresh sample')

    def close(self):
        with self._lock:
            self._sensor = None
