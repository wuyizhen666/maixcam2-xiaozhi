import asyncio
import queue
import unittest
from unittest.mock import Mock, patch
from test_mcp_hardware import service


class Spotter:
    def __init__(self):
        self.decoded = 0
        self.resets = 0
    def is_ready(self, stream):
        return self.decoded < 2
    def decode_stream(self, stream):
        self.decoded += 1
    def get_result(self, stream):
        return '你好小智' if self.decoded == 1 else ''
    def reset_stream(self, stream):
        self.resets += 1


class WakeDecoderTests(unittest.TestCase):
    def test_intermediate_hit_is_not_lost_to_later_blank(self):
        detector = service.WakeWordDetector()
        detector.ok = True
        detector.stream = Mock()
        detector.spotter = Spotter()
        loop = Mock()
        with patch.object(service.S, 'loop', loop), patch.object(service.S, 'wake_event_q', Mock()):
            detector.feed(b'\0' * 640)
        loop.call_soon_threadsafe.assert_called_once()
        self.assertEqual(detector.spotter.resets, 1)

    def test_cooldown_hit_is_still_reset(self):
        detector = service.WakeWordDetector()
        detector.ok = True
        detector.stream = Mock()
        detector.spotter = Spotter()
        detector.last_hit = service.mono()
        loop = Mock()
        with patch.object(service.S, 'loop', loop):
            detector.feed(b'\0' * 640)
        loop.call_soon_threadsafe.assert_not_called()
        self.assertEqual(detector.spotter.resets, 1)

    def test_reset_failure_is_observable(self):
        detector = service.WakeWordDetector()
        detector.ok = True
        detector.spotter = Mock()
        detector.spotter.create_stream.side_effect = RuntimeError('reset failed')
        with patch.object(service, 'log') as log:
            self.assertIs(detector.fresh_stream(), False)
        log.assert_called()


class WakeCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_close_cannot_delay_or_overwrite_local_recovery(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
        ws = Mock(close=close)
        backlog = queue.Queue()
        backlog.put(b'old audio')
        detector = Mock()
        def reset():
            self.assertTrue(service.S.session_active)
            self.assertTrue(backlog.empty())
            return True
        detector.fresh_stream.side_effect = reset
        with patch.object(service, 'kws', detector), patch.object(service.S, 'session_active', True), \
                patch.object(service.S, 'kws_q', backlog), patch.object(service, 'set_status_icon'), \
                patch.object(service, 'render_popup'), patch.object(service, 'write_state'):
            task = asyncio.create_task(service._finish_session(ws))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(service.S.session_active)
            self.assertEqual(service.S.session_state, 'idle')
            detector.fresh_stream.assert_called_once()
            # A later session can start while the old socket finishes closing.
            service.S.session_state = 'listening'
            service.S.session_active = True
            release.set()
            await task
            self.assertEqual(service.S.session_state, 'listening')
            self.assertTrue(service.S.session_active)


if __name__ == '__main__':
    unittest.main()
