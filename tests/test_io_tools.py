import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'xiaozhi' / 'service'))
try:
    io = importlib.import_module('io_tools')
except ModuleNotFoundError:
    io = None


class IOTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(io, 'External I/O implementation is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'policy.json'

    def create(self, targets=None):
        self.path.write_text(json.dumps({'version': 1, 'targets': targets or {}}))
        self.path.chmod(0o600)
        return io.ExternalIO(str(self.path))

    def i2c(self):
        return {'kind': 'i2c', 'bus': 6, 'address': 32,
                'read_registers': [0, 1], 'write_registers': [4, 5]}

    def gpio(self):
        return {'kind': 'gpio', 'pin': 'A4', 'direction': 'output', 'initial': 0, 'safe': 0}

    def test_missing_and_empty_policy_denies_without_hardware(self):
        with patch.object(io, '_LinuxDriver') as driver:
            for obj in (io.ExternalIO(str(self.path)), self.create()):
                self.assertEqual(obj.list_resources()['resources'], [])
                with self.assertRaisesRegex(PermissionError, 'configured'):
                    obj.gpio_read('A4')
                obj.close()
            driver.assert_not_called()

    def test_rejects_board_internal_resources_and_unknown_keys(self):
        bad = [dict(self.i2c(), bus=0), dict(self.i2c(), bus=1), dict(self.i2c(), address=7),
               dict(self.i2c(), address=120), dict(self.i2c(), bus=8),
               dict(self.gpio(), pin='A6'), dict(self.gpio(), pin='B25'),
               dict(self.gpio(), pin='C2'), dict(self.gpio(), pin='A30'),
               dict(self.gpio(), pin='A2'), dict(self.gpio(), device='/dev/gpiochip2'),
               {'kind': 'uart', 'port': 0, 'baud': 115200},
               {'kind': 'uart', 'port': 4, 'baud': 115200},
               {'kind': 'spi', 'device': '/dev/spidev2.3', 'mode': 0, 'speed_hz': 100000}]
        with patch.object(io, '_LinuxDriver') as driver:
            for target in bad:
                with self.subTest(target=target), self.assertRaises((ValueError, PermissionError)):
                    self.create({'bad': target})
            driver.assert_not_called()

    def test_pin_conflicts_and_output_without_safe_state_rejected(self):
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.create({'uart': {'kind':'uart', 'port':2, 'baud':115200},
                         'gpio': dict(self.gpio(), pin='B0')})
        with self.assertRaises(ValueError):
            self.create({'gpio': {'kind':'gpio', 'pin':'A4', 'direction':'output'}})
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.create({'a':self.i2c(), 'b':self.i2c()})

    def test_gpio_direction_values_and_serializable_result(self):
        obj = self.create({'out': self.gpio(), 'in': {'kind':'gpio','pin':'B2','direction':'input'}})
        with patch.object(io, '_LinuxDriver') as cls:
            cls.return_value.gpio_read.return_value = 1
            self.assertEqual(obj.gpio_read('in')['value'], 1)
            self.assertEqual(obj.gpio_write('out', 1)['value'], 1)
            json.dumps(obj.list_resources())
            for value in (True, '1', -1, 2, 1.0):
                with self.assertRaises(ValueError): obj.gpio_write('out', value)
            with self.assertRaises(PermissionError): obj.gpio_write('in', 1)
            obj.close()
            self.assertTrue(cls.return_value.close.called)
            with self.assertRaises(RuntimeError): obj.gpio_read('in')

    def test_i2c_whitelist_covers_every_register_and_short_reads_fail(self):
        obj = self.create({'sensor': self.i2c()})
        with patch.object(io, '_LinuxDriver') as cls:
            driver = cls.return_value
            driver.i2c_read_register.return_value = b'\x12\x34'
            self.assertEqual(obj.i2c_read_register('sensor', 0, 2)['data_hex'], '1234')
            driver.i2c_write_register.return_value = 2
            self.assertEqual(obj.i2c_write_register('sensor', 4, 'aabb')['bytes_written'], 2)
            for register, length in ((1,2), (2,1), (255,2), (True,1), (0,257), (0,0)):
                with self.assertRaises((ValueError, PermissionError)):
                    obj.i2c_read_register('sensor', register, length)
            for value in ('0', 'zz', 'aa bb', '', '00'*257, 5):
                with self.assertRaises(ValueError): obj.i2c_write_register('sensor', 4, value)
            with self.assertRaises(PermissionError): obj.i2c_write_register('sensor', 5, 'aabb')
            driver.i2c_read_register.return_value = b'\x12'
            with self.assertRaisesRegex(IOError, 'short|length'): obj.i2c_read_register('sensor', 0, 2)
            driver.i2c_write_register.return_value = 1
            with self.assertRaisesRegex(IOError, 'short|length'): obj.i2c_write_register('sensor', 4, 'aabb')

    def test_uart_timeout_and_partial_transfer(self):
        obj = self.create({'serial': {'kind':'uart','port':2,'baud':115200}})
        with patch.object(io, '_LinuxDriver') as cls:
            cls.return_value.uart_read.return_value = b'\x00\xff'
            self.assertEqual(obj.uart_read('serial',2,200)['data_hex'], '00ff')
            for timeout in (0,1001,True,1.5):
                with self.assertRaises(ValueError): obj.uart_read('serial',2,timeout)
            cls.return_value.uart_write.return_value = 1
            with self.assertRaises(IOError): obj.uart_write('serial','aabb')
            cls.return_value.uart_read.return_value = b''
            with self.assertRaises(IOError): obj.uart_read('serial',2)

    def test_spi_lengths_and_read_write_kind_mismatch(self):
        obj = self.create({'spi': {'kind':'spi','device':'/dev/spidev2.1','mode':0,'speed_hz':100000}})
        with patch.object(io, '_LinuxDriver') as cls:
            cls.return_value.spi_transfer.return_value = b'\xff'
            self.assertEqual(obj.spi_transfer('spi', '00')['data_hex'], 'ff')
            with self.assertRaises(PermissionError): obj.gpio_read('spi')
            cls.return_value.spi_transfer.return_value = b''
            with self.assertRaises(IOError): obj.spi_transfer('spi', '00')

    def test_spi_speed_is_bounded_for_maximum_transfer_length(self):
        cfg = {'kind':'spi','device':'/dev/spidev2.1','mode':0,'speed_hz':100000}
        with patch.object(io, '_LinuxDriver') as driver:
            for speed in (1, 9999, 1000001, True, 10000.0):
                with self.subTest(speed=speed), self.assertRaisesRegex(ValueError, '10000..1000000'):
                    self.create({'spi':dict(cfg, speed_hz=speed)})
            for speed in (10000, 1000000):
                obj = self.create({'spi':dict(cfg, speed_hz=speed)})
                self.assertEqual(obj.list_resources()['resources'][0]['speed_hz'], speed)
                obj.close()
            driver.assert_not_called()

    def test_malformed_policy_never_silently_enables_or_disables_targets(self):
        for value in ('{', '[]', '{"version":2,"targets":{}}', '{"version":1,"targets":[],"extra":1}'):
            self.path.write_text(value)
            with self.assertRaises(ValueError): io.ExternalIO(str(self.path))




class LinuxBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(io, 'External I/O implementation is missing')

    def driver(self, cfg):
        return io._LinuxDriver(cfg)

    def test_unknown_or_wrong_pinmux_refuses_before_open(self):
        cfg = {'kind':'i2c','bus':6,'address':32,'read_registers':[0],'write_registers':[]}
        for response in ('GPIOA1', None, RuntimeError('unsupported pin')):
            with patch.object(io, '_pin_function', side_effect=response if isinstance(response, Exception) else None,
                              return_value=response), patch.object(io.os, 'open') as opened:
                with self.assertRaises(RuntimeError): self.driver(cfg).i2c_read_register(0, 1)
                opened.assert_not_called()

    def test_i2c_real_message_layout_register_read_and_write(self):
        import ctypes
        cfg = {'kind':'i2c','bus':6,'address':32,'read_registers':[0],'write_registers':[4]}
        driver = self.driver(cfg)
        calls = []
        def ioctl(fd, code, argument):
            self.assertEqual(code, 0x0707)
            packet = ctypes.cast(argument, ctypes.POINTER(io._I2CRdwr)).contents
            calls.append(packet.nmsgs)
            if packet.nmsgs == 2:
                self.assertEqual(packet.msgs[0].addr, 32)
                self.assertEqual(ctypes.string_at(packet.msgs[0].buf, packet.msgs[0].len), b'\x00')
                self.assertEqual(packet.msgs[1].flags, 1)
                ctypes.memmove(packet.msgs[1].buf, b'\x12\x34', 2)
            else:
                self.assertEqual(ctypes.string_at(packet.msgs[0].buf, packet.msgs[0].len), b'\x04\xaa')
            return packet.nmsgs
        with patch.object(driver, '_ready', return_value=9), patch.object(io, '_ioctl', side_effect=ioctl):
            self.assertEqual(driver.i2c_read_register(0,2), b'\x12\x34')
            self.assertEqual(driver.i2c_write_register(4,b'\xaa'), 1)
            self.assertEqual(calls,[2,1])
        with patch.object(driver, '_ready', return_value=9), patch.object(io, '_ioctl', return_value=1):
            with self.assertRaisesRegex(IOError, 'short'): driver.i2c_read_register(0,2)

    def test_spi_real_transfer_checks_byte_count(self):
        import ctypes
        driver = self.driver({'kind':'spi','device':'/dev/spidev2.1','mode':0,'speed_hz':100000})
        def ioctl(fd, code, argument):
            packet = ctypes.cast(argument, ctypes.POINTER(io._SPITransfer)).contents
            self.assertEqual(packet.len, 2)
            self.assertEqual(packet.speed_hz,100000)
            self.assertEqual(ctypes.string_at(packet.tx_buf,2), b'\x01\x02')
            ctypes.memmove(packet.rx_buf,b'\x03\x04',2)
            return 2
        with patch.object(driver,'_ready',return_value=9), patch.object(io,'_ioctl',side_effect=ioctl):
            self.assertEqual(driver.spi_transfer(b'\x01\x02'),b'\x03\x04')
        with patch.object(driver,'_ready',return_value=9), patch.object(io,'_ioctl',return_value=1):
            with self.assertRaisesRegex(IOError,'short'): driver.spi_transfer(b'\x01\x02')

    def test_gpio_close_sets_safe_before_fd_release(self):
        driver = self.driver({'kind':'gpio','pin':'A4','direction':'output','initial':0,'safe':0})
        driver.fd = 10
        order=[]
        def ioctl(fd,code,arg):
            self.assertEqual(code,0xC040B409)
            self.assertEqual(arg[0],0)
            order.append('safe')
            return 0
        with patch.object(io,'_ioctl',side_effect=ioctl), patch.object(io.os,'close',side_effect=lambda fd:order.append('close')):
            driver.close()
        self.assertEqual(order,['safe','close'])
        self.assertIsNone(driver.fd)

    def test_gpio_safe_failure_retains_handle_for_retry(self):
        driver = self.driver({'kind':'gpio','pin':'A4','direction':'output','initial':0,'safe':0})
        driver.fd=10
        with patch.object(io,'_ioctl',side_effect=OSError('failure')), patch.object(io.os,'close') as closed:
            with self.assertRaises(OSError): driver.close()
            closed.assert_not_called()
        self.assertEqual(driver.fd,10)

class AdditionalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(io, 'External I/O implementation is missing')

    def test_occupied_device_detection_fails_closed(self):
        self.assertTrue(hasattr(io, '_device_users'), 'device occupancy check is missing')
        with patch.object(io, '_device_users', return_value=[(123, 'app')]):
            with self.assertRaisesRegex(RuntimeError,'in use'):
                io._assert_device_available('/dev/ttyS2')
        with patch.object(io, '_device_users', side_effect=PermissionError('proc denied')):
            with self.assertRaisesRegex(RuntimeError,'occupancy'):
                io._assert_device_available('/dev/ttyS2')

    def test_uart_low_level_partial_io_and_deadline(self):
        driver = io._LinuxDriver({'kind':'uart','port':2,'baud':115200})
        with patch.object(driver,'_ready',return_value=9), patch.object(io.select,'select',return_value=([9],[9],[])), \
                patch.object(io.os,'read',side_effect=[b'a',b'b']), patch.object(io.os,'write',side_effect=[1,1]):
            self.assertEqual(driver.uart_read(2,200),b'ab')
            self.assertEqual(driver.uart_write(b'ab'),2)
        with patch.object(driver,'_ready',return_value=9), patch.object(io.select,'select',return_value=([],[],[])):
            self.assertEqual(driver.uart_read(2,200),b'')
            self.assertEqual(driver.uart_write(b'ab'),0)

    def test_recheck_spi_mode_on_every_access(self):
        driver = io._LinuxDriver({'kind':'spi','device':'/dev/spidev2.1','mode':0,'speed_hz':100000})
        driver.fd = 9
        def changed_mode(fd,code,buf):
            buf[0]=1
            return 0
        with patch.object(driver,'_check_pins'), patch.object(io,'_ioctl',side_effect=changed_mode):
            with self.assertRaisesRegex(RuntimeError,'mode'): driver._ready()


class OpenAndCloseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(io, 'External I/O implementation is missing')

    def fake_os(self):
        from contextlib import ExitStack
        import stat
        import types
        stack = ExitStack()
        stack.enter_context(patch.dict(sys.modules, {'fcntl':types.SimpleNamespace(LOCK_EX=2,LOCK_NB=4,flock=Mock())}))
        stack.enter_context(patch.object(io.os,'name','posix'))
        stack.enter_context(patch.multiple(io.os,O_CLOEXEC=0,O_NOFOLLOW=0,O_NONBLOCK=0,create=True))
        stack.enter_context(patch.object(io.os,'open',return_value=9))
        stack.enter_context(patch.object(io.os,'fstat',return_value=types.SimpleNamespace(st_mode=stat.S_IFCHR)))
        stack.enter_context(patch.object(io.os,'set_inheritable'))
        close = stack.enter_context(patch.object(io.os,'close'))
        return stack,close

    def test_gpio_kernel_exclusive_request_and_safe_initial_value(self):
        import struct
        driver=io._LinuxDriver({'kind':'gpio','pin':'B2','direction':'output','initial':0,'safe':0})
        def ioctl(fd,code,request):
            self.assertEqual(code,0xC16CB403)
            self.assertEqual(len(request),364)
            self.assertEqual(struct.unpack_from('=I',request,0)[0],2)
            self.assertEqual(struct.unpack_from('=I',request,256)[0],2)
            self.assertEqual(request[260],0)
            self.assertEqual(struct.unpack_from('=I',request,356)[0],1)
            struct.pack_into('=i',request,360,12)
            return 0
        stack,closed=self.fake_os()
        with stack, patch.object(driver,'_check_pins'), patch.object(io,'_ioctl',side_effect=ioctl):
            self.assertEqual(driver._ready(),12)
            io.os.open.assert_called_once_with('/dev/gpiochip1',io.os.O_RDWR)
            closed.assert_called_once_with(9)

    def test_gpio_busy_propagates_and_closes_chip(self):
        driver=io._LinuxDriver({'kind':'gpio','pin':'A4','direction':'input'})
        stack,closed=self.fake_os()
        with stack, patch.object(driver,'_check_pins'), patch.object(io,'_ioctl',side_effect=OSError(16,'device busy')):
            with self.assertRaisesRegex(OSError,'busy'): driver._ready()
            closed.assert_called_once_with(9)
            self.assertIsNone(driver.fd)

    def test_uart_exclusive_raw_configuration_and_restore(self):
        import types
        term=types.SimpleNamespace(TIOCEXCL=1,TIOCNXCL=2,TCSANOW=0,CLOCAL=1,CREAD=2,CS8=4,
                                   B115200=4098,VMIN=0,VTIME=1,
                                   tcgetattr=Mock(return_value=[1,2,3,4,5,6,[7,8]]),tcsetattr=Mock())
        driver=io._LinuxDriver({'kind':'uart','port':2,'baud':115200})
        stack,closed=self.fake_os()
        with stack, patch.dict(sys.modules,{'termios':term}), patch.object(driver,'_check_pins'), \
                patch.object(io,'_assert_device_available'),patch.object(io,'_ioctl') as ioctl:
            self.assertEqual(driver._ready(),9)
            ioctl.assert_called_once_with(9,1,None)
            self.assertEqual(term.tcsetattr.call_args.args[2],[0,0,7,0,4098,4098,[0,0]])
            driver.close()
            self.assertEqual(term.tcsetattr.call_args.args[2],[1,2,3,4,5,6,[7,8]])
            self.assertEqual(ioctl.call_args.args,(9,2,None))
            closed.assert_called_once_with(9)

class BusyAndPolicyMetadataTests(unittest.TestCase):
    setUp = IOTests.setUp
    create = IOTests.create
    gpio = IOTests.gpio

    def test_busy_operation_refuses_without_queueing_or_hardware(self):
        obj = self.create({'out':self.gpio()})
        import threading
        result = []
        def call():
            try: obj.gpio_write('out',1)
            except Exception as exc: result.append(exc)
        with patch.object(io,'_LinuxDriver') as driver:
            obj._lock.acquire()
            thread = threading.Thread(target=call,daemon=True)
            thread.start()
            thread.join(0.1)
            try:
                self.assertFalse(thread.is_alive(),'busy operation incorrectly queues behind hardware')
                self.assertEqual(len(result),1)
                self.assertIsInstance(result[0],RuntimeError)
                self.assertIn('busy',str(result[0]))
                driver.assert_not_called()
            finally:
                obj._lock.release()
                thread.join(1)

if __name__ == '__main__': unittest.main()
