"""On-device SIoT checks using existing Paho 2, without printing credentials."""
import json
from pathlib import Path
import ssl
import statistics
import threading
import time
import urllib.request
import uuid
import paho.mqtt.client as mqtt


def main():
    cfg=json.loads(Path('/etc/siot/monitor.json').read_text())
    relative='verification_'+uuid.uuid4().hex[:10]
    topic=cfg['username']+'/'+relative
    result={'topic':topic};clients=[];received={};lock=threading.Lock();subscribed=threading.Event()
    def client(transport='tcp',port=1883,bad_password=False,tls=False):
        c=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id='verify-'+uuid.uuid4().hex[:12],transport=transport)
        c.username_pw_set(cfg['username'],uuid.uuid4().hex if bad_password else cfg['password'])
        ready=threading.Event();out={}
        def on_connect(client,userdata,flags,reason,properties):out['code']=reason.value;ready.set()
        c.on_connect=on_connect
        if transport=='websockets':c.ws_set_options(path='/ws')
        if tls:
            c.tls_set('/opt/siot/ssl/ca/ca.pem','/opt/siot/ssl/client/cert.pem','/opt/siot/ssl/client/key.pem',cert_reqs=ssl.CERT_REQUIRED)
        c.connect('127.0.0.1',port,keepalive=30);c.loop_start();clients.append(c)
        assert ready.wait(6),'MQTT connection response timeout'
        return c,out['code']
    try:
        def api(path,body=None,token=None,method=None):
            headers={'Content-Type':'application/json'}
            if token:headers['authorization']=token
            request=urllib.request.Request('http://127.0.0.1:8080/api/v2'+path,
                                          data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
            with urllib.request.urlopen(request,timeout=5) as response:return json.loads(response.read())
        login=api('/login',{'username':cfg['username'],'password':cfg['password']})
        token=login['data']['token'];result['web_login']=bool(token)
        topics=api('/topics?length=20&offset=0',token=token);result['topics_api']=topics.get('code')==0
        created=api('/topics',{'name':relative,'description':'Deployment verification'},token,method='PUT')
        assert created.get('code')==0,'Could not create account-owned topic'
        sub,code=client();assert code==0;result['mqtt_connect']=True
        def on_message(client,userdata,message):
            try:body=json.loads(message.payload)
            except ValueError:return
            if body.get('probe')!=topic:return
            with lock:received[body['seq']]={'body':body,'received_ns':time.monotonic_ns()}
        sub.on_message=on_message
        sub.on_subscribe=lambda *args:subscribed.set()
        sub.subscribe(topic,qos=1);assert subscribed.wait(5),'No SUBACK'
        pub,code=client();assert code==0
        sent={};start=time.monotonic()
        for seq in range(200):
            target=start+seq*.05
            if target>time.monotonic():time.sleep(target-time.monotonic())
            sent[seq]=time.monotonic_ns()
            info=pub.publish(topic,json.dumps({'probe':topic,'seq':seq,'value':'娴嬭瘯娑堟伅'},ensure_ascii=False),qos=0,retain=False)
            assert info.rc==mqtt.MQTT_ERR_SUCCESS
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with lock:count=sum(seq in received for seq in sent)
            if count==len(sent):break
            time.sleep(.05)
        with lock:
            latencies=[(received[k]['received_ns']-v)/1e6 for k,v in sent.items() if k in received]
        result['stream']={'sent':len(sent),'received':len(latencies),'target_hz':20,
                          'p50_ms':round(statistics.median(latencies),3),
                          'p95_ms':round(sorted(latencies)[int(len(latencies)*.95)-1],3),
                          'max_ms':round(max(latencies),3)}
        assert len(latencies)==200,'MQTT stream lost messages'
        info=pub.publish(topic,json.dumps({'probe':topic,'seq':200,'value':'淇濆瓨娴嬭瘯'},ensure_ascii=False),qos=1,retain=False)
        info.wait_for_publish(timeout=5);assert info.is_published()
        bad,code=client(bad_password=True);result['wrong_password_rejected']=code!=0;assert code!=0
        bad.disconnect();bad.loop_stop()
        for name,transport,port,tls in [('websocket','websockets',1888,False),('tls','tcp',8883,True)]:
            c,code=client(transport,port,tls=tls);result[name+'_connect']=code==0;assert code==0
            info=c.publish(topic,json.dumps({'probe':topic,'seq':port,'value':name}),qos=1,retain=False)
            info.wait_for_publish(timeout=5);assert info.is_published()
        path='/messages/getMsgByTopic?length=10&offset=0&startTime=%d&endTime=%d'%(int(time.time())-120,int(time.time())+60)
        messages=api(path,{'topic':topic},token)
        result['saved_message_visible']=any(topic in str(x.get('content','')) for x in messages.get('data',{}).get('messages',[]))
        assert result['saved_message_visible'],'Saved test message absent from history API'
        result['passed']=True
    finally:
        for c in clients:
            c.disconnect();c.loop_stop()
        root=Path('/tmp/siot-install-2618');(root/'verification.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
