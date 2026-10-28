"""Coordinate one media source and optional camera publishing without UI hardware."""
import time

from model import validate_publish


class MediaSession:
    def __init__(self, viewer, camera, publisher, clock=time.monotonic, lan_server=None):
        self.viewer=viewer
        self.camera=camera
        self.publisher=publisher
        self.lan_server=lan_server
        self.clock=clock
        self.mode=None
        self.topic=None
        self.continuous_topic=None
        self.notice=''
        self.wire_format='json'
        self.target_fps=5
        self._last_submit=0.
        self._last_sequence=None

    def stop(self):
        self.continuous_topic=None
        self.mode=None
        self.topic=None
        self._last_sequence=None
        # Stop outgoing frames before releasing their source.
        errors=[]
        for resource in (self.publisher,self.lan_server,self.viewer,self.camera):
            if resource is None:continue
            try:resource.stop()
            except Exception as exc:errors.append(exc)
        if self.camera.snapshot().get('status')=='error':errors.append(RuntimeError('Camera stop was not confirmed'))
        if errors:raise RuntimeError('媒体资源未能完全释放，请重开页面') from errors[0]

    def start_topic(self, topic, cached=None):
        validate_publish(topic,'media')
        self.stop()
        self.viewer.start_topic(topic)
        self.mode='topic';self.topic=topic;self.notice='等待所选主题的图片或连续帧'
        if cached and cached[0]==topic:
            if len(cached)>2:self.viewer.submit_topic(topic,cached[1],received_at=cached[2])
            else:self.viewer.submit_topic(topic,cached[1])

    def start_url(self, url):
        self.stop()
        self.viewer.start_url(url)
        self.mode='url';self.notice='正在连接局域网媒体源'

    def start_camera(self, wire_format='json', target_fps=5):
        if wire_format not in ('json','base64'):raise ValueError('不支持的发送格式')
        if type(target_fps) is not int or target_fps not in (1,2,5,10):raise ValueError('不支持的发送帧率')
        self.stop()
        try:
            self.publisher.set_format(wire_format)
            self.publisher.set_rate(target_fps)
            self.wire_format=wire_format
            self.target_fps=target_fps
            self.camera.start()
            self.publisher.start()
        except Exception:
            self.stop()
            raise
        self.mode='camera';self.notice='摄像头预热中'

    def start_lan_sender(self):
        if self.lan_server is None:raise RuntimeError('局域网共享不可用')
        self.stop()
        try:
            self.camera.start()
            self.lan_server.start()
            status=self.lan_server.snapshot()
            if not status.get('running'):raise RuntimeError(status.get('error') or '无法启动局域网共享')
        except Exception:
            self.stop()
            raise
        self.mode='lan_sender';self.notice='本机摄像头通过局域网共享'

    def feed_topic(self, topic, payload, received_at=None):
        if self.mode=='topic' and self.topic==topic:self.viewer.submit_topic(topic,payload,received_at=received_at)

    def pause_sending(self):
        self.continuous_topic=None
        self.publisher.stop()

    def set_continuous(self, topic, enabled):
        if not enabled:
            self.pause_sending();self.notice='连续发送已停止';return
        validate_publish(topic,'media')
        if self.mode!='camera':raise RuntimeError('请先打开本机摄像头')
        self.publisher.start()
        self.continuous_topic=topic;self._last_submit=0.;self._last_sequence=None
        self.notice='连续发送已开启，目标%d帧/秒，不保存历史'%self.target_fps

    def send_single(self, topic):
        validate_publish(topic,'media')
        if self.mode!='camera':raise RuntimeError('请先打开本机摄像头')
        state=self.camera.snapshot()
        if not state.get('jpeg'):raise RuntimeError('画面尚未就绪')
        self.publisher.start()
        result=self.publisher.submit(topic,state['jpeg'],state['sequence'],state['received_at'],state['width'],state['height'])
        self.notice='单帧已排队；接收端显示需另行确认'
        return result

    def poll(self):
        if self.mode is None:return {'sequence':0,'frame':None,'status':'stopped','error':None,'fps':0}
        state=(self.camera if self.mode in ('camera','lan_sender') else self.viewer).snapshot()
        if self.mode=='lan_sender':return dict(state,lan=self.lan_server.snapshot())
        if self.mode=='camera':
            if state.get('status')=='live' and self.notice=='摄像头预热中':self.notice='画面已就绪，可发送单张或开启连续发送'
            outbound=self.publisher.snapshot()
            state=dict(state,publishing=outbound)
            if (self.continuous_topic and state.get('jpeg') and outbound.get('connected')
                    and state.get('sequence')!=self._last_sequence):
                # Publisher owns wire pacing. Feed each new snapshot into its
                # latest-only slot so GUI timing cannot quantize 5 fps to 3 fps.
                self._last_submit=self.clock()
                try:
                    self.publisher.submit(self.continuous_topic,state['jpeg'],state['sequence'],
                                          state['received_at'],state['width'],state['height'])
                    self._last_sequence=state['sequence']
                except (ValueError,RuntimeError):
                    self.notice='发送暂不可用，等待连接恢复'
        return state
