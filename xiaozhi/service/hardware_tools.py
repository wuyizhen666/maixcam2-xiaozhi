"""On-demand MaixCAM2 camera/illumination tools; importing opens no hardware."""
import os
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import requests
from camera_ownership import CameraLease, camera_is_busy

MAX_JPEG_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
CAMERA_WARMUP_SECONDS = 2.0
CAMERA_WARMUP_MIN_FRAMES = 30
CAMERA_WARMUP_TIMEOUT = 5.0
LIGHT_OWNER = Path('/run/xiaozhi-light-owned')
PWM_CHANNEL = Path('/sys/class/pwm/pwmchip4/pwm2')  # MaixCAM2 PWM6


def force_light_off():
    """Stop hook: touch PWM6 only when this service claimed it."""
    if not LIGHT_OWNER.exists():
        return
    if PWM_CHANNEL.exists():
        (PWM_CHANNEL / 'duty_cycle').write_text('0')
        (PWM_CHANNEL / 'enable').write_text('0')
        (PWM_CHANNEL.parent / 'unexport').write_text('2')
        if PWM_CHANNEL.exists():
            raise RuntimeError('PWM6 release did not complete; ownership retained for retry')
    LIGHT_OWNER.unlink(missing_ok=True)


def _create_pwm():
    if PWM_CHANNEL.exists():
        raise RuntimeError('PWM6 is already in use; release it before controlling the light')
    from maix import pwm, pinmap
    LIGHT_OWNER.write_text(str(os.getpid()))
    LIGHT_OWNER.chmod(0o600)
    try:
        if int(pinmap.set_pin_function('B25', 'PWM6')) != 0:
            raise RuntimeError('Cannot assign B25 to PWM6')
        return pwm.PWM(6, freq=100000, duty=0, enable=True)
    except Exception:
        force_light_off()
        raise


def _capture_jpeg():
    with CameraLease():
        return _capture_jpeg_owned()


def _capture_jpeg_owned():
    from maix import camera
    cam = None
    try:
        cam = camera.Camera(640, 480)
        cam.exp_mode(camera.AeMode.Auto)
        cam.awb_mode(camera.AwbMode.Auto)
        # Cold-start AE/AWB can still be far from settled at frame 8. Drain
        # fresh frames for both a minimum frame count and real elapsed time.
        started = time.monotonic()
        deadline = started + CAMERA_WARMUP_TIMEOUT
        frames = 0
        while frames < CAMERA_WARMUP_MIN_FRAMES or time.monotonic() - started < CAMERA_WARMUP_SECONDS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Camera warm-up timed out')
            frame = cam.read(block_ms=min(3000, max(1, int(remaining * 1000))))
            if frame is None:
                raise RuntimeError('Camera returned no frame')
            frames += 1
            if time.monotonic() > deadline:
                raise RuntimeError('Camera warm-up timed out')
        with tempfile.TemporaryDirectory(prefix='xiaozhi-camera-') as directory:
            path = Path(directory) / 'camera.jpg'
            if int(frame.save(str(path), quality=80)) != 0:
                raise RuntimeError('JPEG encoding failed')
            with path.open('rb') as file:
                return file.read(MAX_JPEG_BYTES + 1)
    finally:
        if cam is not None:
            cam.close()


class HardwareTools:
    def __init__(self, device_id, client_id):
        self._device_id = device_id
        self._client_id = client_id
        self._vision_lock = threading.Lock()
        self._capture_lock = threading.Lock()
        self._light_lock = threading.RLock()
        self._vision = None
        self._generation = 0
        self._last_capture_bytes = 0
        self._last_upload_ok = None  # No upload has been attempted in this process.
        self._pwm = None
        self._last_brightness = 25

    def configure_vision(self, vision):
        value = None
        if isinstance(vision, dict) and isinstance(vision.get('url'), str):
            try:
                parsed = urlsplit(vision['url'])
                token = vision.get('token', '')
                # Official cloud currently advertises HTTP. Its same-host HTTPS
                # POST endpoint was verified on this device; never fall back to HTTP.
                if (parsed.scheme == 'http' and parsed.hostname == 'api.xiaozhi.me'
                        and parsed.port in (None, 80) and not parsed.username and not parsed.password):
                    parsed = parsed._replace(scheme='https', netloc=parsed.hostname)
                if (parsed.scheme == 'https' and parsed.hostname and
                        not parsed.username and not parsed.password and
                        isinstance(token, str) and '\r' not in token and '\n' not in token):
                    value = (parsed.geturl(), token)
            except ValueError:
                pass
        with self._vision_lock:
            self._vision = value
            self._generation += 1

    def camera_status(self):
        with self._vision_lock:
            return {'vision_configured': self._vision is not None,
                    'capture_in_progress': self._capture_lock.locked(),
                    'camera_busy': camera_is_busy(),
                    'last_capture_bytes': self._last_capture_bytes,
                    'last_upload_ok': self._last_upload_ok,
                    'width': 640, 'height': 480}

    def _check_generation(self, generation):
        with self._vision_lock:
            if generation != self._generation:
                raise RuntimeError('Camera request belongs to an ended session')

    def take_photo(self, question, on_capture=None):
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise ValueError('question must be non-empty text of at most 2000 characters')
        with self._vision_lock:
            vision, generation = self._vision, self._generation
        if vision is None:
            raise RuntimeError('Server has not provided an HTTPS vision capability')
        if not self._capture_lock.acquire(blocking=False):
            raise RuntimeError('Camera is busy; wait for the current request')
        try:
            jpeg = _capture_jpeg()
            if (not isinstance(jpeg, bytes) or len(jpeg) > MAX_JPEG_BYTES or
                    not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9')):
                raise RuntimeError('Camera produced an invalid or oversized JPEG')
            self._check_generation(generation)
            if on_capture is not None:
                on_capture(jpeg)
            url, token = vision
            headers = {'Device-Id': self._device_id(), 'Client-Id': self._client_id()}
            if token:
                headers['Authorization'] = 'Bearer ' + token
            with self._vision_lock:
                self._last_capture_bytes = len(jpeg)
                self._last_upload_ok = False
            try:
                with requests.post(url, headers=headers, data={'question': question},
                                   files={'file': ('camera.jpg', jpeg, 'image/jpeg')},
                                   timeout=(5, 20), allow_redirects=False, stream=True) as response:
                    if response.status_code != 200:
                        raise RuntimeError('Vision service HTTP %d' % response.status_code)
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=4096):
                        self._check_generation(generation)
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise RuntimeError('Vision response is too large')
            except requests.RequestException as exc:
                # Request exceptions may contain URLs or authorization details.
                raise RuntimeError('Vision request failed: ' + type(exc).__name__) from None
            self._check_generation(generation)
            text = body.decode('utf-8')
            if not text.strip():
                raise RuntimeError('Vision service returned an empty answer')
            try:
                answer = json.loads(text)
            except ValueError:
                answer = None
            if isinstance(answer, dict) and answer.get('success') is False:
                raise RuntimeError('Vision service reported analysis failure')
            with self._vision_lock:
                self._last_upload_ok = True
            return text
        finally:
            self._capture_lock.release()

    def light_status(self):
        with self._light_lock:
            if self._pwm is None:
                if PWM_CHANNEL.exists():
                    return {'owned': False, 'enabled': None, 'brightness': None,
                            'detail': 'PWM6 is controlled outside this service'}
                return {'owned': False, 'enabled': False, 'brightness': 0}
            duty = self._pwm.duty()
            if not 0 <= duty <= 100:
                raise RuntimeError('Cannot read light PWM duty')
            enabled = bool(self._pwm.is_enabled()) and duty > 0
            return {'owned': True, 'enabled': enabled,
                    'brightness': round(duty) if enabled else 0}

    def set_light_brightness(self, brightness):
        if type(brightness) is not int or not 0 <= brightness <= 100:
            raise ValueError('brightness must be an integer from 0 to 100')
        with self._light_lock:
            if self._pwm is None:
                if brightness == 0:
                    status = self.light_status()
                    if status['enabled'] is None:
                        raise RuntimeError('PWM6 is controlled outside this service')
                    return status
                self._pwm = _create_pwm()
            try:
                if brightness and int(self._pwm.enable()) != 0:
                    raise RuntimeError('Cannot enable light PWM')
                result = self._pwm.duty(brightness)
                if result < 0 or abs(result - brightness) > 1:
                    raise RuntimeError('Light brightness write failed')
                if brightness == 0 and int(self._pwm.disable()) != 0:
                    raise RuntimeError('Cannot disable light PWM')
                status = self.light_status()
                if status['brightness'] != brightness:
                    raise RuntimeError('Light brightness readback mismatch')
                if brightness:
                    self._last_brightness = brightness
                return status
            except Exception:
                self.close()
                raise

    def set_light_power(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        return self.set_light_brightness(self._last_brightness if enabled else 0)

    def close(self):
        with self._light_lock:
            try:
                if self._pwm is not None:
                    self._pwm.duty(0)
                    self._pwm.disable()
            finally:
                self._pwm = None
                force_light_off()


if __name__ == '__main__' and sys.argv[1:] == ['--lights-off']:
    force_light_off()
