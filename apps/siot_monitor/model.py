"""Bounded, hardware-independent state for the SIoT touchscreen client."""
from collections import OrderedDict, deque
import time
import math

TOPIC_BYTES = 128
PAYLOAD_BYTES = 2048


def validate_publish(topic, payload):
    if not isinstance(topic, str) or not topic or any(x in topic for x in ('#', '+')) or any(ord(x)<32 or ord(x)==127 for x in topic):
        raise ValueError('主题不能为空，也不能包含 #、+ 或空字符')
    if len(topic.encode('utf-8')) > TOPIC_BYTES:
        raise ValueError('主题超过128字节')
    if not isinstance(payload, str) or not payload:
        raise ValueError('请输入消息内容')
    if len(payload.encode('utf-8')) > PAYLOAD_BYTES:
        raise ValueError('消息超过2048字节')
    return topic, payload


def saved_topic_name(topic, username):
    """Return the relative REST name for an account-owned storage topic."""
    prefix = username + '/'
    if not topic.startswith(prefix) or not topic[len(prefix):]:
        raise ValueError('保存主题必须以 ' + prefix + ' 开头且包含主题名')
    return topic[len(prefix):]


def page_items(items, page, size):
    pages = max(1, (len(items) + size - 1) // size)
    page = min(max(0, page), pages - 1)
    return items[page * size:(page + 1) * size], page, pages


def button_contains(rect, x, y):
    """Extend only outer-edge targets; keep internal gaps and targets disjoint."""
    bx,by,width,height=rect
    left=0 if bx<=8 else bx
    top=0 if by<=6 else by
    right=640 if bx+width>=624 else bx+width
    bottom=480 if by+height>=474 else by+height
    return left<=x<right and top<=y<bottom


class TextEditor:
    def __init__(self, value='', limit=PAYLOAD_BYTES):
        self.value = value
        self.cursor = len(value)
        self.limit = limit

    def insert(self, text):
        result = self.value[:self.cursor] + text + self.value[self.cursor:]
        if len(result.encode('utf-8')) > self.limit:
            raise ValueError('输入已达到长度上限')
        self.value = result
        self.cursor += len(text)

    def backspace(self):
        if self.cursor:
            self.value = self.value[:self.cursor - 1] + self.value[self.cursor:]
            self.cursor -= 1

    def move(self, offset):
        self.cursor = max(0, min(len(self.value), self.cursor + offset))

    def clear(self):
        self.value = ''
        self.cursor = 0


class PressEdge:
    def __init__(self):
        self.pressed = False

    def update(self, pressed):
        edge = bool(pressed) and not self.pressed
        self.pressed = bool(pressed)
        return edge


class TouchGestures:
    """One release-based tap or a scroll gesture, latched to its start region.

    Release samples can contain reset coordinates on Maix touch drivers, so
    only pressed samples update the position. Call cancel() on page changes.
    """
    TAP_DISTANCE = 12
    SWIPE_DISTANCE = 32

    def __init__(self):
        self._pressed = False
        self._ignored = False
        self._start = self._last = None
        self._mode = False
        self._region = None
        self._travel = 0

    def cancel(self):
        self._ignored = self._pressed
        self._start = self._last = None
        self._region = None
        self._travel = 0

    reset = cancel

    @staticmethod
    def _target(x, y, enabled):
        if enabled:
            if 8 <= x < 200 and 100 <= y <= 366:
                return ('topic_scroll' if x >= 184 else 'topic_swipe', 100, 366)
            if 204 <= x < 640 and 100 <= y <= 382:
                return ('scroll' if x >= 616 else 'swipe', 100, 382)
        return None

    def _scroll_event(self):
        kind, top, bottom = self._region
        return kind, max(0., min(1., (self._last[1]-top)/(bottom-top)))

    def update(self, x, y, pressed, message_scroll=False):
        pressed = bool(pressed)
        enabled = bool(message_scroll)
        if self._pressed and self._start is not None and enabled != self._mode:
            self.cancel()
        if self._ignored:
            self._pressed = pressed
            if not pressed:self._ignored = False
            return None
        if pressed:
            started = not self._pressed
            if started:
                self._start = self._last = (x, y)
                self._travel = 0
                self._mode = enabled
                self._region = self._target(x, y, enabled)
            self._pressed = True
            if not (0 <= x < 640 and 0 <= y < 480):
                self.cancel()
                return None
            previous = self._last
            self._last = (x, y)
            dx, dy = x-self._start[0], y-self._start[1]
            self._travel = max(self._travel, dx*dx+dy*dy)
            if self._region and self._region[0] in ('scroll', 'topic_scroll'):
                if started or previous != self._last:
                    return self._scroll_event()
            return None
        if not self._pressed:
            return None
        self._pressed = False
        if self._start is None:
            return None
        start, last, region, travel = self._start, self._last, self._region, self._travel
        if region and region[0] in ('scroll', 'topic_scroll'):
            event = self._scroll_event()
        else:
            dx, dy = last[0]-start[0], last[1]-start[1]
            if region and abs(dy) >= self.SWIPE_DISTANCE and abs(dy) > abs(dx):
                event = region[0], 1 if dy < 0 else -1
            elif travel <= self.TAP_DISTANCE*self.TAP_DISTANCE:
                event = ('tap', *start)
            else:
                event = None
        self._start = self._last = None
        self._region = None
        return event


class MonitorModel:
    def __init__(self, max_topics=64, max_messages=50):
        self.max_topics = max_topics
        self.max_messages = max_messages
        self.topics = OrderedDict()
        self.selected_topic = None
        self.connected = False
        self.connection_text = '连接中'
        self.received = 0
        self.dropped = 0
        self.notice = ''

    def receive(self, topic, payload, timestamp=None, retained=False, source='实时', message_id=None):
        if not isinstance(topic, str) or not topic:
            return
        raw = payload if isinstance(payload, bytes) else str(payload).encode('utf-8')
        if topic not in self.topics:
            if len(self.topics) >= self.max_topics:
                victim = next((key for key in self.topics if key != self.selected_topic), None)
                if victim is not None:
                    del self.topics[victim]
            self.topics[topic] = deque(maxlen=self.max_messages)
        self.topics[topic].append({'text': raw[:4096].decode('utf-8', 'replace'),
                                  'bytes': len(raw), 'truncated': len(raw) > 4096,
                                  'timestamp': time.time() if timestamp is None else timestamp,
                                  'retained': bool(retained), 'source': source, 'id': message_id,
                                  'sequence':self.received+1})
        if self.selected_topic is None:
            self.selected_topic = topic
        self.received += 1

    def seed_topics(self, topics):
        """Optional REST adapter entry point; does not invent history messages."""
        for topic in topics:
            if isinstance(topic, str) and topic and topic not in self.topics and len(self.topics) < self.max_topics:
                self.topics[topic] = deque(maxlen=self.max_messages)
        if self.selected_topic is None and self.topics:
            self.selected_topic = next(iter(self.topics))

    def history(self):
        return list(self.topics.get(self.selected_topic, ()))

    def clear_messages(self, topic, keep_after_sequence=None):
        """Clear existing cache while retaining messages received after confirmation."""
        self.seed_topics([topic])
        if topic not in self.topics:return
        keep=[] if keep_after_sequence is None else [m for m in self.topics[topic]
            if type(m.get('sequence')) is int and m['sequence']>keep_after_sequence]
        self.topics[topic]=deque(keep,maxlen=self.max_messages)

    def add_history(self, topic, messages):
        self.seed_topics([topic])
        if topic not in self.topics:
            return
        current = list(self.topics[topic])
        ids = {str(m['id']) for m in current if m.get('id') is not None}
        # MQTT has no server message ID. Match complete live records one-for-one
        # within a second, never collapsing repeated equal-value publications.
        live_matches = {}
        for index, item in enumerate(current):
            if item.get('source') != '实时' or item.get('id') is not None or item.get('truncated'):
                continue
            text = item.get('text', '')
            raw = text.encode('utf-8')
            # A replacement character may hide invalid original UTF-8 bytes.
            if '\ufffd' in text or len(raw) != item.get('bytes'):
                continue
            try:
                second = int(item['timestamp'])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            live_matches.setdefault((raw, len(raw), second), deque()).append(index)
        for message in messages:
            mid = message.get('id')
            if mid is not None and str(mid) in ids:
                continue
            raw = str(message.get('content', '')).encode('utf-8')
            try:
                timestamp = float(message.get('timestamp', 0))
            except (TypeError, ValueError):
                timestamp = 0
            if not math.isfinite(timestamp) or not 0 <= timestamp <= time.time()+86400:
                timestamp = 0
            saved = {'text': raw[:4096].decode('utf-8', 'replace'), 'bytes':len(raw),
                     'truncated':len(raw)>4096, 'timestamp':timestamp,
                     'retained':False, 'source':'历史', 'id':mid}
            matches = live_matches.get((raw, len(raw), int(timestamp))) if mid is not None and not saved['truncated'] else None
            if matches:
                index=matches.popleft()
                if 'sequence' in current[index]:saved['sequence']=current[index]['sequence']
                current[index] = saved
            else:
                current.append(saved)
            if mid is not None:
                ids.add(str(mid))
        def chronological(item):
            identifier = item.get('id')
            numeric = None
            if type(identifier) is int:
                numeric = identifier
            elif isinstance(identifier, str) and identifier.isascii() and identifier.isdigit():
                try:numeric = int(identifier)
                except ValueError:pass
            # REST can return a whole second in reverse order. Numeric server
            # IDs restore that order without inventing fractional timestamps.
            return (item['timestamp'], 0, numeric) if numeric is not None else (item['timestamp'], 1, 0)
        current.sort(key=chronological)
        self.topics[topic] = deque(current[-self.max_messages:], maxlen=self.max_messages)
