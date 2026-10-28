import struct
import unittest
from unittest.mock import patch
from test_mcp_hardware import service


class AudioVolumeTests(unittest.TestCase):
    def test_mute_never_changes_capture_mixer_controls(self):
        with patch.object(service.S, 'muted', True), patch.object(service.S, 'cfg', {'volume':100}), \
                patch.object(service.os, 'system') as command:
            service.apply_volume()
        command.assert_not_called()

    def test_muted_pcm_is_silence_of_same_length(self):
        data = struct.pack('5h',-20000,-1000,0,1000,20000)
        with patch.object(service.S, 'muted', True), patch.object(service.S, 'cfg', {'volume':100}):
            service.apply_volume()
            self.assertEqual(service.scale_playback_pcm(data), bytes(len(data)))

    def test_half_volume_scales_only_playback_samples(self):
        data = struct.pack('5h',-20000,-1000,0,1000,20000)
        with patch.object(service.S, 'muted', False), patch.object(service.S, 'cfg', {'volume':50}):
            service.apply_volume()
            self.assertEqual(struct.unpack('5h', service.scale_playback_pcm(data)),
                             (-10000,-500,0,500,10000))

    def test_full_volume_preserves_pcm_and_zero_volume_silences(self):
        data = struct.pack('3h',-32768,0,32767)
        with patch.object(service.S, 'muted', False), patch.object(service.S, 'cfg', {'volume':100}):
            service.apply_volume()
            self.assertEqual(service.scale_playback_pcm(data), data)
            service.S.cfg['volume'] = 0
            service.apply_volume()
            self.assertEqual(service.scale_playback_pcm(data), bytes(len(data)))


if __name__ == '__main__':
    unittest.main()
