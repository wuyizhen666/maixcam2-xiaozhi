import asyncio
import importlib
import json
from pathlib import Path
import sys
import types
import threading
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'xiaozhi'/'service'))
# Audio codecs are device dependencies; these tests exercise real MCP dispatch only.
_stubs = {'opuslib':types.ModuleType('opuslib'),'pyaudio':types.ModuleType('pyaudio')}
_previous = {name:sys.modules.get(name) for name in _stubs}
try:
    sys.modules.update(_stubs)
    service=importlib.import_module('xiaozhi_service')
finally:
    # Restore only the two codec entries. Restoring the whole modules dict
    # unloads newly imported stdlib C modules and splits exception identities.
    for _name, _module in _previous.items():
        if _module is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _module

class Socket:
    def __init__(self):self.messages=[]
    async def send(self,message):self.messages.append(json.loads(message))

class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        p = patch.object(service,'_get_vision_siot_bridge',return_value=None)
        p.start();self.addCleanup(p.stop)
        from ui_feedback import UiFeedback
        self.ui_threads = []
        self.feedback = UiFeedback(lambda: self.ui_threads.append(threading.get_ident()))
        for name, value in [('feedback', self.feedback)]:
            p = patch.object(service, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(service.S, 'cfg', {'enabled': True, 'popup_enabled': True})
        p.start(); self.addCleanup(p.stop)
        p = patch.object(service.S, 'popup_dismissed', False)
        p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self.feedback.clear()

    async def test_schema_lists_original_and_new_tools(self):
        tools={t.name:t.to_json() for t in service.MCP_TOOLS}
        for name in ['self.audio_speaker.set_volume','self.audio_speaker.get_volume','self.audio_speaker.get_volume_status','self.get_device_status','self.display.show_message','self.camera.take_photo','self.camera.get_status','self.light.set_power','self.light.set_brightness','self.light.get_status']:
            self.assertIn(name,tools)
        schema=tools['self.camera.take_photo']['inputSchema']
        self.assertEqual(schema['required'],['question'])
        self.assertEqual(schema['properties']['question']['type'],'string')

    async def test_peripheral_and_external_tools_are_registered(self):
        names={t.name for t in service.MCP_TOOLS}
        expected={'self.led.set_power','self.led.get_status','self.led.restore_system',
            'self.buttons.get_state','self.buttons.get_events','self.imu.read','self.rtc.get_time','self.adc.read_raw',
            'self.io.list_resources','self.gpio.read','self.gpio.write','self.i2c.read_register',
            'self.i2c.write_register','self.spi.transfer','self.uart.read','self.uart.write'}
        self.assertTrue(expected <= names, expected-names)
        self.assertTrue({'self.imu.calibrate_gyro', 'self.imu.get_calibration',
                         'self.imu.clear_calibration'} <= names)

    async def test_tool_pages_respect_utf8_wire_budget_and_do_not_skip_tools(self):
        ws=Socket();server=service.McpServer(ws,lambda:'会话'*20)
        server.tools=[service.McpTool('tool_%02d'%i,'测'*650,[],lambda:'ok') for i in range(12)]
        seen=[];cursor=''
        for page in range(12):
            await server.parse_message({'jsonrpc':'2.0','id':page,'method':'tools/list','params':{'cursor':cursor}})
            message=ws.messages[-1]
            self.assertLessEqual(len(json.dumps(message,ensure_ascii=False).encode('utf-8')),8000)
            result=message['payload']['result'];seen.extend(t['name'] for t in result['tools'])
            if not result.get('nextCursor'):break
            self.assertNotEqual(cursor,result['nextCursor']);cursor=result['nextCursor']
        self.assertEqual(seen,['tool_%02d'%i for i in range(12)])
        await server.close()

    async def test_invalid_cursor_and_oversized_single_schema_return_errors(self):
        ws=Socket();server=service.McpServer(ws,lambda:'session')
        await server.parse_message({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{'cursor':'does-not-exist'}})
        self.assertIn('error',ws.messages[-1]['payload'])
        server.tools=[service.McpTool('large','测'*3000,[],lambda:'ok')]
        await server.parse_message({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
        self.assertIn('error',ws.messages[-1]['payload'])
        await server.close()

    async def test_async_tool_result_is_awaited(self):
        async def callback():await asyncio.sleep(0);return {'ok':True}
        tool=service.McpTool('test','test',[],callback)
        result=json.loads(await tool.call({}))
        self.assertFalse(result['isError'])
        self.assertEqual(json.loads(result['content'][0]['text']),{'ok':True})

    async def test_invalid_schema_arguments_return_error_without_callback(self):
        called=[]
        tool=service.McpTool('light','light',[service.Property('brightness','integer',None,0,100)],lambda **kw:called.append(kw))
        for args in [{},{'brightness':True},{'brightness':2.5},{'brightness':'40'},{'brightness':101},[]]:
            with self.subTest(args=args):self.assertTrue(json.loads(await tool.call(args))['isError'])
        self.assertEqual(called,[])

    async def test_initialize_reads_vision_and_clear_on_missing_capability(self):
        self.assertTrue(hasattr(service,'hardware'),'hardware bridge missing')
        ws=Socket();server=service.McpServer(ws,lambda:'session-A')
        await server.parse_message({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'capabilities':{'vision':{'url':'https://vision.example','token':'secret'}}}})
        self.assertTrue(service.hardware.camera_status()['vision_configured'])
        self.assertNotIn('secret',json.dumps(ws.messages))
        await server.parse_message({'jsonrpc':'2.0','id':2,'method':'initialize','params':{}})
        self.assertFalse(service.hardware.camera_status()['vision_configured'])

    async def test_slow_tool_does_not_block_dispatch_and_reply_is_session_bound(self):
        self.assertTrue(hasattr(service.McpServer,'dispatch'),'non-blocking dispatch missing')
        old=Socket();new=Socket();gate=asyncio.Event()
        async def callback():await gate.wait();return 'finished'
        server=service.McpServer(old,lambda:'old-session')
        server.tools=[service.McpTool('slow','slow',[],callback)]
        await asyncio.wait_for(server.dispatch({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'slow','arguments':{}}}),.2)
        service.S.ws=new;service.S.session_id='new-session'
        gate.set();await asyncio.sleep(.02)
        self.assertEqual(new.messages,[])
        self.assertEqual(old.messages[0]['session_id'],'old-session')
        await server.close()

    async def test_close_cancels_pending_tool_without_late_reply(self):
        self.assertTrue(hasattr(service.McpServer,'dispatch'),'non-blocking dispatch missing')
        ws=Socket();gate=asyncio.Event()
        async def callback():await gate.wait();return 'late'
        server=service.McpServer(ws,lambda:'old-session')
        server.tools=[service.McpTool('slow','slow',[],callback)]
        await server.dispatch({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'slow'}})
        await asyncio.sleep(0);await server.close();gate.set();await asyncio.sleep(0)
        self.assertEqual(ws.messages,[])

    async def test_failed_tool_still_shows_its_name_until_session_closes(self):
        ws = Socket(); gate = asyncio.Event()
        async def callback():
            await gate.wait()
            raise RuntimeError('failure')
        server = service.McpServer(ws, lambda: 'session')
        server.tools = [service.McpTool('self.light.set_power', 'test', [], callback)]
        await server.dispatch({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'self.light.set_power'}})
        await asyncio.sleep(0)
        self.assertEqual(self.feedback.tool_name, 'self.light.set_power')
        gate.set(); await asyncio.sleep(.02)
        self.assertTrue(ws.messages[0]['payload']['result']['isError'])
        self.assertEqual(self.feedback.tool_name, 'self.light.set_power')
        await server.close()
        self.assertEqual(self.feedback.tool_name, '')

    async def test_imu_panel_uses_exact_mcp_sample_without_a_second_read(self):
        sample={'model':'LSM6DSOWTR','acceleration_m_s2':{'x':1.,'y':2.,'z':3.},
                'angular_velocity_deg_s':{'x':4.,'y':5.,'z':6.},'temperature_c':None,
                'gyro_calibrated':False,'timestamp_unix':1234.5}
        ws=Socket();server=service.McpServer(ws,lambda:'session')
        with patch.object(service.imu_reader,'read',return_value=sample) as reader:
            await server.parse_message({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'self.imu.read'}})
        value=json.loads(ws.messages[-1]['payload']['result']['content'][0]['text'])
        self.assertEqual(self.feedback.imu_sample,value)
        self.assertEqual(value,sample);self.assertEqual(reader.call_count,1)
        await server.close();self.assertIsNone(self.feedback.imu_sample)

    async def test_failed_imu_call_clears_previous_displayed_sample(self):
        ws=Socket();server=service.McpServer(ws,lambda:'session')
        self.feedback.show_imu(server,{'old':True})
        with patch.object(service.imu_reader,'read',side_effect=RuntimeError('sensor unavailable')):
            await server.parse_message({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'self.imu.read'}})
        self.assertTrue(ws.messages[-1]['payload']['result']['isError'])
        self.assertIsNone(self.feedback.imu_sample)
        await server.close()

    async def _wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 2)

    async def _imu_call(self, server, name='self.imu.read', rid=1):
        await server.parse_message({'jsonrpc':'2.0', 'id':rid, 'method':'tools/call',
                                    'params':{'name':name}})

    async def test_live_imu_updates_after_panel_collapse_without_extra_mcp_replies(self):
        ws=Socket(); server=service.McpServer(ws, lambda:'session'); samples=[]
        def read():
            sample={'timestamp_unix':len(samples)+1, 'gyro_calibrated':False}
            samples.append(sample)
            return sample
        # Accelerate only the display timer; sampling remains independently driven.
        schedule=self.feedback._schedule
        with patch.object(service.imu_reader, 'read', side_effect=read), \
             patch.object(service, 'IMU_SAMPLE_INTERVAL', .005), \
             patch.object(service, 'IMU_DRAW_INTERVAL', .01), \
             patch.object(self.feedback, '_schedule', side_effect=lambda cb,*args,**kw: schedule(cb,*args,seconds=.04)):
            try:
                await self._imu_call(server)
                self.assertTrue(self.feedback.imu_expanded)
                await self._wait_until(lambda:not self.feedback.imu_expanded)
                previous=self.feedback.imu_sample['timestamp_unix']
                await self._wait_until(lambda:self.feedback.imu_sample['timestamp_unix']>previous)
                self.assertEqual(len(ws.messages),1)
                self.assertFalse(self.feedback.imu_expanded)
                self.assertTrue(all(t==threading.get_ident() for t in self.ui_threads))
            finally:
                await server.close()
            count=len(samples)
            await asyncio.sleep(.03)
            self.assertEqual(len(samples),count)
            self.assertIsNone(self.feedback.imu_sample)

    async def test_dismiss_or_disable_stops_sampling_and_removes_stale_readings(self):
        for reason in ('dismiss', 'enabled', 'popup_enabled'):
            with self.subTest(reason=reason):
                service.S.popup_dismissed=False
                service.S.cfg.update(enabled=True,popup_enabled=True)
                server=service.McpServer(Socket(),lambda:'session')
                with patch.object(service.imu_reader,'read',return_value={'timestamp_unix':1}) as reader, \
                     patch.object(service,'IMU_SAMPLE_INTERVAL',.005):
                    try:
                        await self._imu_call(server)
                        if reason=='dismiss':service.S.popup_dismissed=True
                        else:service.S.cfg[reason]=False
                        await self._wait_until(lambda:server._imu_task.done())
                        count=reader.call_count
                        await asyncio.sleep(.02)
                        self.assertEqual(reader.call_count,count)
                        self.assertIsNone(self.feedback.imu_sample)
                    finally:await server.close()

    async def test_calibration_waits_for_active_read_and_pauses_live_sampling(self):
        ws=Socket(); server=service.McpServer(ws,lambda:'session')
        reading=threading.Event(); release_read=threading.Event()
        calibrating=threading.Event(); release_calibration=threading.Event(); calls=[]
        def read():
            calls.append('read')
            if calls.count('read')==2:
                reading.set(); release_read.wait(2)
            self.assertFalse(calibrating.is_set())
            return {'timestamp_unix':len(calls),'gyro_calibrated':'calibrate' in calls}
        def calibrate():
            self.assertTrue(release_read.is_set())
            calls.append('calibrate'); calibrating.set()
            release_calibration.wait(2); calibrating.clear()
            return {'gyro_calibrated':True}
        with patch.object(service.imu_reader,'read',side_effect=read), \
             patch.object(service.imu_reader,'calibrate_gyro',side_effect=calibrate), \
             patch.object(service,'IMU_SAMPLE_INTERVAL',.005):
            try:
                await self._imu_call(server)
                await self._wait_until(reading.is_set)
                operation=asyncio.create_task(self._imu_call(server,'self.imu.calibrate_gyro',2))
                await asyncio.sleep(.02)
                self.assertFalse(calibrating.is_set())
                release_read.set(); await self._wait_until(calibrating.is_set)
                count=len(calls); await asyncio.sleep(.03)
                self.assertEqual(len(calls),count)
                release_calibration.set(); await operation
                self.assertFalse(ws.messages[-1]['payload']['result']['isError'])
                self.assertTrue(self.feedback.imu_sample['gyro_calibrated'])
            finally:
                release_read.set(); release_calibration.set(); await server.close()

    async def test_calibration_success_is_not_lost_when_preview_read_fails(self):
        ws=Socket(); server=service.McpServer(ws,lambda:'session')
        with patch.object(service.imu_reader,'calibrate_gyro',return_value={'gyro_calibrated':True}), \
             patch.object(service.imu_reader,'read',side_effect=RuntimeError('preview unavailable')):
            try:
                await self._imu_call(server,'self.imu.calibrate_gyro')
                result=ws.messages[-1]['payload']['result']
                self.assertFalse(result['isError'])
                self.assertTrue(json.loads(result['content'][0]['text'])['gyro_calibrated'])
                self.assertIsNone(self.feedback.imu_sample)
            finally:await server.close()

    async def test_repeated_imu_call_waits_for_previous_inflight_read(self):
        ws=Socket(); server=service.McpServer(ws,lambda:'session')
        reading=threading.Event(); release=threading.Event(); calls=[]
        def read():
            self.assertFalse(reading.is_set(), 'concurrent IMU read')
            calls.append(len(calls)+1)
            if len(calls)==2:
                reading.set(); release.wait(2); reading.clear()
            return {'timestamp_unix':len(calls)}
        with patch.object(service.imu_reader,'read',side_effect=read), \
             patch.object(service,'IMU_SAMPLE_INTERVAL',.005):
            try:
                await self._imu_call(server)
                await self._wait_until(reading.is_set)
                old_task=server._imu_task
                second=asyncio.create_task(self._imu_call(server,rid=2))
                await asyncio.sleep(.02)
                self.assertEqual(len(calls),2)
                self.assertFalse(second.done())
                release.set(); await second
                self.assertTrue(old_task.done())
                self.assertIsNot(server._imu_task,old_task)
                self.assertEqual(len(ws.messages),2)
                self.assertFalse(ws.messages[-1]['payload']['result']['isError'])
                self.assertEqual(self.feedback.imu_sample['timestamp_unix'],3)
            finally:release.set(); await server.close()

    async def test_simultaneous_initial_imu_calls_leave_only_one_owned_poll_loop(self):
        ws=Socket(); server=service.McpServer(ws,lambda:'session'); loops=[]; calls=[]
        async def read():
            calls.append(len(calls)+1)
            sequence=calls[-1]
            # Both unlocked calls pass their initial stop before either starts
            # a loop. The locked implementation instead finishes them in order.
            await asyncio.sleep(.03)
            return {'timestamp_unix':sequence}
        async def poll(stop):
            loops.append((stop,asyncio.current_task()))
            await stop.wait()
        server.tools=[service.McpTool('self.imu.read','test',[],read)]
        with patch.object(server,'_poll_imu',side_effect=poll):
            try:
                await asyncio.gather(self._imu_call(server,rid=1),self._imu_call(server,rid=2))
                await asyncio.sleep(0)
                self.assertEqual(len(loops),2)
                self.assertEqual(sum(not stop.is_set() for stop,_ in loops),1)
                self.assertEqual(len(ws.messages),2)
                self.assertTrue(all(not msg['payload']['result']['isError'] for msg in ws.messages))
                await server.close()
                self.assertTrue(all(stop.is_set() and task.done() for stop,task in loops))
            finally:
                await server.close()
                # Also clean the intentionally exposed orphan in RED runs.
                for stop,_ in loops:stop.set()
                await asyncio.gather(*(task for _,task in loops))

    async def test_calibration_status_does_not_sample_and_clear_restarts_raw_live_display(self):
        ws=Socket(); server=service.McpServer(ws,lambda:'session')
        with patch.object(service.imu_reader,'get_calibration',return_value={'gyro_calibrated':True}), \
             patch.object(service.imu_reader,'clear_calibration',return_value={'gyro_calibrated':False}) as clear, \
             patch.object(service.imu_reader,'read',return_value={'gyro_calibrated':False}) as reader:
            try:
                await self._imu_call(server,'self.imu.get_calibration')
                reader.assert_not_called()
                self.assertIsNone(self.feedback.imu_sample)
                await self._imu_call(server,'self.imu.clear_calibration',2)
                clear.assert_called_once_with()
                self.assertFalse(self.feedback.imu_sample['gyro_calibrated'])
                self.assertTrue(self.feedback.imu_expanded)
                self.assertFalse(ws.messages[-1]['payload']['result']['isError'])
            finally:await server.close()

    async def test_capture_previews_on_event_loop_before_upload_finishes(self):
        ws = Socket(); release = threading.Event()
        def capture(question, on_capture=None):
            on_capture(b'captured-jpeg'); release.wait(2); return 'answer'
        server = service.McpServer(ws, lambda: 'session')
        async def wait_preview():
            while self.feedback.photo_jpeg is None: await asyncio.sleep(.005)
        with patch.object(service.hardware, 'take_photo', side_effect=capture):
            try:
                await server.dispatch({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'self.camera.take_photo','arguments':{'question':'what?'}}})
                await asyncio.wait_for(wait_preview(), 1)
                self.assertEqual(self.feedback.photo_jpeg, b'captured-jpeg')
                self.assertEqual(ws.messages, [])
                self.assertTrue(all(t == threading.get_ident() for t in self.ui_threads))
            finally:
                release.set(); await server.close()

    async def test_late_capture_after_close_does_not_reopen_preview(self):
        ws = Socket(); release = threading.Event(); callbacks = []
        def capture(question, on_capture=None):
            callbacks.append(on_capture); release.wait(2); return 'answer'
        server = service.McpServer(ws, lambda: 'session')
        async def wait_started():
            while not callbacks: await asyncio.sleep(.005)
        with patch.object(service.hardware, 'take_photo', side_effect=capture):
            try:
                await server.dispatch({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'self.camera.take_photo','arguments':{'question':'what?'}}})
                await asyncio.wait_for(wait_started(), 1)
                await server.close(); callbacks[0](b'late'); await asyncio.sleep(0)
                self.assertIsNone(self.feedback.photo_jpeg)
                self.assertEqual(self.feedback.tool_name, '')
                self.assertEqual(ws.messages, [])
            finally: release.set()

    async def test_photo_relays_original_jpeg_then_matched_cloud_text(self):
        relay=Mock();relay.begin_capture.return_value='capture-one'
        answer='{"text":"a test object"}'
        def capture(question,on_capture=None):
            on_capture(b'original-jpeg');return answer
        with patch.object(service,'_get_vision_siot_bridge',return_value=relay),patch.object(service.hardware,'take_photo',side_effect=capture):
            result=await service._tool_camera_take_photo('describe')
        self.assertEqual(result,answer)
        relay.begin_capture.assert_called_once_with(b'original-jpeg','describe')
        relay.complete_capture.assert_called_once_with('capture-one','a test object')

    async def test_siot_failure_does_not_replace_cloud_answer(self):
        relay=Mock();relay.begin_capture.side_effect=RuntimeError('relay unavailable')
        def capture(question,on_capture=None):on_capture(b'original');return 'cloud answer'
        with patch.object(service,'_get_vision_siot_bridge',return_value=relay),patch.object(service.hardware,'take_photo',side_effect=capture):
            self.assertEqual(await service._tool_camera_take_photo('describe'),'cloud answer')
        relay.complete_capture.assert_not_called()

    async def test_cloud_error_is_linked_to_the_existing_capture_id(self):
        relay=Mock();relay.begin_capture.return_value='capture-error'
        def capture(question,on_capture=None):
            on_capture(b'original');raise RuntimeError('cloud unavailable')
        with patch.object(service,'_get_vision_siot_bridge',return_value=relay),patch.object(service.hardware,'take_photo',side_effect=capture):
            with self.assertRaises(RuntimeError):await service._tool_camera_take_photo('describe')
        self.assertEqual(relay.complete_capture.call_args.args,('capture-error',''))
        self.assertIn('error',relay.complete_capture.call_args.kwargs)

if __name__=='__main__':unittest.main()
