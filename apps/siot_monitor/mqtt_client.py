"""Paho 2 client; callbacks enqueue bounded data and never draw the UI."""
import json
from pathlib import Path
import queue
import threading
import time
import uuid
import urllib.request
import urllib.parse

from model import validate_publish, saved_topic_name


def load_config(path='/etc/siot/monitor.json'):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('监控连接配置格式错误')
    for name in ('host', 'username', 'password'):
        if not isinstance(config.get(name), str) or not config[name]:
            raise ValueError('连接配置缺少 ' + name)
    port = config.get('port', 1883)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('MQTT端口不合法')
    return config


class MonitorClient:
    def __init__(self, config, factory=None, registry=None):
        if factory is None:
            import paho.mqtt.client as mqtt
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                      client_id='maixmon-' + uuid.uuid4().hex[:10])
        else:
            self.client = factory()
        self.config = config
        self.messages = queue.Queue(maxsize=256)
        self.controls = queue.Queue(maxsize=32)
        self._media = queue.Queue(maxsize=1)
        self._media_lock = threading.Lock()
        self._media_topic = None
        self.connected = False
        self.dropped = 0
        self._dropped_lock = threading.Lock()
        self._send_queue = queue.Queue(maxsize=8)
        self._send_stop = threading.Event()
        self._send_thread = threading.Thread(target=self._sender, daemon=True)
        self._send_lock = threading.Lock()
        self._request_id = 0
        self._outstanding = set()
        self._pending_mids = {}
        self._early_acks = {}
        # Separate HTTP client/token from the history worker: no shared login race.
        self.registry = registry if registry is not None else HistoryClient(config)
        self.client.username_pw_set(config['username'], config['password'])
        self.client.reconnect_delay_set(min_delay=1, max_delay=15)
        self.client.max_queued_messages_set(8)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_publish = self._on_publish

    @staticmethod
    def _put(target, item):
        try:
            target.put_nowait(item)
        except queue.Full:
            try:
                target.get_nowait()
            except queue.Empty:
                pass
            try:
                target.put_nowait(item)
            except queue.Full:
                pass

    def _on_connect(self, client, userdata, flags, reason, properties):
        self.connected = not reason.is_failure
        if self.connected:
            client.subscribe('#', qos=0)
            self._put(self.controls, ('connection', True, '已连接'))
        else:
            self._put(self.controls, ('connection', False, '连接被拒绝，检查凭据'))

    def _on_disconnect(self, client, userdata, flags, reason, properties):
        self.connected = False
        self._put(self.controls, ('connection', False, '已断开，自动重连中'))

    def _on_message(self, client, userdata, message):
        received_at = time.time()
        topic = str(message.topic)
        if self.messages.full():
            with self._dropped_lock:
                self.dropped += 1
        self._put(self.messages, ('message', topic, bytes(message.payload[:4096]),
                                 received_at, bool(message.retain), len(message.payload)))
        with self._media_lock:
            if topic == self._media_topic:
                if len(message.payload) > 3 * 1024 * 1024:
                    event = ('media_error', topic, '媒体消息超过3MiB，未加载')
                else:
                    event = ('media', topic, bytes(message.payload), received_at)
                self._put(self._media, event)

    def set_media_topic(self, topic):
        """Keep at most one full payload for the selected topic, never a backlog."""
        if topic is not None:
            validate_publish(topic, 'media-selection')
        with self._media_lock:
            if topic == self._media_topic:
                return
            self._media_topic = topic
            try:
                self._media.get_nowait()
            except queue.Empty:
                pass

    def _on_publish(self, client, userdata, mid, reason, properties):
        success = not reason.is_failure
        with self._send_lock:
            pending = self._pending_mids.pop(mid, None)
            if pending is None:
                # Paho can deliver QoS0 completion before publish() returns.
                if len(self._early_acks) >= 16:
                    self._early_acks.pop(next(iter(self._early_acks)))
                self._early_acks[mid] = success
                return
        self._finish_send(pending[0], success, pending[1])

    def start(self):
        self.client.connect_async(self.config['host'], self.config.get('port', 1883), keepalive=30)
        self.client.loop_start()
        self._send_thread.start()

    def publish(self, topic, payload, save=True):
        validate_publish(topic, payload)
        if save:
            saved_topic_name(topic, self.config['username'])
        if not self.connected:
            raise RuntimeError('尚未连接，未发送')
        with self._send_lock:
            if len(self._outstanding) >= 8:
                raise RuntimeError('等待确认的消息过多，请稍后再试')
            self._request_id += 1
            request_id = self._request_id
            self._outstanding.add(request_id)
        try:
            self._send_queue.put_nowait((request_id, topic, payload, bool(save)))
        except queue.Full:
            with self._send_lock:self._outstanding.discard(request_id)
            raise RuntimeError('发送队列已满')
        return request_id

    def _finish_send(self, request_id, success, save):
        with self._send_lock:self._outstanding.discard(request_id)
        self._put(self.controls, ('published', request_id, success, save))

    def _process_send(self, request):
        request_id, topic, payload, save = request
        try:
            if self._send_stop.is_set() or not self.connected:
                raise RuntimeError('连接已断开，未发送')
            if save:
                self._put(self.controls, ('send_progress', request_id, '检查并登记保存主题…'))
                self.registry.ensure_topic(topic)
            if self._send_stop.is_set() or not self.connected:
                raise RuntimeError('连接已断开，未发送')
            info = self.client.publish(topic, payload.encode('utf-8'), qos=1 if save else 0, retain=False)
            # Paho queues QoS1 before it can report MQTT_ERR_NO_CONN (4).
            # It owns reconnect delivery; an error here would incorrectly let
            # the user resend a message that is still pending inside Paho.
            reconnect_pending = save and int(info.rc) == 4
            if int(info.rc) != 0 and not reconnect_pending:
                raise RuntimeError('客户端未接受发送')
            if reconnect_pending:
                # Enqueue progress before exposing the mid, so even an early
                # acknowledgement leaves completion as the last UI event.
                self._put(self.controls, ('send_progress', request_id,
                                         '等待重连确认，请勿重复发送'))
            with self._send_lock:
                early = self._early_acks.pop(info.mid, None)
                if early is None:self._pending_mids[info.mid] = (request_id, save)
            if early is not None:self._finish_send(request_id, early, save)
        except Exception:
            with self._send_lock:self._outstanding.discard(request_id)
            message = '主题登记或连接失败，未发送保存消息' if save else '连接异常，未发送'
            self._put(self.controls, ('send_error', request_id, message))

    def _sender(self):
        while not self._send_stop.is_set():
            try:request=self._send_queue.get(timeout=.2)
            except queue.Empty:continue
            self._process_send(request)

    def drain(self, limit=64):
        events = []
        for source, count in ((self.controls, 32), (self.messages, limit)):
            for _ in range(count):
                try:
                    events.append(source.get_nowait())
                except queue.Empty:
                    break
        with self._media_lock:
            try:
                events.append(self._media.get_nowait())
            except queue.Empty:
                pass
        return events

    def close(self):
        self.set_media_topic(None)
        self._send_stop.set()
        self.connected = False
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()
            if self._send_thread.is_alive():self._send_thread.join(timeout=.5)


class HistoryClient:
    """Bounded serial HTTP worker; REST failures do not block MQTT or touch."""
    def __init__(self, config):
        self.config = config
        self.requests = queue.Queue(maxsize=4)
        self.read_requests = queue.Queue(maxsize=4)
        self.events = queue.Queue(maxsize=16)
        self.change_events = queue.Queue(maxsize=8)
        self._change_lock = threading.Lock()
        self._read_enqueue_lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._event_sequence = 0
        self._change_id = 0
        self.stop = threading.Event()
        self.token = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        self.refresh_topics()

    def _request(self, path, body=None, authenticated=True, method=None):
        host = self.config.get('http_host', self.config['host'])
        port = int(self.config.get('http_port', 8080))
        headers = {'Content-Type':'application/json'}
        if authenticated:
            if self.token is None:
                result = self._request('/login', {'username':self.config['username'],
                                                  'password':self.config['password']}, False)
                self.token = result['data']['token']
                if not isinstance(self.token,str) or not self.token or '\n' in self.token or '\r' in self.token:
                    raise ValueError('Invalid login response')
            headers['authorization'] = self.token
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request('http://%s:%d/api/v2%s' % (host, port, path),
                                         data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=4) as response:
            raw = response.read(512*1024+1)
        if len(raw)>512*1024:
            raise ValueError('REST response too large')
        result = json.loads(raw)
        if not isinstance(result,dict) or result.get('code') != 0:
            raise ValueError('REST response schema invalid')
        return result

    def _lookup_topic(self, wanted, topic_id=None):
        deadline=time.monotonic()+12
        for offset in range(0,480,30):
            if time.monotonic()>deadline:raise RuntimeError('Topic lookup deadline')
            data=self._request('/topics?'+urllib.parse.urlencode({'length':30,'offset':offset}))['data']
            if not isinstance(data,dict) or not isinstance(data.get('topics'),list):
                raise ValueError('Invalid topics response')
            batch=data['topics']
            matches=[x for x in batch if isinstance(x,dict) and
                     (x.get('name')==wanted or (topic_id is not None and x.get('id')==topic_id))]
            if len(matches)>1:raise RuntimeError('Ambiguous topic identity')
            if matches:return dict(matches[0])
            if len(batch)<30:return None
        raise RuntimeError('Topic lookup exceeds bounded page budget')

    def _find_topic(self, wanted):
        return self._lookup_topic(wanted) is not None

    def ensure_topic(self, topic):
        relative = saved_topic_name(topic, self.config['username'])
        try:
            if self._find_topic(topic):return
            # The server prepends username; sending the full name would double it.
            self._request('/topics', {'name':relative}, method='PUT')
            if not self._find_topic(topic):raise RuntimeError('Created topic not confirmed')
        except Exception:
            self.token=None
            raise

    def create_topic(self, topic):
        validate_publish(topic, 'topic-management')
        saved_topic_name(topic, self.config['username'])
        self.ensure_topic(topic)

    @staticmethod
    def _topic_id(record, expected_name):
        if not isinstance(record,dict) or record.get('name')!=expected_name:
            raise RuntimeError('Topic no longer matches the requested name')
        topic_id=record.get('id')
        if type(topic_id) is not int or not 0<topic_id<2**63:
            raise RuntimeError('Invalid topic id')
        return topic_id

    def delete_topic(self, topic):
        validate_publish(topic, 'topic-management')
        saved_topic_name(topic, self.config['username'])
        try:
            topic_id=self._topic_id(self._lookup_topic(topic),topic)
            # Bind the exact ID/name immediately before DELETE. A renamed or
            # recreated topic is a different target and must not be deleted.
            current=self._lookup_topic(topic,topic_id)
            if self._topic_id(current,topic)!=topic_id:
                raise RuntimeError('Topic identity changed')
            self._request('/topics/'+str(topic_id),method='DELETE')
            if self._lookup_topic(topic,topic_id) is not None:
                raise RuntimeError('Topic deletion not confirmed')
        except Exception:
            self.token=None
            raise

    def _history_id_page(self, topic, offset, end_time):
        query=urllib.parse.urlencode({'length':30,'offset':offset,'startTime':0,'endTime':end_time})
        result=self._request('/messages/getMsgByTopic?'+query,{'topic':topic})['data']
        if not isinstance(result,dict) or not isinstance(result.get('messages'),list):
            raise ValueError('Invalid history verification response')
        messages=result['messages'];total=result.get('total')
        if len(messages)>30 or type(total) is not int or total<0:
            raise ValueError('Invalid history page size')
        ids=set()
        for message in messages:
            mid=message.get('id') if isinstance(message,dict) else None
            if type(mid) is int and mid>0:
                ids.add(str(mid))
            elif type(mid) is str and 0<len(mid)<=128:
                ids.add(mid)
            else:
                raise ValueError('History message lacks a verifiable id')
        return ids,len(messages),total

    def _assert_old_messages_absent(self, topic, old_ids):
        deadline=time.monotonic()+12
        end_time=int(time.time())
        for offset in range(0,480,30):
            if time.monotonic()>deadline:
                raise RuntimeError('History verification deadline exceeded')
            ids,count,total=self._history_id_page(topic,offset,end_time)
            if old_ids.intersection(ids):
                raise RuntimeError('Previous history remains after clear')
            # New messages can arrive while clearing. Their presence is not a
            # failure; only the observed pre-clear IDs must be absent.
            if not old_ids or offset+count>=total:
                return
            if count==0:
                raise RuntimeError('History pagination could not be confirmed')
        raise RuntimeError('History verification exceeds bounded page budget')

    def clear_topic(self, topic):
        validate_publish(topic,'topic-management')
        saved_topic_name(topic,self.config['username'])
        try:
            topic_id=self._topic_id(self._lookup_topic(topic),topic)
            old_ids,_,_=self._history_id_page(topic,0,int(time.time()))
            current=self._lookup_topic(topic,topic_id)
            if self._topic_id(current,topic)!=topic_id:
                raise RuntimeError('Topic identity changed before clear')
            self._request('/messages/delMsgByTopic',{'topic':topic},method='POST')
            self._assert_old_messages_absent(topic,old_ids)
            remaining=self._lookup_topic(topic,topic_id)
            if self._topic_id(remaining,topic)!=topic_id:
                raise RuntimeError('Topic identity changed during clear')
        except Exception:
            self.token=None
            raise

    def enqueue_topic_change(self, action, topic):
        if action not in ('create','delete','clear'):
            raise ValueError('不支持的主题操作')
        validate_publish(topic, 'topic-management')
        saved_topic_name(topic,self.config['username'])
        if self.stop.is_set():
            raise RuntimeError('主题管理已关闭')
        with self._change_lock:
            request_id=self._change_id+1
            try:self.requests.put_nowait((action,request_id,topic))
            except queue.Full:raise RuntimeError('主题操作队列已满，请稍后再试')
            self._change_id=request_id
        return request_id

    def refresh_topics(self):
        with self._read_enqueue_lock:
            MonitorClient._put(self.read_requests, ('topics', None))

    def refresh_history(self, topic):
        with self._read_enqueue_lock:
            MonitorClient._put(self.read_requests, ('history', topic))

    def _discard_history_requests(self, topic):
        # Called by the serial worker after a confirmed delete/clear. Hold the same
        # producer lock so later explicit refreshes are not accidentally lost.
        with self._read_enqueue_lock:
            keep=[]
            while True:
                try:request=self.read_requests.get_nowait()
                except queue.Empty:break
                if request!=('history',topic):keep.append(request)
            for request in keep:self.read_requests.put_nowait(request)

    def _emit_read(self, event):
        with self._event_lock:
            self._event_sequence+=1
            MonitorClient._put(self.events,(self._event_sequence,event))

    def _emit_change(self, event):
        # Never let best-effort read notifications overwrite mutation results.
        while not self.stop.is_set():
            with self._event_lock:
                try:
                    self.change_events.put_nowait((self._event_sequence+1,event))
                    self._event_sequence+=1
                    return
                except queue.Full:pass
            self.stop.wait(.2)

    def _process_change(self, request):
        action, request_id, topic=request
        if self.stop.is_set():return
        try:
            if action=='create':self.create_topic(topic)
            elif action=='delete':
                self.delete_topic(topic)
                self._discard_history_requests(topic)
            elif action=='clear':
                self.clear_topic(topic)
                self._discard_history_requests(topic)
            else:raise ValueError('Invalid topic change')
            event=({'create':'topic_created','delete':'topic_deleted','clear':'topic_cleared'}[action],request_id,topic)
        except Exception:
            self.token=None
            verb={'create':'创建','delete':'删除','clear':'清空数据'}.get(action,'操作')
            event=('topic_error',request_id,verb+'失败或无法确认，请刷新主题列表')
        # Refresh notifications can be dropped, but never overwrite a mutation
        # completion. If the UI is stalled, apply backpressure to this worker.
        self._emit_change(event)

    def _run(self):
        while not self.stop.is_set():
            try:
                request=self.requests.get_nowait()
            except queue.Empty:
                request=None
            if request is not None:
                self._process_change(request)
                continue
            try:
                action, topic = self.read_requests.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                if action=='topics':
                    names=[]
                    for offset in (0,30,60):
                        result=self._request('/topics?'+urllib.parse.urlencode({'length':30,'offset':offset}))['data']
                        batch=result.get('topics',[])
                        names.extend(x['name'] for x in batch if isinstance(x,dict) and isinstance(x.get('name'),str))
                        if len(batch)<30 or len(names)>=64:
                            break
                    self._emit_read(('topics',names[:64]))
                else:
                    query=urllib.parse.urlencode({'length':30,'offset':0,'startTime':0,'endTime':int(time.time())})
                    result=self._request('/messages/getMsgByTopic?'+query, {'topic':topic})['data']
                    messages=result.get('messages',[])
                    if not isinstance(messages,list):raise ValueError('Invalid messages')
                    self._emit_read(('history',topic,[x for x in messages[:30] if isinstance(x,dict)]))
            except Exception:
                self.token=None
                self._emit_read(('history_error','历史读取失败；实时订阅仍可用'))

    def drain(self):
        pending=[]
        with self._event_lock:
            for source,limit in ((self.events,16),(self.change_events,8)):
                for _ in range(limit):
                    try:pending.append(source.get_nowait())
                    except queue.Empty:break
        # A read already queued before deletion must be delivered first; a
        # genuinely newer server read must remain after the deletion event.
        pending.sort(key=lambda item:item[0])
        return [event for _,event in pending]

    def close(self):
        self.stop.set()
