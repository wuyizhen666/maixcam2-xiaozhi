#!/usr/bin/env python3
"""MaixCAM2 SIoT topic monitor and touch message editor."""
import os
import sys

STRIP_KEYS = ('LD_PRELOAD', 'MAIXCAM2_GF', 'MAIXCAM2_OVL', 'MAIXCAM2_GF_LOG')
if __name__ == '__main__' and any(os.environ.get(k) for k in STRIP_KEYS):
    clean = {k:v for k,v in os.environ.items() if k not in STRIP_KEYS}
    os.execve('/usr/bin/python3', ['/usr/bin/python3', os.path.abspath(__file__)] + sys.argv[1:], clean)

from functools import lru_cache
from pathlib import Path
import json
import queue
import signal
import threading
import time

from PIL import Image, ImageDraw, ImageFont, ImageOps
from model import MonitorModel, TextEditor, TouchGestures, page_items, validate_publish, button_contains
from mqtt_client import MonitorClient, HistoryClient, load_config
from numeric_chart import numeric_series, render_plot

BG = '#101b29'
PANEL = '#1c2b3c'
BLUE = '#206ba4'
INK = '#e8f2fa'
DIM = '#9bb0c4'
GREEN = '#46ddb0'
ORANGE = '#ffbd6b'
FONT_PATHS = [os.environ.get('SIOT_MONITOR_FONT', ''),
              '/maixapp/share/font/SourceHanSansCN-Regular.otf',
              'C:/Windows/Fonts/msyh.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
FONT_PATH = next((p for p in FONT_PATHS if p and Path(p).is_file()), None)


@lru_cache(maxsize=16)
def font(size):
    return ImageFont.truetype(FONT_PATH, size) if FONT_PATH else ImageFont.load_default()


@lru_cache(maxsize=256)
def tile(value, size, color):
    f = font(size)
    box = f.getbbox(value or ' ')
    out = Image.new('RGBA', (max(1,box[2]-box[0]+2), size+10))
    ImageDraw.Draw(out).text((1-box[0],1-box[1]), value, font=f, fill=color)
    return out


def fit(value, width, size=18):
    value = str(value).replace('\n',' ').replace('\r',' ')
    if len(value)>1024:value=value[:1024]+'…'
    return _fit_text(value,width,size)


@lru_cache(maxsize=512)
def _fit_text(value,width,size):
    f = font(size)
    if f.getlength(value) <= width:
        return value
    lo, hi = 0, len(value)
    while lo < hi:
        mid = (lo+hi+1)//2
        if f.getlength(value[:mid]+'…') <= width:lo=mid
        else:hi=mid-1
    return value[:lo]+'…'


@lru_cache(maxsize=64)
def wrapped(value, width, size=18):
    lines=[]
    for paragraph in str(value).split('\n'):
        if not paragraph:
            lines.append('');continue
        while paragraph:
            lo,hi=1,len(paragraph)
            while lo<hi:
                mid=(lo+hi+1)//2
                if font(size).getlength(paragraph[:mid])<=width:lo=mid
                else:hi=mid-1
            lines.append(paragraph[:lo]);paragraph=paragraph[lo:]
    return lines or ['']


CHINESE = ['开','关','启动','停止','是','否','测试','你好','正常','报警',
           '成功','失败','收到','状态','温度','湿度','灯','速度','角度','电机',
           '左','右','前','后']
KEYS = {
    'abc': list('qwertyuiopasdfghjklzxcvbnm'),
    '123': list('1234567890-+./:_%=(),'),
    'sym': list('{}[]":,./_-+*=!?@#$%&()<>\\\'`~;|'),
    'cn': CHINESE,
}


class Screen:
    def __init__(self, model):
        self.model=model
        self.page='monitor'
        self.topic_page=0
        self.message_page=0
        self.detail=None
        self.detail_page=0
        self.focus='payload'
        self.topic=TextEditor('siot/test',128)
        self.payload=TextEditor('',2048)
        self.keyboard='abc'
        self.upper=False
        self.save=True
        self.pending_mid=None
        self.pending_topic_change=None
        self.delete_target=None
        self.clear_sequence=None
        self.clear_media_time=None
        self.media_cleared_before={}
        self.account_prefix=''
        self.search=TextEditor('',128)
        self.search_query=''
        self.view='messages'
        self.media_session=None
        self.media_state={}
        self.last_media_payload=None
        self.media_url=TextEditor('',512)
        self.media_send_topic=TextEditor('',128)
        self.send_fps=5
        self.buttons=[]
        self.notice='实时订阅与本次缓存；选择主题可读最近历史'

    def text(self, img, x, y, value, size=18, color=INK, width=None):
        value=fit(value,width,size) if width else str(value)
        pic=tile(value,size,color)
        img.paste(pic,(int(x),int(y)),pic)

    def button(self,img,rect,label,action,selected=False,size=18,enabled=True):
        x,y,w,h=rect
        ImageDraw.Draw(img).rounded_rectangle((x,y,x+w-1,y+h-1),radius=6,fill=BLUE if selected else PANEL)
        self.text(img,x+9,y+(h-size)//2-2,label,size,color=INK if enabled else '#627387',width=w-16)
        if enabled:self.buttons.append((rect,action))

    def draw(self):
        img=Image.new('RGB',(640,480),BG)
        self.buttons=[]
        back='media_menu' if self.page in ('media_view','media_url','media_send_topic') else ('exit' if self.page=='monitor' else 'monitor')
        if self.page in ('media_lan_menu','media_format'):back='media_menu'
        if self.page=='media_url' or (self.page=='media_view' and self.media_session and self.media_session.mode in ('url','lan_sender')):back='media_lan_menu'
        self.button(img,(8,6,80,42),'返回',back)
        titles={'monitor':'SIoT 监控','detail':'消息详情','edit':'编辑消息','create':'新建主题','delete':'删除主题','clear_data':'清空数据','search':'搜索主题'}
        titles.update({'media_menu':'图像 / 视频','media_lan_menu':'局域网收发','media_format':'选择发送格式','media_view':'图像 / 视频','media_url':'局域网接收地址','media_send_topic':'图片发送主题'})
        self.text(img,102,12,titles[self.page],22)
        if self.page in ('edit','create','search','media_url','media_send_topic'):
            if self.page=='edit':self.button(img,(362,6,150,42),'保存 开' if self.save else '保存 关','save',self.save)
            label,action={'edit':('发送','send'),'create':('创建','create_confirm'),'search':('筛选','search_apply'),
                          'media_url':('连接','media_url_start'),'media_send_topic':('确定','media_topic_apply')}[self.page]
            self.button(img,(520,6,112,42),label,action,True)
        else:
            connection=('MQTT '+self.model.connection_text) if self.page.startswith('media_') else self.model.connection_text
            self.text(img,414,17,connection,16,GREEN if self.model.connected else ORANGE,width=216)
        if self.page=='monitor':self.draw_monitor(img)
        elif self.page=='detail':self.draw_detail(img)
        elif self.page in ('delete','clear_data'):self.draw_delete(img)
        elif self.page=='media_menu':self.draw_media_menu(img)
        elif self.page=='media_lan_menu':self.draw_lan_menu(img)
        elif self.page=='media_format':self.draw_media_format(img)
        elif self.page=='media_view':self.draw_media(img)
        else:self.draw_editor(img)
        return img

    def draw_monitor(self,img):
        self.button(img,(8,54,88,38),'清筛选' if self.search_query else '搜索','clear_search' if self.search_query else 'search_topic',size=16)
        self.button(img,(106,54,88,38),'刷新','refresh',size=16)
        topics,self.topic_page,pages=page_items(self.topic_names(),self.topic_page,6)
        for i,name in enumerate(topics):
            self.button(img,(8,100+i*45,172,41),name,('topic',name),name==self.model.selected_topic,size=16)
        if not topics:self.text(img,14,120,'无匹配主题' if self.search_query else '暂无主题',18,DIM)
        self.scrollbar(img,188,100,266,self.topic_page,pages)
        self.button(img,(8,392,88,44),'新建主题','new_topic',True,size=16)
        self.button(img,(106,392,88,44),'删除主题','delete_topic',size=16,enabled=self.model.selected_topic is not None)
        self.text(img,12,440,'主题 %d/%d 页 · 上下滑动'%(self.topic_page+1,pages),12,DIM,width=186)
        self.text(img,210,61,self.model.selected_topic or '选择主题查看消息',18,GREEN,width=292)
        self.button(img,(514,54,118,38),'清空数据','clear_topic',size=16,enabled=self.model.selected_topic is not None)
        history=list(reversed(self.model.history()))
        messages,self.message_page,mpages=page_items(history,self.message_page,4)
        if self.view=='chart':self.draw_chart(img)
        else:
            for i,message in enumerate(messages):
                y=100+i*71
                self.button(img,(204,y,408,66),'',('detail',message))
                stamp=time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(message['timestamp']))
                self.text(img,214,y+5,'%s · %s · %dB'%(stamp,message.get('source','实时'),message['bytes']),14,DIM)
                self.text(img,214,y+29,message['text'],18,width=388)
            if not messages:self.text(img,218,132,'尚未收到消息',18,DIM)
            self.scrollbar(img,624,100,282,self.message_page,mpages)
        self.button(img,(204,392,80,44),'消息','view_messages',self.view=='messages',size=16)
        self.button(img,(292,392,80,44),'图表','view_chart',self.view=='chart',size=16)
        self.button(img,(380,392,120,44),'图像/视频','view_media',size=16)
        self.button(img,(508,392,124,44),'编辑/发送','edit',True,size=16)
        footer='图表按时间顺序 · 仅使用真实数值' if self.view=='chart' else '消息 %d/%d 页 · 最新在前 · 上下滑动'%(self.message_page+1,mpages)
        self.text(img,210,440,footer,13,DIM,width=422)
        self.text(img,8,461,self.notice,13,ORANGE,width=624)

    def topic_names(self):
        query=self.search_query.casefold()
        return [name for name in self.model.topics if query in name.casefold()]

    def scrollbar(self,img,x,y,height,page,pages):
        if pages<=1:return
        draw=ImageDraw.Draw(img);draw.rounded_rectangle((x,y,x+6,y+height-1),radius=3,fill=PANEL)
        size=max(24,height//pages);top=y+round((height-size)*page/(pages-1))
        draw.rounded_rectangle((x,top,x+6,top+size-1),radius=3,fill=DIM)

    def draw_chart(self,img):
        series=numeric_series(self.model.history())
        if series['status']!='numeric':
            message='尚未收到数值' if series['status']=='empty' else '当前消息含非数字内容'
            self.text(img,218,140,message,22,DIM,width=398)
            self.text(img,218,182,'切换“消息”查看原文',18,DIM,width=398)
            return
        self.text(img,214,101,'最新 '+format(series['latest'],'.6g'),24,GREEN,width=400)
        self.text(img,214,137,'最小 '+format(series['min'],'.6g')+'   最大 '+format(series['max'],'.6g'),15,DIM,width=400)
        img.paste(render_plot(series,408,170),(204,163))
        for y,label,point in [(338,'起',series['points'][0]),(358,'止',series['points'][-1])]:
            self.text(img,214,y,label+' '+time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(point[0])),14,DIM,width=400)

    def editor(self):
        if self.focus=='media_url':return self.media_url
        if self.focus=='media_send_topic':return self.media_send_topic
        return self.search if self.focus=='search' else (self.topic if self.focus=='topic' else self.payload)

    def draw_media_menu(self,img):
        self.text(img,16,66,'选择画面来源',22,GREEN)
        self.button(img,(16,110,608,66),'1. 主题图片 / 连续画面','media_topic_start',size=22)
        self.button(img,(16,192,608,66),'2. 局域网收发 / 图片视频地址','media_lan_menu',size=22)
        self.button(img,(16,274,608,66),'3. 本机摄像头 / 向主题发送','media_camera_start',size=22)
        self.text(img,18,365,'主题: '+(self.model.selected_topic or '请先在列表选择'),17,DIM,width=600)
        self.text(img,18,400,'切换来源或退出时停止画面和连续发送。',17,DIM,width=600)
        self.text(img,18,440,self.notice,15,ORANGE,width=600)

    def draw_lan_menu(self,img):
        self.text(img,18,76,'选择当前设备的角色',22,GREEN)
        self.button(img,(16,132,608,82),'接收端：填写图片 / 视频地址','media_url_edit',size=22)
        self.text(img,24,226,'接收 HTTP 图片、MJPEG 或 RTSP 视频。',17,DIM,width=592)
        self.button(img,(16,280,608,82),'发送端：共享本机摄像头','media_lan_start',size=22)
        self.text(img,24,374,'提供网页预览、单张 JPG 和连续 MJPEG 地址。',17,DIM,width=592)
        self.text(img,18,440,self.notice,15,ORANGE,width=600)

    def draw_media_format(self,img):
        self.text(img,18,76,'先选帧率，再点发送格式开始',22,GREEN)
        self.button(img,(16,118,608,66),'JSON：MaixCAM2 / 自定义接收端','media_format_json',size=22)
        self.text(img,24,194,'图片附带帧序号、时间、尺寸等信息。',17,DIM,width=592)
        self.button(img,(16,230,608,66),'Base64：Mind+ 网络图片','media_format_base64',size=22)
        self.text(img,24,306,'包含图片类型前缀，适配Mind+组件。',17,DIM,width=592)
        self.text(img,18,352,'帧率',18,DIM)
        for index,rate in enumerate((2,5,10)):
            self.button(img,(100+index*164,338,148,48),str(rate)+' 帧/秒',('send_fps',rate),self.send_fps==rate,size=18)
        self.text(img,18,420,'发送主题: '+self.media_send_topic.value,16,DIM,width=600)
        self.text(img,18,450,self.notice,14,ORANGE,width=600)

    def draw_lan_sender(self,img):
        state=self.media_state;lan=state.get('lan',{})
        self.text(img,14,58,'局域网共享中' if lan.get('running') else '局域网共享已停止',20,GREEN)
        frame=state.get('frame')
        if frame is not None:
            fitted=ImageOps.contain(frame,(300,225),method=Image.Resampling.NEAREST);img.paste(fitted,(8+(300-fitted.width)//2,94+(225-fitted.height)//2))
        else:self.text(img,18,176,state.get('error') or '摄像头预热中',17,DIM,width=284)
        urls=lan.get('urls',{})
        for y,key,label in [(94,'page','网页预览'),(172,'snapshot','单张图片 JPG'),(250,'mjpeg','连续视频 MJPEG')]:
            self.text(img,324,y,label,17,GREEN,width=302)
            for index,line in enumerate(wrapped(urls.get(key,'等待连接地址'),298,14)[:2]):self.text(img,324,y+24+index*18,line,14)
        self.text(img,14,342,'同一局域网：接收端填写右侧地址，浏览器可打开网页。',16,DIM,width=612)
        self.text(img,14,370,'客户端 %d · %.1f fps · %s'%(lan.get('clients',0),state.get('fps',0),state.get('error') or lan.get('error') or self.notice),14,ORANGE,width=612)
        self.button(img,(8,404,300,46),'停止共享 / 返回','media_lan_menu',size=18)
        self.button(img,(324,404,308,46),'返回主题列表','monitor',size=18)

    def draw_media(self,img):
        session=self.media_session
        mode=session.mode if session else None
        state=self.media_state
        if mode=='lan_sender':self.draw_lan_sender(img);return
        labels={'topic':'主题接收: '+(session.topic or '') if session else '', 'url':'局域网媒体源', 'camera':'本机摄像头 · '+('Base64' if session and session.wire_format=='base64' else 'JSON')}
        recognition=state.get('recognition') or ''
        self.text(img,14,58,labels.get(mode,'已停止'),17,GREEN,width=492 if recognition or mode=='camera' else 612)
        if recognition:self.button(img,(514,52,118,30),'识别详情','media_result_detail',size=15)
        elif mode=='camera':self.button(img,(514,52,118,30),'格式/帧率','media_camera_start',size=15)
        frame=state.get('frame')
        frame_height=210 if recognition else 280
        if frame is not None:
            fitted=ImageOps.contain(frame,(624,frame_height),method=Image.Resampling.NEAREST)
            img.paste(fitted,((640-fitted.width)//2,84+(frame_height-fitted.height)//2))
        else:self.text(img,30,180,state.get('error') or (session.notice if session else '媒体未就绪'),19,DIM,width=580)
        if recognition:
            for index,line in enumerate(wrapped(recognition[:512],608,16)[:3]):self.text(img,16,300+index*20,line,16,INK)
        age=time.time()-state['received_at'] if state.get('received_at') else None
        status=state.get('status','waiting')
        labels={'starting':'启动中','warming':'预热中','live':'实时预览','loading':'连接中','waiting':'等待画面',
                'image':'图片已加载','streaming':'已收到画面','stale':'旧画面，暂未更新','ended':'视频已结束',
                'error':'错误','timeout':'连接超时','stopped':'已停止'}
        line='状态: '+labels.get(status,str(status))
        if age is not None:line+=(' · 读取后 ' if mode=='camera' else ' · 接收后 ')+('%.1f 秒 · %.1f fps'%(max(0,age),state.get('fps',0)))
        self.text(img,14,368,line,14,DIM,width=612)
        if mode=='camera':
            self.button(img,(8,398,170,40),'停止预览','media_menu',size=17)
            self.button(img,(186,398,190,40),'发送单张','media_send_single',size=17)
            sending=bool(session.continuous_topic)
            self.button(img,(384,398,248,40),'连续发送: 开' if sending else '连续发送: 关','media_send_toggle',sending,size=17)
            self.button(img,(8,444,624,32),'发送主题: '+self.media_send_topic.value,'media_topic_edit',size=14)
            publishing=state.get('publishing',{})
            # Keep static CJK glyphs cached instead of shaping a whole new
            # sentence for every increment of the frame counters.
            self.text(img,14,382,'已发',12,ORANGE)
            self.text(img,44,382,str(publishing.get('sent',0)),12,ORANGE,width=62)
            self.text(img,112,382,'跳过旧帧',12,ORANGE)
            self.text(img,166,382,str(publishing.get('dropped',0)),12,ORANGE,width=56)
            self.text(img,230,382,publishing.get('error') or self.notice or session.notice,12,ORANGE,width=398)
        else:
            self.button(img,(8,404,300,46),'停止 / 选择来源','media_menu',size=18)
            self.button(img,(324,404,308,46),'返回主题列表','monitor',size=18)
            self.text(img,14,456,state.get('error') or self.notice or (session.notice if session else ''),13,ORANGE,width=612)

    def poll_media(self):
        if not self.media_session or not self.media_session.mode:return False
        current=self.media_session.poll()
        old=self.media_state
        changed=(current.get('sequence'),current.get('status'),current.get('error'),str(current.get('publishing')),str(current.get('lan')))!=(old.get('sequence'),old.get('status'),old.get('error'),str(old.get('publishing')),str(old.get('lan')))
        self.media_state=current
        return changed

    def stop_media(self):
        if self.media_session:self.media_session.stop()
        self.media_state={}

    def gesture(self,event,network,history):
        if event[0]=='tap':return self.handle(event[1],event[2],network,history)
        if self.page!='monitor':return False
        if event[0].startswith('topic_'):
            pages=max(1,(len(self.topic_names())+5)//6)
            value=self.topic_page+event[1] if event[0]=='topic_swipe' else round(event[1]*(pages-1))
            self.topic_page=min(max(0,value),pages-1)
        elif self.view=='messages':
            pages=max(1,(len(self.model.history())+3)//4)
            value=self.message_page+event[1] if event[0]=='swipe' else round(event[1]*(pages-1))
            self.message_page=min(max(0,value),pages-1)
        return False

    def draw_detail(self,img):
        self.text(img,10,61,self.model.selected_topic or '',18,GREEN,width=620)
        message=self.detail or {'text':''}
        if 'timestamp' in message:
            stamp=time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(message['timestamp']))
            self.text(img,14,83,stamp+' · '+message.get('source','实时')+' · 设备本地时间',14,DIM,width=610)
        lines=wrapped(message['text'],610,18)
        visible,self.detail_page,pages=page_items(lines,self.detail_page,12)
        for i,line in enumerate(visible):self.text(img,14,104+i*24,line,18)
        self.button(img,(8,422,120,46),'上一页','detail_prev')
        self.button(img,(138,422,120,46),'下一页','detail_next')
        self.text(img,274,436,'%d/%d 页'%(self.detail_page+1,pages),16,DIM)
        self.button(img,(418,422,214,46),'复制到编辑','copy',True)
        if message.get('truncated'):self.text(img,10,397,'内容超过4096字节，已截断显示',14,ORANGE)

    def draw_editor(self,img):
        single={'search':self.search,'media_url':self.media_url,'media_send_topic':self.media_send_topic}
        fields=[(self.page,(8,56,624,42),single[self.page])] if self.page in single else [('topic',(8,56,624,42),self.topic),('payload',(8,104,624,76),self.payload)]
        if self.page=='search':
            self.text(img,18,116,'输入主题名称片段，忽略英文大小写。',18,DIM,width=600)
            self.text(img,18,145,'仅筛选列表，不修改服务器主题。',18,DIM,width=600)
        elif self.page=='media_url':
            self.text(img,18,116,'HTTP(S) 图片 / MJPEG，或 RTSP 视频地址。',17,DIM,width=600)
            self.text(img,18,145,'仅连接你指定的来源；输入后点右上角“连接”。',17,DIM,width=600)
        elif self.page=='media_send_topic':
            self.text(img,18,116,'接收设备订阅相同主题，即可接收图片或连续帧。',17,DIM,width=600)
            self.text(img,18,145,'连续帧不保存历史；修改主题会先停止发送。',17,DIM,width=600)
        for key,rect,editor in fields:
            if self.page=='create' and key=='payload':
                self.text(img,18,116,'填写完整主题名称，保留账号前缀。',18,DIM,width=600)
                self.text(img,18,145,'仅创建主题，不会发送消息。',18,DIM,width=600)
                continue
            self.button(img,rect,'',('focus',key),self.focus==key)
            label={'topic':'主题: ','payload':'消息: ','search':'搜索: ','media_url':'地址: ','media_send_topic':'主题: '}[key]
            value=editor.value[:editor.cursor]+'▏'+editor.value[editor.cursor:] if self.focus==key else editor.value
            lines=wrapped(label+value,600,18)
            count=1 if key=='topic' else 2
            cursor_line=next((i for i,line in enumerate(lines) if '▏' in line),len(lines)-1)
            start=max(0,cursor_line-count+1)
            for index,line in enumerate(lines[start:start+count]):self.text(img,18,rect[1]+8+index*25,line,18)
        self.text(img,10,184,self.notice,14,ORANGE,width=620)
        for i,(key,label) in enumerate([('abc','英文'),('123','数字'),('sym','符号'),('cn','中文')]):
            self.button(img,(8+i*80,210,74,36),label,('keyboard',key),self.keyboard==key,size=16)
        self.button(img,(332,210,74,36),'大小写','shift',self.upper,size=15)
        self.button(img,(412,210,68,36),'←','cursor_left')
        self.button(img,(486,210,68,36),'→','cursor_right')
        self.button(img,(560,210,72,36),'清空','clear',size=16)
        keys=KEYS[self.keyboard]
        columns=6 if self.keyboard=='cn' else 10
        width=624//columns
        for i,char in enumerate(keys):
            shown=char.upper() if self.keyboard=='abc' and self.upper else char
            self.button(img,(8+(i%columns)*width,254+(i//columns)*44,width-4,40),shown,('insert',shown),size=16)
        self.button(img,(8,432,116,44),'退格删字','backspace',True,size=17)
        self.button(img,(132,432,280,44),'空格',('insert',' '),size=17)
        self.button(img,(420,432,100,44),'换行',('insert','\n'),size=17)
        done={'media_url':'media_url_start','media_send_topic':'media_topic_apply'}.get(self.page,'monitor')
        self.button(img,(528,432,104,44),'完成',done,size=17)

    def draw_delete(self,img):
        clearing=self.page=='clear_data'
        self.text(img,16,70,'即将清空以下主题的数据：' if clearing else '即将删除以下主题：',20,ORANGE)
        for index,line in enumerate(wrapped(self.delete_target or '',600,20)[:5]):
            self.text(img,16,110+index*26,line,20)
        self.text(img,16,266,'清空历史和已有缓存，保留主题本身。' if clearing else '此服务器会同时删除该主题的历史消息。',18,ORANGE,width=608)
        self.text(img,16,290,'此操作不可撤销；确认后新收到的消息继续显示。' if clearing else '此操作不可撤销；持续发布可能使主题再次出现。',16,ORANGE,width=608)
        self.text(img,16,330,self.notice,16,DIM,width=608)
        self.button(img,(16,406,280,56),'取消','monitor')
        self.button(img,(320,406,304,56),'确认清空' if clearing else '确认删除','clear_confirm' if clearing else 'delete_confirm',True)

    def handle(self,x,y,network,history):
        action=next((a for rect,a in reversed(self.buttons) if button_contains(rect,x,y)),None)
        if action is None:return False
        if self.pending_topic_change is not None:
            self.notice='主题操作正在确认，请稍候'
            return False
        if action=='exit':self.stop_media();return True
        if isinstance(action,str) and (action.startswith('media_') or action=='view_media'):
            self.notice=''
            try:
                if self.media_session is None:raise RuntimeError('媒体连接尚未准备好')
                if action in ('view_media','media_menu'):
                    self.stop_media();self.page='media_menu';self.notice=''
                elif action=='media_lan_menu':
                    self.stop_media();self.page='media_lan_menu';self.notice=''
                elif action=='media_lan_start':
                    self.media_session.start_lan_sender();self.page='media_view'
                elif action=='media_topic_start':
                    if not self.model.selected_topic:raise ValueError('请先选择一个主题')
                    self.media_session.start_topic(self.model.selected_topic,self.last_media_payload);self.page='media_view'
                elif action=='media_url_edit':
                    self.stop_media();self.page='media_url';self.focus='media_url';self.notice=''
                elif action=='media_url_start':
                    self.media_session.start_url(self.media_url.value.strip());self.page='media_view'
                elif action=='media_camera_start':
                    self.stop_media();self.page='media_format';self.notice=''
                elif action in ('media_format_json','media_format_base64'):
                    self.media_session.start_camera('json' if action=='media_format_json' else 'base64',target_fps=self.send_fps);self.page='media_view'
                elif action=='media_topic_edit':
                    self.media_session.pause_sending();self.page='media_send_topic';self.focus='media_send_topic';self.notice=''
                elif action=='media_topic_apply':
                    validate_publish(self.media_send_topic.value,'media');self.page='media_view'
                elif action=='media_send_single':self.media_session.send_single(self.media_send_topic.value)
                elif action=='media_send_toggle':self.media_session.set_continuous(self.media_send_topic.value,not self.media_session.continuous_topic)
                elif action=='media_result_detail':
                    text=self.media_state.get('recognition') or ''
                    raw=text.encode('utf-8')
                    self.detail={'text':raw[:4096].decode('utf-8','ignore'),'bytes':len(raw),'truncated':len(raw)>4096,'timestamp':self.media_state.get('received_at') or time.time(),'source':'云端识别'}
                    self.stop_media();self.detail_page=0;self.page='detail'
                self.media_state=self.media_session.poll()
            except (ValueError,RuntimeError,OSError) as exc:self.notice=str(exc)
            return False
        if isinstance(action,tuple):
            kind,value=action
            if kind=='topic':
                self.model.selected_topic=value;self.message_page=0
                if history:history.refresh_history(value)
            elif kind=='detail':self.detail=value;self.detail_page=0;self.page='detail'
            elif kind=='focus':self.focus=value
            elif kind=='keyboard':self.keyboard=value
            elif kind=='send_fps':self.send_fps=value
            elif kind=='insert':
                try:self.editor().insert(value);self.notice=''
                except ValueError as exc:self.notice=str(exc)
            return False
        editor=self.editor()
        if action=='monitor':self.stop_media();self.page='monitor';self.delete_target=None
        elif action in ('new_topic','delete_topic','clear_topic'):
            if self.pending_mid is not None:self.notice='消息尚未确认，请先等待发送完成';return False
            if history is None or not self.account_prefix:self.notice='主题管理连接不可用';return False
            if action=='new_topic':
                self.topic=TextEditor(self.account_prefix,128);self.focus='topic';self.page='create';self.notice='输入名称后点击右上角“创建”'
            elif self.model.selected_topic:
                self.delete_target=self.model.selected_topic;self.page='clear_data' if action=='clear_topic' else 'delete';self.notice=''
            else:self.notice='请先选择要删除的主题'
        elif action in ('create_confirm','delete_confirm','clear_confirm'):
            try:
                if history is None:raise RuntimeError('主题管理连接不可用')
                topic=self.topic.value if action=='create_confirm' else self.delete_target
                validate_publish(topic,'validation')
                operation={'create_confirm':'create','delete_confirm':'delete','clear_confirm':'clear'}[action]
                if operation=='clear':self.clear_sequence=self.model.received;self.clear_media_time=time.time()
                self.pending_topic_change=history.enqueue_topic_change(operation,topic)
                self.notice={'create':'正在创建主题…','delete':'正在删除并核对结果…','clear':'正在清空并核对结果…'}[operation]
            except (ValueError,RuntimeError) as exc:self.notice=str(exc)
        elif action=='edit':
            if self.model.selected_topic:self.topic=TextEditor(self.model.selected_topic,128)
            self.page='edit';self.focus='payload';self.notice='保存需使用账号前缀主题；不会代替其他设备确认'
        elif action=='search_topic':
            self.search=TextEditor(self.search_query,128);self.page='search';self.focus='search';self.notice='输入名称后点右上角“筛选”'
        elif action in ('search_apply','clear_search'):
            self.search_query=self.search.value.strip() if action=='search_apply' else ''
            self.topic_page=0;self.page='monitor'
            names=self.topic_names()
            if self.model.selected_topic not in names:
                self.model.selected_topic=names[0] if names else None;self.message_page=0
                if history and names:history.refresh_history(names[0])
            self.notice='筛选: '+self.search_query if self.search_query else '已显示全部主题'
        elif action in ('view_messages','view_chart'):self.view='messages' if action=='view_messages' else 'chart'
        elif action=='copy':
            self.topic=TextEditor(self.model.selected_topic or 'siot/test',128)
            value=(self.detail or {}).get('text','')
            self.payload=TextEditor('',2048)
            try:self.payload.insert(value)
            except ValueError:self.notice='消息太长，无法完整复制'
            self.page='edit';self.focus='payload'
        elif action=='refresh':
            if history:
                history.refresh_topics()
                if self.model.selected_topic:history.refresh_history(self.model.selected_topic)
            self.notice='正在刷新主题和历史'
        elif action=='detail_prev':self.detail_page-=1
        elif action=='detail_next':self.detail_page+=1
        elif action=='shift':self.upper=not self.upper
        elif action=='save':self.save=not self.save
        elif action=='backspace':editor.backspace()
        elif action=='clear':editor.clear()
        elif action=='cursor_left':editor.move(-1)
        elif action=='cursor_right':editor.move(1)
        elif action=='send':
            try:
                if self.pending_mid is not None:
                    raise RuntimeError('上一条消息等待确认，请勿重复发送')
                validate_publish(self.topic.value,self.payload.value)
                if network is None:raise RuntimeError('连接不可用')
                self.pending_mid=network.publish(self.topic.value,self.payload.value,self.save)
                self.notice='已排队，正在检查主题并发送' if self.save else '已排队，等待发送'
            except (ValueError,RuntimeError) as exc:self.notice=str(exc)
        return False

    def topic_result(self,event,history):
        if event[1]!=self.pending_topic_change:return
        self.pending_topic_change=None
        if event[0]=='topic_error':self.notice=event[2];return
        topic=event[2]
        if event[0]=='topic_created':
            if topic not in self.model.topics and len(self.model.topics)>=self.model.max_topics:
                self.model.topics.pop(next(iter(self.model.topics)))
            self.model.seed_topics([topic]);self.model.selected_topic=topic
            self.search_query=''
            self.notice='主题已创建，尚未发送消息'
        elif event[0]=='topic_cleared':
            self.model.clear_messages(topic,self.clear_sequence)
            self.media_cleared_before[topic]=self.clear_media_time or time.time()
            if len(self.media_cleared_before)>64:self.media_cleared_before.pop(next(iter(self.media_cleared_before)))
            if self.last_media_payload and self.last_media_payload[0]==topic and (len(self.last_media_payload)<3 or self.last_media_payload[2]<=self.media_cleared_before[topic]):self.last_media_payload=None
            self.clear_sequence=None
            self.notice='历史与已有缓存已清空，主题保留'
        else:
            self.model.topics.pop(topic,None)
            if self.last_media_payload and self.last_media_payload[0]==topic:self.last_media_payload=None
            if self.model.selected_topic==topic:self.model.selected_topic=next(iter(self.topic_names()),None)
            self.notice='主题已删除并核对'
        self.page='monitor';self.delete_target=None;self.message_page=0
        names=self.topic_names()
        if self.model.selected_topic in names:self.topic_page=names.index(self.model.selected_topic)//6
        if history:history.refresh_topics()


def main():
    from maix import app, display, image, touchscreen
    state=MonitorModel();screen=Screen(state);network=history=None
    stop=threading.Event();clicks=queue.Queue(maxsize=16)
    def stop_signal(signum,frame):
        stop.set()
        app.set_exit_flag(True)
    signal.signal(signal.SIGTERM,stop_signal);signal.signal(signal.SIGINT,stop_signal)
    disp=display.Display();touch=touchscreen.TouchScreen()
    def touch_reader():
        gesture=TouchGestures();last_page=screen.page
        while not stop.is_set():
            try:
                page=screen.page
                if page!=last_page:gesture.reset();last_page=page
                x,y,pressed=touch.read()
                event=gesture.update(x,y,pressed,message_scroll=page=='monitor')
                if event:MonitorClient._put(clicks,(page,event))
            except Exception:
                gesture.reset()
            stop.wait(.02)
    reader=threading.Thread(target=touch_reader,daemon=True);reader.start()
    try:
        try:
            config=load_config()
            screen.account_prefix=config['username']+'/'
            screen.media_send_topic=TextEditor(config['username']+'/camera',128)
            default_topic=config.get('default_topic',config['username']+'/monitor')
            if isinstance(default_topic,str) and len(default_topic.encode('utf-8'))<=128:
                screen.topic=TextEditor(default_topic,128)
            network=MonitorClient(config);network.start()
            history=HistoryClient(config);history.start()
            from media_viewer import MediaViewer
            from camera_preview import CameraPreview
            from media_publisher import MediaPublisher
            from media_session import MediaSession
            from lan_camera_server import LanCameraServer
            camera_source=CameraPreview()
            screen.media_session=MediaSession(MediaViewer(),camera_source,MediaPublisher(config),
                lan_server=LanCameraServer(camera_source.snapshot,port=config.get('media_http_port',8090)))
        except Exception:
            state.connection_text='连接配置不可用'
            screen.notice='请检查 /etc/siot/monitor.json 和服务状态'
        dirty=True;last_draw=0.;preview_done=False;media_topic=None;last_media_status=0.;profile=[]
        while not stop.is_set() and not app.need_exit():
            if network and state.selected_topic!=media_topic:
                media_topic=state.selected_topic;network.set_media_topic(media_topic);screen.last_media_payload=None
            for _ in range(16):
                try:page,event=clicks.get_nowait()
                except queue.Empty:break
                if page!=screen.page:continue
                dirty=True
                if screen.gesture(event,network,history):stop.set();break
            if network:
                for event in network.drain():
                    dirty=True
                    if event[0]=='connection':state.connected,state.connection_text=event[1:]
                    elif event[0]=='message':
                        state.receive(event[1],event[2],event[3],event[4])
                        state.topics[event[1]][-1]['bytes']=event[5]
                        state.topics[event[1]][-1]['truncated']=event[5]>4096
                    elif event[0]=='published' and event[1]==screen.pending_mid:
                        screen.notice=('服务器已确认，已请求保存' if event[3] else '已发送（未请求保存）') if event[2] else '服务器拒绝发送'
                        screen.pending_mid=None
                    elif event[0] in ('send_error','send_progress') and event[1]==screen.pending_mid:
                        screen.notice=event[2]
                        if event[0]=='send_error':screen.pending_mid=None
                    elif event[0]=='media' and event[1]==state.selected_topic:
                        if event[3]>screen.media_cleared_before.get(event[1],float('-inf')):
                            screen.last_media_payload=(event[1],event[2],event[3])
                            if screen.media_session:screen.media_session.feed_topic(event[1],event[2],event[3])
                    elif event[0]=='media_error' and event[1]==state.selected_topic:screen.notice=event[2]
                state.dropped=network.dropped
            if history:
                for event in history.drain():
                    dirty=True
                    if event[0] in ('topic_created','topic_deleted','topic_cleared','topic_error'):
                        screen.topic_result(event,history)
                    elif event[0]=='topics':
                        before=state.selected_topic;state.seed_topics(event[1])
                        if before is None and state.selected_topic:history.refresh_history(state.selected_topic)
                    elif event[0]=='history':
                        state.add_history(event[1],event[2])
                        if screen.page=='monitor':screen.notice='历史已更新；新消息实时显示'
                    elif screen.page=='monitor':screen.notice=event[1]
            now=time.monotonic()
            if screen.poll_media():dirty=True
            if screen.page=='media_view' and now-last_media_status>=1:
                dirty=True;last_media_status=now
            if dirty and now-last_draw>=(.1 if screen.page=='media_view' else .2):
                draw_start=time.monotonic()
                frame=screen.draw()
                draw_done=time.monotonic()
                native_frame=image.from_bytes(640,480,image.Format.FMT_RGB888,frame.tobytes())
                convert_done=time.monotonic()
                disp.show(native_frame)
                native_frame=None
                show_done=time.monotonic()
                last_draw=draw_start;dirty=False
                marker=Path('/tmp/siot-monitor-profile')
                if marker.exists():
                    profile.append((draw_start,draw_done,convert_done,show_done))
                    if len(profile)>=30:
                        values={name:[(row[index+1]-row[index])*1000 for row in profile] for index,name in enumerate(('draw_ms','convert_ms','show_ms'))}
                        report={name:{'mean':sum(v)/len(v),'max':max(v)} for name,v in values.items()}
                        report['submitted_fps']=(len(profile)-1)/(profile[-1][3]-profile[0][3])
                        out=Path('/tmp/siot-monitor');out.mkdir(mode=0o700,exist_ok=True)
                        (out/'render-profile.json').write_text(json.dumps(report))
                        marker.unlink(missing_ok=True);profile=[]
                else:profile=[]
                # First frame plus explicit on-demand snapshots; no periodic image writes.
                capture=Path('/tmp/siot-monitor-capture')
                if not preview_done or capture.exists():
                    out=Path('/tmp/siot-monitor');out.mkdir(mode=0o700,exist_ok=True)
                    frame.save(out/'preview.jpg',quality=85);preview_done=True
                    if capture.exists():capture.unlink()
            if Path('/tmp/siot-monitor-capture').exists():dirty=True
            stop.wait(.01 if screen.page=='media_view' else .02)
    finally:
        app.set_exit_flag(True)
        stop.set();reader.join(timeout=2)
        try:screen.stop_media()
        except Exception as exc:print('Media cleanup:',type(exc).__name__,flush=True)
        if history:history.close()
        if network:network.close()
        if not reader.is_alive():touch.close()
        # Run native destructors before MaixPy's process-wide atexit cleanup.
        # Display.close() alone does not destroy its two private CMM pools.
        disp=None
        if not reader.is_alive():touch=None
        import gc
        gc.collect()
    return 0


if __name__=='__main__':
    sys.exit(main())
