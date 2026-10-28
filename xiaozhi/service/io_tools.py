"""Default-deny external I/O. Importing/listing never opens a hardware device.

The local policy is loaded once. RPC callers select logical targets only. Pinmux
is inspected, never changed. Linux drivers perform actual transfers; tests replace
only OS boundaries. A successful transfer is not proof of an attached device's
identity or of application-level behavior.
"""
import ctypes
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import select
import stat
import struct
import threading
import time

MAX_BYTES = 256
GPIO_PINS = {'A4', 'A8', 'A9', 'B0', 'B1', 'B2', 'B3', 'B18', 'B19', 'B20', 'B21', 'A31'}
UART_BAUDS = {9600, 19200, 38400, 57600, 115200}


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('%s must be an integer in %d..%d' % (name, low, high))
    return value


def _hex(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}){1,256}', value):
        raise ValueError('data_hex must contain 1..256 bytes as consecutive hex pairs')
    return bytes.fromhex(value)


def _pins(cfg):
    kind = cfg['kind']
    if kind == 'gpio':
        pin = cfg['pin']
        return {pin: 'GPIO' + pin}
    if kind == 'i2c':
        return ({'A1':'I2C6_SCL', 'A0':'I2C6_SDA'} if cfg['bus'] == 6 else
                {'A8':'I2C7_SCL', 'A9':'I2C7_SDA'})
    if kind == 'spi':
        return {'B18':'SPI2_MOSI', 'B19':'SPI2_MISO', 'B20':'SPI2_SCK', 'B21':'SPI2_CS1'}
    return {'B0':'UART2_TX', 'B1':'UART2_RX'}


def _validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {'version', 'targets'}:
        raise ValueError('policy requires only version and targets')
    if type(policy['version']) is not int or policy['version'] != 1 or not isinstance(policy['targets'], dict):
        raise ValueError('unsupported policy version or targets')
    used = set()
    for name, cfg in policy['targets'].items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name):
            raise ValueError('invalid logical target name')
        if not isinstance(cfg, dict):
            raise ValueError('target configuration must be an object')
        kind = cfg.get('kind')
        if kind == 'gpio':
            keys = {'kind', 'pin', 'direction'}
            if not isinstance(cfg.get('pin'), str) or cfg['pin'] not in GPIO_PINS or cfg.get('direction') not in ('input', 'output'):
                raise ValueError('GPIO pin or direction is not allowed')
            if cfg['direction'] == 'output':
                keys |= {'initial', 'safe'}
                _integer(cfg.get('initial'), 'initial', 0, 1)
                _integer(cfg.get('safe'), 'safe', 0, 1)
        elif kind == 'i2c':
            keys = {'kind', 'bus', 'address', 'read_registers', 'write_registers'}
            if type(cfg.get('bus')) is not int or cfg['bus'] not in (6, 7):
                raise ValueError('only external I2C6 and I2C7 are allowed')
            _integer(cfg.get('address'), 'address', 0x08, 0x77)
            for key in ('read_registers', 'write_registers'):
                values = cfg.get(key)
                if not isinstance(values, list) or len(values) > 256:
                    raise ValueError(key + ' must be a register allowlist')
                for register in values:
                    _integer(register, 'register', 0, 255)
                if len(set(values)) != len(values):
                    raise ValueError('duplicate register')
        elif kind == 'spi':
            keys = {'kind', 'device', 'mode', 'speed_hz'}
            if cfg.get('device') != '/dev/spidev2.1':
                raise ValueError('only /dev/spidev2.1 is allowed')
            _integer(cfg.get('mode'), 'mode', 0, 3)
            # At most 256 bytes: nominal wire time is <= 0.2048 seconds.
            # Slower protocols require a separate timing/safety assessment.
            _integer(cfg.get('speed_hz'), 'speed_hz', 10000, 1000000)
        elif kind == 'uart':
            keys = {'kind', 'port', 'baud'}
            if type(cfg.get('port')) is not int or cfg['port'] != 2:
                raise ValueError('only external UART2 is allowed')
            if type(cfg.get('baud')) is not int or cfg['baud'] not in UART_BAUDS:
                raise ValueError('unsupported UART baud')
        else:
            raise ValueError('unknown I/O kind')
        if set(cfg) != keys:
            raise ValueError('target has missing or unknown keys')
        pins = set(_pins(cfg))
        if pins & used:
            raise ValueError('target pin conflict: ' + ', '.join(sorted(pins & used)))
        used |= pins
    return policy['targets']


def _load_policy(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return {}, False
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError('policy must be a regular local file')
        if os.name == 'posix' and (info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022):
            raise PermissionError('policy must be owned by root/service user and not group/world writable')
        raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError('policy exceeds 64 KiB')
        def unique_object(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('duplicate policy key: ' + key)
                result[key] = value
            return result
        policy = json.loads(raw, object_pairs_hook=unique_object)
        return _validate_policy(policy), True


class ExternalIO:
    def __init__(self, policy_path='/root/xiaozhi/io_access.json'):
        self._targets, self._configured = _load_policy(policy_path)
        self._lock = threading.Lock()
        self._drivers = {}
        self._closed = False

    @contextmanager
    def _operation(self):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('external I/O is busy; previous operation may still be running')
        try:
            yield
        finally:
            self._lock.release()

    def list_resources(self):
        with self._operation():
            return {'policy_configured':self._configured,
                    'hardware_verified':False,
                    'resources':[dict(name=name, **cfg) for name,cfg in self._targets.items()]}

    def _target(self, target, kind):
        if self._closed:
            raise RuntimeError('external I/O is closed')
        if not isinstance(target, str) or target not in self._targets:
            raise PermissionError('logical target is not configured')
        cfg = self._targets[target]
        if cfg['kind'] != kind:
            raise PermissionError('logical target is not configured for ' + kind)
        return cfg

    def _driver(self, target, cfg):
        if target not in self._drivers:
            self._drivers[target] = _LinuxDriver(cfg)
        return self._drivers[target]

    def gpio_read(self, target):
        with self._operation():
            cfg = self._target(target, 'gpio')
            value = self._driver(target,cfg).gpio_read()
            _integer(value, 'GPIO response', 0, 1)
            return {'target':target, 'value':value}

    def gpio_write(self, target, value):
        _integer(value, 'value', 0, 1)
        with self._operation():
            cfg = self._target(target, 'gpio')
            if cfg['direction'] != 'output':
                raise PermissionError('GPIO target is not an output')
            self._driver(target,cfg).gpio_write(value)
            return {'target':target, 'value':value}

    def _registers(self, cfg, register, length, permission):
        _integer(register, 'register', 0, 255)
        _integer(length, 'length', 1, MAX_BYTES)
        if register + length > 256:
            raise ValueError('register range exceeds 8-bit addressing')
        if not set(range(register, register + length)) <= set(cfg[permission]):
            raise PermissionError('register range is not allowed for this operation')

    @staticmethod
    def _read_result(target, data, length):
        if not isinstance(data, bytes) or len(data) != length:
            raise IOError('short or invalid read length; communication is not verified')
        return {'target':target, 'data_hex':data.hex(), 'bytes_read':length}

    @staticmethod
    def _write_result(target, count, length):
        if type(count) is not int or count != length:
            raise IOError('short write length; partial device side effects are possible')
        return {'target':target, 'bytes_written':count}

    def i2c_read_register(self, target, register, length):
        with self._operation():
            cfg = self._target(target, 'i2c')
            self._registers(cfg,register,length,'read_registers')
            data = self._driver(target,cfg).i2c_read_register(register,length)
            return self._read_result(target,data,length)

    def i2c_write_register(self, target, register, data_hex):
        data = _hex(data_hex)
        with self._operation():
            cfg = self._target(target,'i2c')
            self._registers(cfg,register,len(data),'write_registers')
            count = self._driver(target,cfg).i2c_write_register(register,data)
            return self._write_result(target,count,len(data))

    def spi_transfer(self, target, data_hex):
        data = _hex(data_hex)
        with self._operation():
            cfg = self._target(target,'spi')
            received = self._driver(target,cfg).spi_transfer(data)
            return self._read_result(target,received,len(data))

    def uart_read(self, target, length, timeout_ms=200):
        _integer(length,'length',1,MAX_BYTES)
        _integer(timeout_ms,'timeout_ms',1,1000)
        with self._operation():
            cfg = self._target(target,'uart')
            data = self._driver(target,cfg).uart_read(length,timeout_ms)
            return self._read_result(target,data,length)

    def uart_write(self, target, data_hex):
        data = _hex(data_hex)
        with self._operation():
            cfg = self._target(target,'uart')
            count = self._driver(target,cfg).uart_write(data)
            return self._write_result(target,count,len(data))

    def close(self):
        with self._lock:
            self._closed = True
            errors = []
            for name, driver in list(self._drivers.items()):
                try:
                    driver.close()
                except Exception as exc:
                    errors.append('%s: %s' % (name,exc))
                else:
                    del self._drivers[name]
            if errors:
                raise RuntimeError('external I/O cleanup failed; retry close: ' + '; '.join(errors))
            return {'closed':True}


class _I2CMessage(ctypes.Structure):
    _fields_ = [('addr',ctypes.c_uint16), ('flags',ctypes.c_uint16),
                ('len',ctypes.c_uint16), ('buf',ctypes.c_void_p)]


class _I2CRdwr(ctypes.Structure):
    _fields_ = [('msgs',ctypes.POINTER(_I2CMessage)), ('nmsgs',ctypes.c_uint32)]


class _SPITransfer(ctypes.Structure):
    _fields_ = [('tx_buf',ctypes.c_uint64), ('rx_buf',ctypes.c_uint64),
                ('len',ctypes.c_uint32), ('speed_hz',ctypes.c_uint32),
                ('delay_usecs',ctypes.c_uint16), ('bits_per_word',ctypes.c_uint8),
                ('cs_change',ctypes.c_uint8), ('tx_nbits',ctypes.c_uint8),
                ('rx_nbits',ctypes.c_uint8), ('word_delay_usecs',ctypes.c_uint8),
                ('pad',ctypes.c_uint8)]


def _ioctl(fd, request, argument):
    # libc preserves the integer result (I2C message count / SPI byte count).
    # fcntl.ioctl with a ctypes pointer would instead copy pointer bytes.
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.ioctl
    function.restype = ctypes.c_int
    function.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
    if isinstance(argument, bytearray):
        buffer = (ctypes.c_ubyte * len(argument)).from_buffer(argument)
        argument = ctypes.cast(buffer,ctypes.c_void_p)
    result = function(fd,request,argument)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code,os.strerror(code))
    return result


def _pin_function(pin):
    from maix import pinmap
    return pinmap.get_pin_function(pin)


def _device_users(path, own_fd=None):
    """Detect existing users; this is a check, not a kernel-exclusive bus claim.

    Root/service deployment must be able to inspect procfs. I2C/spidev have no
    exclusive-open API; a noncooperating privileged process can still race this
    check. flock coordinates cooperating applications only.
    """
    device = os.stat(path).st_rdev
    users = []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            handles = list((process / 'fd').iterdir())
        except FileNotFoundError:
            continue
        for handle in handles:
            if int(process.name) == os.getpid() and handle.name == str(own_fd):
                continue
            try:
                info = handle.stat()
            except FileNotFoundError:
                continue
            if stat.S_ISCHR(info.st_mode) and info.st_rdev == device:
                users.append((int(process.name),handle.name))
    return users


def _assert_device_available(path, own_fd=None):
    try:
        users = _device_users(path,own_fd)
    except OSError as exc:
        raise RuntimeError('cannot verify device occupancy for ' + path) from exc
    if users:
        raise RuntimeError('device is already in use: ' + path)


class _LinuxDriver:
    def __init__(self, cfg):
        self.cfg = cfg
        self.fd = None
        self._uart_original = None

    def _check_pins(self):
        for pin, expected in _pins(self.cfg).items():
            try:
                actual = _pin_function(pin)
            except Exception as exc:
                raise RuntimeError('cannot verify pinmux for ' + pin) from exc
            if actual != expected:
                raise RuntimeError('%s requires existing %s pinmux; got %r' % (pin,expected,actual))

    def _ready(self):
        self._check_pins()
        cfg = self.cfg
        if cfg['kind'] == 'i2c':
            owner = Path('/sys/bus/i2c/devices/%d-%04x/driver' % (cfg['bus'],cfg['address']))
            if owner.exists():
                raise RuntimeError('I2C address is owned by a kernel driver')
        if self.fd is not None:
            if cfg['kind'] == 'spi':
                self._check_spi_mode(self.fd)
            return self.fd
        if os.name != 'posix':
            raise RuntimeError('external I/O requires Linux')
        import fcntl
        kind = cfg['kind']
        if kind == 'gpio':
            path = '/dev/gpiochip%d' % (ord(cfg['pin'][0]) - ord('A'))
        elif kind == 'i2c':
            path = '/dev/i2c-%d' % cfg['bus']
        elif kind == 'spi':
            path = '/dev/spidev2.1'
        else:
            path = '/dev/ttyS2'
        if kind != 'gpio':
            _assert_device_available(path)
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISCHR(os.fstat(fd).st_mode):
                raise RuntimeError('expected Linux character device')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if kind != 'gpio':
                _assert_device_available(path,fd)
            if kind == 'gpio':
                request = bytearray(364)  # linux gpiohandle_request, ABI v1
                struct.pack_into('=I',request,0,int(cfg['pin'][1:]))
                struct.pack_into('=I',request,256,2 if cfg['direction']=='output' else 1)
                request[260] = cfg.get('initial',0)
                request[324:335] = b'xiaozhi-io\x00'
                struct.pack_into('=I',request,356,1)
                _ioctl(fd,0xC16CB403,request)
                handle = struct.unpack_from('=i',request,360)[0]
                if handle < 0:
                    raise IOError('GPIO line handle request failed')
                os.close(fd)
                fd = handle
                os.set_inheritable(fd,False)
            elif kind == 'spi':
                self._check_spi_mode(fd)
            elif kind == 'uart':
                import termios
                _ioctl(fd,termios.TIOCEXCL,None)
                original = termios.tcgetattr(fd)
                updated = list(original)
                updated[6] = list(original[6])
                updated[0] = updated[1] = updated[3] = 0
                updated[2] = termios.CLOCAL | termios.CREAD | termios.CS8
                updated[4] = updated[5] = getattr(termios,'B%d' % cfg['baud'])
                updated[6][termios.VMIN] = 0
                updated[6][termios.VTIME] = 0
                termios.tcsetattr(fd,termios.TCSANOW,updated)
                self._uart_original = original
            self.fd = fd
            return fd
        except Exception:
            os.close(fd)
            raise

    def _check_spi_mode(self,fd):
        mode = bytearray(4)
        _ioctl(fd,0x80046B05,mode)  # SPI_IOC_RD_MODE32
        if struct.unpack('=I',mode)[0] != self.cfg['mode']:
            raise RuntimeError('SPI mode differs from approved existing configuration')

    def gpio_read(self):
        values = bytearray(64)
        _ioctl(self._ready(),0xC040B408,values)
        return values[0]

    def gpio_write(self,value):
        values = bytearray(64)
        values[0] = value
        _ioctl(self._ready(),0xC040B409,values)

    def i2c_read_register(self,register,length):
        fd = self._ready()
        reg = ctypes.create_string_buffer(bytes([register]),1)
        data = ctypes.create_string_buffer(length)
        messages = (_I2CMessage * 2)(
            _I2CMessage(self.cfg['address'],0,1,ctypes.addressof(reg)),
            _I2CMessage(self.cfg['address'],1,length,ctypes.addressof(data)))
        packet = _I2CRdwr(messages,2)
        if _ioctl(fd,0x0707,ctypes.byref(packet)) != 2:
            raise IOError('short I2C read transaction')
        return data.raw

    def i2c_write_register(self,register,data):
        fd = self._ready()
        buffer = ctypes.create_string_buffer(bytes([register]) + data,len(data)+1)
        messages = (_I2CMessage * 1)(_I2CMessage(self.cfg['address'],0,len(data)+1,ctypes.addressof(buffer)))
        packet = _I2CRdwr(messages,1)
        if _ioctl(fd,0x0707,ctypes.byref(packet)) != 1:
            raise IOError('short I2C write transaction; partial side effects possible')
        return len(data)

    def spi_transfer(self,data):
        fd = self._ready()
        tx = ctypes.create_string_buffer(data,len(data))
        rx = ctypes.create_string_buffer(len(data))
        packet = _SPITransfer(ctypes.addressof(tx),ctypes.addressof(rx),len(data),self.cfg['speed_hz'],0,8)
        if _ioctl(fd,0x40206B00,ctypes.byref(packet)) != len(data):
            raise IOError('short SPI transfer; partial side effects possible')
        return rx.raw

    def uart_read(self,length,timeout_ms):
        fd = self._ready()
        deadline = time.monotonic() + timeout_ms / 1000.0
        result = bytearray()
        while len(result) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd],[],[],remaining)[0]:
                break
            try:
                data = os.read(fd,length-len(result))
            except BlockingIOError:
                continue
            if not data:
                break
            result.extend(data)
        return bytes(result)

    def uart_write(self,data):
        fd = self._ready()
        deadline = time.monotonic() + 1.0
        count = 0
        while count < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([],[fd],[],remaining)[1]:
                break
            try:
                written = os.write(fd,data[count:])
            except BlockingIOError:
                continue
            if written == 0:
                break
            count += written
        return count

    def close(self):
        if self.fd is None:
            return
        if self.cfg['kind'] == 'gpio' and self.cfg['direction'] == 'output':
            values = bytearray(64)
            values[0] = self.cfg['safe']
            # Retain the handle if restoring safe fails, allowing explicit retry.
            _ioctl(self.fd,0xC040B409,values)
        if self._uart_original is not None:
            import termios
            termios.tcsetattr(self.fd,termios.TCSANOW,self._uart_original)
            _ioctl(self.fd,termios.TIOCNXCL,None)
        os.close(self.fd)
        self.fd = None
