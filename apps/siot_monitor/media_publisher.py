"""Best-effort JPEG MQTT stream; one local send and one replaceable frame.

``sent`` counts local QoS0 socket completion, not receipt by another device.
Encoding and network work run outside the UI. Each restart uses a new client
so a stopped stream cannot replay its frames on a later camera session.
"""
import base64
import io
import json
import math
import threading
import time
import uuid

from model import validate_publish


class MediaPublisher:
    MAX_JPEG_BYTES = 512 * 1024
    DEFAULT_FPS = 5

    def __init__(self, config, factory=None, wire_format='json'):
        self._validate_format(wire_format)
        self.config = dict(config)
        self._factory = factory
        self._condition = threading.Condition()
        self._lifecycle = threading.Lock()
        self._publish_gate = threading.Lock()
        self._active = False
        self._generation = 0
        self._connection_generation = 0
        self._client = None
        self._pending = None
        self._request_id = 0
        self._state = dict(connected=False, sent=0, dropped=0,
                           error=None, last_sequence=None, wire_format=wire_format,
                           target_fps=self.DEFAULT_FPS)

    @staticmethod
    def _validate_format(wire_format):
        if wire_format not in ('json', 'base64'):
            raise ValueError('媒体格式必须为json或base64')

    def set_format(self, wire_format):
        self._validate_format(wire_format)
        with self._lifecycle:
            with self._condition:
                if self._active:
                    raise RuntimeError('请先停止媒体发送，再切换格式')
                self._state['wire_format'] = wire_format

    def set_rate(self, fps):
        if type(fps) is not int or fps not in (1, 2, 5, 10):
            raise ValueError('媒体发送速度必须为1、2、5或10FPS')
        with self._lifecycle:
            with self._condition:
                if self._active:
                    raise RuntimeError('请先停止媒体发送，再切换速度')
                self._state['target_fps'] = fps

    def snapshot(self):
        # Only the short state lock: never wait for encoding, sockets or shutdown.
        with self._condition:
            return dict(self._state)

    def _new_client(self):
        if self._factory is not None:
            return self._factory()
        import paho.mqtt.client as mqtt
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                           client_id='maixmedia-' + uuid.uuid4().hex[:10])

    def start(self):
        with self._lifecycle:
            with self._condition:
                if self._active:
                    return
            client = self._new_client()
            client.username_pw_set(self.config['username'], self.config['password'])
            client.max_queued_messages_set(1)
            client.max_inflight_messages_set(1)
            client.reconnect_delay_set(min_delay=1, max_delay=15)
            with self._condition:
                self._generation += 1
                generation = self._generation
                self._client = client
                self._active = True
                self._state.update(connected=False, error=None)
                wire_format = self._state['wire_format']
                interval = 1.0 / self._state['target_fps']
            client.on_connect = lambda c, u, f, r, p: self._connection(
                generation, c, not r.is_failure)
            client.on_disconnect = lambda c, u, f, r, p: self._connection(
                generation, c, False)
            try:
                client.connect_async(self.config['host'], self.config.get('port', 1883), keepalive=30)
                client.loop_start()
            except Exception:
                with self._condition:
                    self._active = False
                    self._state.update(connected=False, error='媒体连接启动失败')
                threading.Thread(target=self._shutdown, args=(client,), daemon=True).start()
                raise RuntimeError('媒体连接启动失败') from None
            threading.Thread(target=self._run, args=(generation, client, wire_format, interval), daemon=True).start()

    def _connection(self, generation, client, connected):
        with self._condition:
            if not self._current(generation, client):
                return
            self._connection_generation += 1
            self._state['connected'] = connected
            self._state['error'] = None if connected else '媒体连接已断开，等待重连'
            if not connected and self._pending is not None:
                self._pending = None
                self._state['dropped'] += 1
            self._condition.notify_all()

    def _current(self, generation, client):
        return self._active and generation == self._generation and client is self._client

    def submit(self, topic, jpeg, sequence, timestamp, width, height):
        validate_publish(topic, 'media-frame')
        if (not isinstance(jpeg, bytes) or not 4 <= len(jpeg) <= self.MAX_JPEG_BYTES
                or not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9')):
            raise ValueError('JPEG数据无效或超过512KiB')
        if type(sequence) is not int or not 0 <= sequence < 2**63:
            raise ValueError('帧序号无效')
        if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                or timestamp < 0):
            raise ValueError('帧时间无效')
        if any(type(v) is not int or not 1 <= v <= 2048 for v in (width, height)):
            raise ValueError('图像尺寸无效')
        with self._condition:
            if not self._active:
                raise RuntimeError('媒体发送未启动')
            self._request_id += 1
            request_id = self._request_id
            if not self._state['connected']:
                self._state['dropped'] += 1
                raise RuntimeError('媒体尚未连接，本帧未发送')
            if self._pending is not None:
                self._state['dropped'] += 1
            self._pending = (request_id, topic, jpeg, sequence, timestamp, width, height)
            self._condition.notify_all()
            return request_id

    @staticmethod
    def _encode(frame, wire_format='json'):
        from PIL import Image
        _, _, jpeg, sequence, timestamp, width, height = frame
        with Image.open(io.BytesIO(jpeg)) as image:
            if image.format != 'JPEG' or image.size != (width, height):
                raise ValueError('JPEG尺寸与帧描述不一致')
            image.verify()
        encoded = base64.b64encode(jpeg)
        if wire_format == 'base64':
            # Verified Mind+ network-image widget expects a full Data URL;
            # raw Base64 overwrites the widget with a broken image.
            return b'data:image/jpeg;base64,' + encoded
        return json.dumps(dict(type='image', mime='image/jpeg', encoding='base64',
                               sequence=sequence, timestamp=timestamp,
                               width=width, height=height,
                               data=encoded.decode('ascii')),
                          separators=(',', ':'), allow_nan=False).encode('utf-8')

    def _run(self, generation, client, wire_format, interval):
        next_send = 0.
        while True:
            with self._condition:
                while self._current(generation, client):
                    delay = next_send - time.monotonic()
                    if self._state['connected'] and self._pending is not None and delay <= 0:
                        frame, self._pending = self._pending, None
                        connection_generation = self._connection_generation
                        break
                    self._condition.wait(timeout=max(.01, delay) if delay > 0 else .2)
                else:
                    return
            try:
                # The worker captures its session's format. A later restart
                # cannot change an old frame while encoding is in progress.
                payload = self._encode(frame, wire_format)
                # publish() is nonblocking; serialize its acceptance with stop().
                # No encoding or waiting for socket completion holds this gate.
                with self._publish_gate:
                    with self._condition:
                        valid = (self._current(generation, client)
                                 and connection_generation == self._connection_generation
                                 and self._state['connected'])
                    if not valid:
                        self._drop(generation)
                        continue
                    next_send = time.monotonic() + interval
                    info = client.publish(frame[1], payload, qos=0, retain=False)
                if int(info.rc) != 0:
                    raise RuntimeError('媒体连接未接受发送')
                # Keep one outstanding QoS0 packet: Paho's queued-message limit
                # applies to QoS1/2, so our worker also bounds QoS0 explicitly.
                while not info.is_published():
                    with self._condition:
                        if (not self._current(generation, client)
                                or connection_generation != self._connection_generation
                                or not self._state['connected']):
                            break
                    info.wait_for_publish(timeout=.05)
                with self._condition:
                    if not self._current(generation, client):
                        return
                    if (connection_generation == self._connection_generation
                            and self._state['connected'] and info.is_published()):
                        self._state['sent'] += 1
                        self._state['last_sequence'] = frame[3]
                        self._state['error'] = None
                    else:
                        self._state['dropped'] += 1
            except Exception:
                self._drop(generation, '媒体数据或连接异常，本帧未完成发送')

    def _drop(self, generation, error=None):
        with self._condition:
            if generation == self._generation:
                self._state['dropped'] += 1
                if error is not None:
                    self._state['error'] = error

    @staticmethod
    def _shutdown(client):
        try:
            client.disconnect()
        except Exception:
            pass
        finally:
            try:
                client.loop_stop()
            except Exception:
                pass

    def stop(self):
        with self._lifecycle:
            with self._publish_gate:
                with self._condition:
                    if not self._active:
                        return
                    self._active = False
                    self._generation += 1
                    self._state['connected'] = False
                    if self._pending is not None:
                        self._state['dropped'] += 1
                    self._pending = None
                    client, self._client = self._client, None
                    self._condition.notify_all()
            # loop_stop can wait on a network thread; never block touch on it.
            threading.Thread(target=self._shutdown, args=(client,), daemon=True).start()
