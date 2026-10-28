import unittest
import struct
from test_mcp_hardware import service


class TouchHoldTests(unittest.TestCase):
    def test_kernel_snapshot_release_is_not_a_cached_press(self):
        state = service.Maixcam2TouchState.__new__(service.Maixcam2TouchState)
        state.fd = 123
        state.slot_count = 6
        pressed = [True]
        active = [2]
        def ioctl(fd, request, buffer, mutate):
            self.assertEqual(fd,123)
            if request in (0x80184575, 0x80184576):
                # Real hyn_ts exposes zero here; coordinates live in slots.
                buffer[:] = bytes(24)
            elif request & 0xffff == 0x450a:
                code = struct.unpack_from('i',buffer)[0]
                values = [-1]*6 if code == 57 else [0]*6
                for slot in active:
                    values[slot] = slot if code == 57 else (455 if code == 53 else 29)
                buffer[:] = struct.pack('7i',code,*values)
            elif request == 0x80604518 and pressed[0]:
                buffer[330//8] |= 1 << (330%8)
        state.ioctl = ioctl
        self.assertEqual(state.read(), (29,24,True))
        pressed[0] = False
        self.assertFalse(state.read()[2])
        pressed[0] = True
        active.append(3)
        self.assertFalse(state.read()[2], 'Multiple fingers must cancel the hold')

    def test_short_tap_does_not_wake_and_two_seconds_does(self):
        hold = service.TouchHoldToWake()
        self.assertFalse(hold.update(29,24,True,True,0))
        self.assertFalse(hold.update(29,24,True,True,1.999))
        self.assertTrue(hold.update(29,24,True,True,2.0))
        self.assertFalse(hold.update(29,24,True,True,8))

    def test_other_screen_locations_never_wake(self):
        for point in [(320,240),(620,20),(100,24),(52,24),(29,48),(-1,24)]:
            hold = service.TouchHoldToWake()
            self.assertFalse(hold.update(*point,True,True,0))
            self.assertFalse(hold.update(*point,True,True,20))

    def test_recorded_icon_edge_holds_but_not_center_tap(self):
        hold = service.TouchHoldToWake()
        hits = []
        for start, end, x, y in [(221.667,224.751,1,0), (232.784,236.422,1,2),
                                  (237.889,238.193,369,305)]:
            t = start
            while t < end:
                if hold.update(x,y,True,True,t):
                    hits.append(t)
                t += .05
            hold.update(x,y,False,True,end)
        self.assertEqual(len(hits), 2)
        self.assertGreaterEqual(hits[0]-221.667, 2)
        self.assertGreaterEqual(hits[1]-232.784, 2)

    def test_move_out_resets_the_two_second_timer(self):
        hold = service.TouchHoldToWake()
        hold.update(29,24,True,True,0)
        self.assertFalse(hold.update(100,100,True,True,1.5))
        self.assertFalse(hold.update(29,24,True,True,2))
        self.assertFalse(hold.update(29,24,True,True,3.9))
        self.assertTrue(hold.update(29,24,True,True,4))

    def test_release_resets_and_allows_a_new_gesture(self):
        hold = service.TouchHoldToWake()
        hold.update(29,24,True,True,0)
        self.assertFalse(hold.update(29,24,False,True,1))
        self.assertFalse(hold.update(29,24,True,True,2))
        self.assertTrue(hold.update(29,24,True,True,4))
        hold.update(29,24,False,True,5)
        hold.update(29,24,True,True,6)
        self.assertTrue(hold.update(29,24,True,True,8))

    def test_disable_resets_timer_but_does_not_rearm_held_finger(self):
        hold = service.TouchHoldToWake()
        hold.update(29,24,True,True,0)
        hold.update(29,24,True,False,1)
        self.assertFalse(hold.update(29,24,True,True,3))
        self.assertTrue(hold.update(29,24,True,True,5))
        hold.update(29,24,True,False,6)
        self.assertFalse(hold.update(29,24,True,True,10))


if __name__ == '__main__':
    unittest.main()
