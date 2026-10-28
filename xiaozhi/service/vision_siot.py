"""Best-effort original-photo/recognition relay, independent of cloud vision.

No photo files or payload logs are written. QoS0 ``local_sent`` means local
socket completion; QoS1 ``broker_ack`` does not promise receiver display or
SIoT database persistence. Missing SIoT configuration never raises into vision.
"""
import base64
from collections import OrderedDict, deque
import io
import json
from pathlib import Path
import threading
import time
import uuid


class VisionSiotBridge:
    MAX_JPEG = 2 * 1024 * 1024
    MAX_ENVELOPE = 3 * 1024 * 1024
    MAX_TEXT = 256 * 1024
    MAX_QUESTION = 8 * 1024
    IMAGE_TTL = 300
    STATUS_TTL = 900

    def __init__(self, config_path='/etc/siot/monitor.json', factory=None):
        self._path = config_path
        self._factory = factory
        self._condition = threading.Condition()
        self._lifecycle = threading.Lock()
        self._publish_gate = threading.Lock()
        self._generation = 0
        self._connection_generation = 0
        self._active = False
        self._configured = False
        self._connected = False
        self._config = None
        self._client = None
        self._queue = deque()
        self._records = OrderedDict()
        self._images = OrderedDict()
        self._pending = {}
        self._early_acks = OrderedDict()
        self._error = None
        self._dropped = 0
        self._last_capture = None

    @staticmethod
    def _topic(value):
        if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 128
                or any(c in value for c in '+#') or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError('Invalid topic')
        return value

    def _load_config(self):
        with Path(self._path).open('rb') as file:
            raw = file.read(65537)
        if len(raw) > 65536:
            raise ValueError('Configuration too large')
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ValueError('Invalid configuration')
        for name in ('host', 'username', 'password'):
            if not isinstance(config.get(name), str) or not config[name]:
                raise ValueError('Missing configuration')
        if type(config.get('port', 1883)) is not int or not 1 <= config.get('port', 1883) <= 65535:
            raise ValueError('Invalid port')
        config['vision_image_topic'] = self._topic(config.get('vision_image_topic', config['username']+'/vision/image'))
        config['vision_result_topic'] = self._topic(config.get('vision_result_topic', config['username']+'/vision/result'))
        return config

    def start(self):
        with self._lifecycle:
            with self._condition:
                if self._active:
                    return
            client = None
            try:
                config = self._load_config()
                if self._factory is None:
                    import paho.mqtt.client as mqtt
                    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                         client_id='xiaozhi-vision-'+uuid.uuid4().hex[:10])
                else:
                    client = self._factory()
                client.username_pw_set(config['username'], config['password'])
                client.max_queued_messages_set(8)
                client.max_inflight_messages_set(1)
                client.reconnect_delay_set(min_delay=1, max_delay=15)
                with self._condition:
                    self._config = config
                    self._configured = True
                    self._generation += 1
                    generation = self._generation
                    self._client = client
                    self._active = True
                    self._connected = False
                    self._error = None
                client.on_connect = lambda c,u,f,r,p: self._connection(generation,c,not r.is_failure)
                client.on_disconnect = lambda c,u,f,r,p: self._connection(generation,c,False)
                client.on_publish = lambda c,u,m,r,p: self._on_publish(generation,c,m,not r.is_failure)
                client.connect_async(config['host'], config.get('port',1883), keepalive=30)
                client.loop_start()
                threading.Thread(target=self._worker, args=(generation,client), daemon=True).start()
            except Exception:
                with self._condition:
                    self._active = False
                    self._connected = False
                    self._configured = False
                    self._error = 'SIoT配置或连接启动不可用'
                if client is not None:
                    threading.Thread(target=self._shutdown,args=(client,),daemon=True).start()

    def _current(self, generation, client):
        return self._active and generation == self._generation and client is self._client

    def _connection(self, generation, client, connected):
        with self._condition:
            if not self._current(generation,client):
                return
            self._connected = connected
            self._connection_generation += 1
            if not connected:
                # QoS1 already accepted by Paho remains pending through reconnect.
                for mid, task in list(self._pending.items()):
                    if task[1] == 'result':
                        self._set_status(task,'pending_reconnect')
                    else:
                        self._set_status(task,'unconfirmed')
                        del self._pending[mid]
                while self._queue:
                    self._set_status(self._queue.popleft(),'offline')
                    self._dropped += 1
            self._condition.notify_all()

    def _expire(self):
        now = time.monotonic()
        for capture_id, value in list(self._images.items()):
            if now-value[1] > self.IMAGE_TTL:
                del self._images[capture_id]
        for capture_id, record in list(self._records.items()):
            if now-record['created'] > self.STATUS_TTL:
                self._records.pop(capture_id,None)
                self._images.pop(capture_id,None)

    def begin_capture(self, jpeg, question):
        capture_id = uuid.uuid4().hex
        try:
            valid_question = isinstance(question,str) and len(question.encode('utf-8')) <= self.MAX_QUESTION
        except UnicodeError:
            valid_question = False
        valid_jpeg = (isinstance(jpeg,bytes) and 4 <= len(jpeg) <= self.MAX_JPEG
                      and jpeg.startswith(b'\xff\xd8') and jpeg.endswith(b'\xff\xd9'))
        with self._condition:
            self._expire()
            record = dict(created=time.monotonic(), timestamp=time.time(), question=question if valid_question else '',
                          status='captured', image_status='unavailable', result_status='not_ready',
                          error=None, revision=0, completed=False)
            self._records[capture_id] = record
            self._last_capture = capture_id
            while len(self._records)>16:
                old,_ = self._records.popitem(last=False)
                self._images.pop(old,None)
            if valid_jpeg and valid_question:
                # Without a running bridge there is no expiry worker; do not
                # retain a photo that cannot be sent or automatically expired.
                if self._active and self._configured:
                    self._images[capture_id] = (jpeg,time.monotonic())
                    while len(self._images)>4:
                        self._images.popitem(last=False)
                self._enqueue([(capture_id,'image',0,None,'captured')])
            else:
                record.update(image_status='error',error='原始JPEG或问题超过限制或格式无效')
            return capture_id

    def complete_capture(self, capture_id, text, error=None):
        try:
            valid_text = isinstance(text,str) and len(text.encode('utf-8')) <= self.MAX_TEXT
        except UnicodeError:
            valid_text = False
        with self._condition:
            self._expire()
            record = self._records.get(capture_id)
            if record is None or record['completed']:
                return
            record['completed'] = True
            record['revision'] = 1
            record['status'] = 'error' if error is not None else 'complete'
            if not valid_text:
                record.update(image_status='error',result_status='error',error='识别文字无效或超过256KiB')
                return
            if error is not None:
                record['error'] = '云端识别失败'
            tasks = []
            if capture_id in self._images:
                tasks.append((capture_id,'image',1,text,record['status']))
            else:
                record['image_status'] = 'expired'
            tasks.append((capture_id,'result',1,text,record['status']))
            self._enqueue(tasks)

    def _enqueue(self, tasks):
        unavailable = 'offline' if self._configured and self._active else 'unavailable'
        if not self._connected or not self._active:
            for task in tasks:
                self._set_status(task,unavailable)
            self._dropped += len(tasks)
            return
        if len(self._queue)+len(tasks)>8:
            for task in tasks:
                self._set_status(task,'queue_full')
            self._dropped += len(tasks)
            return
        self._queue.extend(tasks)
        for task in tasks:
            self._set_status(task,'queued')
        self._condition.notify_all()

    def _set_status(self, task, status, error=None):
        capture_id,kind,revision = task[:3]
        record = self._records.get(capture_id)
        if record is not None and (kind=='result' or record['revision']==revision):
            record[kind+'_status'] = status
            if error is not None:
                record['error'] = error

    def _encode(self, task):
        capture_id,kind,revision,text,status = task
        with self._condition:
            record = self._records.get(capture_id)
            if record is None:
                raise ValueError('Expired capture')
            question,timestamp = record['question'],record['timestamp']
            image_data = self._images.get(capture_id)
        if kind=='image':
            if image_data is None:
                raise ValueError('Expired image')
            jpeg = image_data[0]
            from PIL import Image
            with Image.open(io.BytesIO(jpeg)) as image:
                width,height = image.size
                if (image.format!='JPEG' or not 0<width<=8192 or not 0<height<=8192
                        or width*height>16*1024*1024):
                    raise ValueError('Invalid image dimensions')
                image.load()
            envelope = dict(type='image',mime='image/jpeg',encoding='base64',capture_id=capture_id,
                            question=question,timestamp=timestamp,width=width,height=height,status=status,
                            data=base64.b64encode(jpeg).decode('ascii'))
            if text is not None:
                envelope['recognition'] = text
        else:
            envelope = dict(type='vision_result',capture_id=capture_id,question=question,text=text,status=status)
        payload = json.dumps(envelope,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        if len(payload)>self.MAX_ENVELOPE:
            raise ValueError('Envelope exceeds limit')
        return payload

    def _on_publish(self, generation, client, mid, success):
        with self._condition:
            if not self._current(generation,client):
                return
            task = self._pending.pop(mid,None)
            if task is None:
                self._early_acks[mid] = success
                while len(self._early_acks)>8:
                    self._early_acks.popitem(last=False)
            else:
                self._finish(task,success)
            self._condition.notify_all()

    def _finish(self, task, success):
        status = ('broker_ack' if task[1]=='result' else 'local_sent') if success else 'error'
        self._set_status(task,status,None if success else 'MQTT发送未确认成功')

    def _worker(self, generation, client):
        while True:
            with self._condition:
                self._expire()
                if not self._current(generation,client):
                    return
                if not self._connected or self._pending or not self._queue:
                    self._condition.wait(.1)
                    continue
                task = self._queue.popleft()
                connection_generation = self._connection_generation
            try:
                payload = self._encode(task)
                with self._publish_gate:
                    with self._condition:
                        if not self._current(generation,client):
                            return
                        if not self._connected or connection_generation != self._connection_generation:
                            self._set_status(task,'offline')
                            self._dropped += 1
                            continue
                        topic = self._config['vision_'+task[1]+'_topic']
                    info = client.publish(topic,payload,qos=1 if task[1]=='result' else 0,retain=False)
                    payload = None
                    with self._condition:
                        reconnect_pending = task[1]=='result' and int(info.rc)==4
                        if int(info.rc)!=0 and not reconnect_pending:
                            raise RuntimeError('MQTT rejected send')
                        # Completion can arrive from the network before publish returns.
                        early = self._early_acks.pop(info.mid,None)
                        tracked = task[:3]  # No JPEG or recognition text held for ACK.
                        if early is not None:
                            self._finish(tracked,early)
                        elif task[1]=='image' and (not self._connected or connection_generation != self._connection_generation):
                            # Disconnect may run before publish returns, before
                            # the callback can see this QoS0 mid in _pending.
                            self._set_status(task,'unconfirmed')
                            self._dropped += 1
                        else:
                            status = 'pending_reconnect' if reconnect_pending or not self._connected else 'pending'
                            self._set_status(task,status)
                            self._pending[info.mid] = tracked
            except Exception:
                with self._condition:
                    if self._current(generation,client):
                        self._set_status(task,'error','原图或消息超限、无效，或SIoT发送失败')
                        self._dropped += 1

    def snapshot(self, capture_id=None):
        with self._condition:
            self._expire()
            if capture_id is not None:
                record = self._records.get(capture_id)
                if record is None:
                    return dict(capture_id=capture_id,status='expired',image_status='unavailable',result_status='unavailable')
                return dict(capture_id=capture_id,status=record['status'],image_status=record['image_status'],
                            result_status=record['result_status'],error=record['error'],
                            cached_image=capture_id in self._images)
            return dict(configured=self._configured,running=self._active,connected=self._connected,
                        pending=len(self._queue),awaiting_ack=len(self._pending),dropped=self._dropped,
                        error=self._error,capture_count=len(self._records),cached_images=len(self._images),
                        last_capture_id=self._last_capture)

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
                    client = self._client
                    self._active = False
                    self._connected = False
                    self._generation += 1
                    for task in self._queue:
                        self._set_status(task,'stopped')
                    for task in self._pending.values():
                        self._set_status(task,'unconfirmed')
                    self._queue.clear()
                    self._pending.clear()
                    self._early_acks.clear()
                    self._images.clear()
                    self._client = None
                    self._condition.notify_all()
            if client is not None:
                threading.Thread(target=self._shutdown,args=(client,),daemon=True).start()
