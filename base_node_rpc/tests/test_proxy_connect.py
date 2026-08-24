# coding: utf-8
"""
:meth:`base_node_rpc.proxy.SerialProxyMixin._connect` connection matrix.

``_connect()`` is the only place that decides *which* port a proxy talks to,
and every one of its failure paths used to leak a
:class:`serial_device.threaded.KeepAliveReader` -- a background thread holding
the port open and reconnecting forever behind a proxy that had already given
up.  The behaviours pinned here are:

* an auto-scan **skips** a port that does not answer with an ``ID_RESPONSE``
  (rather than paying several multi-second RPC timeouts to find that out);
* an **explicitly requested** port instead falls back to identifying the device
  through its RPC ``properties``;
* :class:`DeviceVersionMismatch` propagates out of ``_connect()`` (so callers
  can offer a firmware update) unless it is listed in ``ignore``;
* every failure path tears the reader thread down and clears
  ``self.serial_thread``;
* ``_close_serial_thread()`` returns (``closed.wait()`` must not block
  forever), and ``terminate()`` is bounded even when the command lock is held.

Hardware-free: the port scan and the device-ID request are stubbed, and the one
port that is actually opened is pyserial's in-memory ``loop://`` URL.
"""
import threading
import time

import pandas as pd
import pytest
import serial
import serial_device as sd

import base_node_rpc.proxy as bnrp
from base_node_rpc.proxy import (DeviceNotFound, DeviceVersionMismatch,
                                 SerialProxyMixin)

#: pyserial's in-memory loopback URL -- opens without any hardware.
LOOP_PORT = 'loop://'
#: A port that enumerates but never answers an ``ID_RESPONSE``.  It must never
#: be *opened*, so the name does not have to be openable.
SILENT_PORT = 'SILENT0'


class _Signals:
    """Recording stand-in for the proxy's ``blinker`` signal namespace."""

    def __init__(self):
        self.sent = []

    def signal(self, name):
        outer = self

        class _Signal:
            @staticmethod
            def send(payload=None):
                outer.sent.append((name, payload))

        return _Signal()

    @property
    def names(self):
        return [name for name, _ in self.sent]


class _Stub:
    """Minimal stand-in providing exactly what ``_connect()`` touches."""

    device_name = None
    device_version = None
    port = None
    ignore = None
    baudrate = 115200
    _settling_time_s = 0

    #: Bound straight off the mixin -- this is the code under test.
    _connect = SerialProxyMixin._connect
    _close_serial_thread = SerialProxyMixin._close_serial_thread
    terminate = SerialProxyMixin.terminate

    def __init__(self, package_name='stub_device', ram_free_raises=None):
        self.serial_thread = None
        self.device_verified = threading.Event()
        self.serial_signals = _Signals()
        self._command_lock = threading.Lock()
        self._packet_queue_manager = type(
            'Q', (), {'parse': staticmethod(lambda data: None)})()
        self._package_name = package_name
        self._ram_free_raises = ram_free_raises
        self.connection_lost_calls = []
        self.reconnection_made_calls = []
        self.ram_free_calls = 0

    def connection_lost(self, protocol, exception):
        self.connection_lost_calls.append(exception)

    def reconnection_made(self, protocol):
        self.reconnection_made_calls.append(protocol)

    def ram_free(self):
        self.ram_free_calls += 1
        if self._ram_free_raises is not None:
            raise self._ram_free_raises
        return 1234

    @property
    def properties(self):
        return {'package_name': self._package_name}


@pytest.fixture
def patched_scan(monkeypatch):
    """Stub the port scan and the ``ID_RESPONSE`` request.

    Returns a callable ``configure(ports, device_ids)`` where ``device_ids``
    maps a port name to either a ``dict`` (the device answered) or an exception
    instance (raised, as a silent device does).
    """
    opened = []
    real_serial_for_url = serial.serial_for_url

    def recording_serial_for_url(url, *args, **kwargs):
        opened.append(url)
        return real_serial_for_url(url, *args, **kwargs)

    monkeypatch.setattr(serial, 'serial_for_url', recording_serial_for_url)

    state = {'device_ids': {}}

    def fake_serial_ports(**kwargs):
        return pd.DataFrame(index=pd.Index(state['ports'], name='port'))

    def fake_read_device_id(port=None, **kwargs):
        value = state['device_ids'].get(port)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(bnrp, 'serial_ports', fake_serial_ports)
    monkeypatch.setattr(bnrp, 'read_device_id', fake_read_device_id)
    # `KeepAliveReader` re-checks port availability against the *real* comport
    # list.  Stub it too, so that a device physically attached to the host
    # cannot influence (or be touched by) these tests.
    monkeypatch.setattr(sd.threaded, 'get_serial_ports',
                        lambda **kwargs: list(state['ports']))

    def configure(ports, device_ids=None):
        state['ports'] = list(ports)
        state['device_ids'] = dict(device_ids or {})
        return state

    configure([LOOP_PORT])
    configure.opened = opened
    return configure


def _shutdown(stub):
    """Close the reader thread, asserting that closing actually returns."""
    reader = stub.serial_thread
    done = threading.Event()
    threading.Thread(target=lambda: (stub._close_serial_thread(), done.set()),
                     daemon=True).start()
    assert done.wait(20), '`_close_serial_thread()` deadlocked'
    if reader is not None:
        assert reader.closed.is_set()
    assert stub.serial_thread is None


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
def test_connect_verifies_device_and_starts_the_reader(patched_scan):
    """A successful connect verifies the device and leaves a live reader.

    Also pins that ``_close_serial_thread()`` returns: ``__exit__`` waits on
    ``closed``, which is only ever set if the reader thread signals on *every*
    exit path.
    """
    patched_scan([LOOP_PORT], {LOOP_PORT: None})
    stub = _Stub()

    stub._connect(port=LOOP_PORT)

    reader = stub.serial_thread
    assert reader is not None
    assert reader.connected.is_set()
    assert not reader.error.is_set()
    assert stub.device_verified.is_set()
    assert stub.ram_free_calls == 1
    assert 'connected' in stub.serial_signals.names

    _shutdown(stub)
    assert stub.connection_lost_calls, 'parent.connection_lost() never called'
    assert 'disconnected' in stub.serial_signals.names


def test_connect_signals_the_parent_on_an_exceptional_disconnect(patched_scan):
    """A hot-unplug propagates to the parent and flips the reader events.

    The ``ReaderThread`` delivers the unplug as
    ``protocol.connection_lost(exception)``; the events used to be left in the
    *connected* state, so the proxy kept writing to a dead port.
    """
    patched_scan([LOOP_PORT], {LOOP_PORT: None})
    stub = _Stub()
    stub._connect(port=LOOP_PORT)
    protocol = stub.serial_thread.protocol

    protocol.connection_lost(serial.SerialException('device unplugged'))

    assert protocol.disconnected.is_set()
    assert not protocol.connected.is_set()
    assert isinstance(stub.connection_lost_calls[-1], serial.SerialException)
    assert 'disconnected' in stub.serial_signals.names
    _shutdown(stub)


# --------------------------------------------------------------------------
# Port selection
# --------------------------------------------------------------------------
def test_scan_skips_a_port_that_never_identifies_itself(patched_scan):
    """Auto-scan skips a silent port instead of opening it.

    A port that does not answer an ``ID_RESPONSE`` is almost certainly not a
    device of interest (a modem, a Bluetooth port).  Attempting the RPC
    handshake there costs several ``_timeout_s`` waits *per port*.
    """
    patched_scan([SILENT_PORT, LOOP_PORT],
                 {SILENT_PORT: RuntimeError('no ID_RESPONSE'),
                  LOOP_PORT: None})
    stub = _Stub()

    stub._connect(port=None)

    assert stub.device_verified.is_set()
    assert SILENT_PORT not in patched_scan.opened, 'silent port was opened'
    assert LOOP_PORT in patched_scan.opened
    _shutdown(stub)


def test_explicit_port_falls_back_to_properties_identification(patched_scan):
    """An explicitly requested silent port is identified over RPC instead.

    The caller named this port, so skipping it would be wrong -- the device may
    simply predate ``ID_RESPONSE`` support.
    """
    patched_scan([LOOP_PORT], {LOOP_PORT: RuntimeError('no ID_RESPONSE')})
    stub = _Stub(package_name='stub_device')
    stub.device_name = 'stub_device'

    stub._connect(port=LOOP_PORT)

    assert stub.device_verified.is_set()
    assert stub.ram_free_calls == 1
    _shutdown(stub)


def test_explicit_port_properties_name_mismatch_is_rejected(patched_scan):
    """The ``properties`` fallback still enforces the expected device name.

    Otherwise any RPC-speaking board on the named port would be accepted as
    *the* device.
    """
    patched_scan([LOOP_PORT], {LOOP_PORT: RuntimeError('no ID_RESPONSE')})
    stub = _Stub(package_name='some_other_board')
    stub.device_name = 'stub_device'

    with pytest.raises(IOError, match='Device not found'):
        stub._connect(port=LOOP_PORT)

    assert not stub.device_verified.is_set()
    assert stub.serial_thread is None, 'reader thread leaked on the reject path'


def test_id_response_name_mismatch_is_rejected(patched_scan):
    """A device that identifies itself as something else is not connected to."""
    patched_scan([LOOP_PORT], {LOOP_PORT: {'device_name': 'other_board',
                                           'device_version': '1.0.0'}})
    stub = _Stub()
    stub.device_name = 'stub_device'

    with pytest.raises(IOError, match='Device not found'):
        stub._connect(port=LOOP_PORT)
    assert stub.serial_thread is None


def test_unknown_port_raises_device_not_found(patched_scan):
    """A requested port that does not enumerate is reported, not opened."""
    patched_scan([LOOP_PORT], {LOOP_PORT: None})
    stub = _Stub()

    with pytest.raises(DeviceNotFound):
        stub._connect(port='NOT-ENUMERATED')
    assert stub.serial_thread is None


# --------------------------------------------------------------------------
# Version mismatch
# --------------------------------------------------------------------------
def test_device_version_mismatch_propagates(patched_scan):
    """:class:`DeviceVersionMismatch` escapes ``_connect()``.

    It is caught and re-raised explicitly so that it is not swallowed by the
    generic ``except Exception: continue`` that skips unusable ports -- callers
    need it to offer a firmware update.
    """
    patched_scan([LOOP_PORT], {LOOP_PORT: {'device_name': 'stub_device',
                                           'device_version': '0.9.0'}})
    stub = _Stub()
    stub.device_name = 'stub_device'
    stub.device_version = '1.0.0'

    with pytest.raises(DeviceVersionMismatch) as exc_info:
        stub._connect(port=LOOP_PORT)

    assert exc_info.value.device_version == '0.9.0'
    assert str(exc_info.value)          # `__str__` must never raise
    assert stub.serial_thread is None


@pytest.mark.parametrize('ignore', [True, [DeviceVersionMismatch]])
def test_device_version_mismatch_can_be_ignored(patched_scan, ignore):
    """``ignore`` (``True`` or an explicit list) downgrades the mismatch.

    ``ignore=True`` means "ignore all optional exceptions"; both spellings must
    connect rather than raise.
    """
    patched_scan([LOOP_PORT], {LOOP_PORT: {'device_name': 'stub_device',
                                           'device_version': '0.9.0'}})
    stub = _Stub()
    stub.device_name = 'stub_device'
    stub.device_version = '1.0.0'
    stub.ignore = ignore

    stub._connect(port=LOOP_PORT)

    assert stub.device_verified.is_set()
    _shutdown(stub)


def test_device_version_mismatch_str_survives_a_proxy_without_a_version():
    """``DeviceVersionMismatch.__str__`` never raises.

    Not every proxy exposes ``device_version`` (``dropbot.SerialProxy`` does
    not).  A raising ``__str__`` breaks ``str(exc)``, f-strings, *and*
    traceback formatting -- turning a recoverable mismatch into an
    uninterpretable crash.
    """
    class _NoVersion:
        pass

    exception = DeviceVersionMismatch(_NoVersion(), '0.9.0')
    assert isinstance(str(exception), str)
    assert '0.9.0' in str(exception) or 'does not match' in str(exception)


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------
def test_open_failure_cleans_up_the_reader_thread(patched_scan, monkeypatch):
    """A port that enumerates but cannot be opened leaves nothing behind.

    The reader is assigned *before* ``__enter__()`` precisely so that a failed
    ``__enter__`` -- which has already started the thread -- is still torn
    down.
    """
    patched_scan([LOOP_PORT], {LOOP_PORT: None})

    def refuse(url, *args, **kwargs):
        raise serial.SerialException('cannot open')

    monkeypatch.setattr(serial, 'serial_for_url', refuse)
    stub = _Stub()

    with pytest.raises(IOError, match='Device not found'):
        stub._connect(port=LOOP_PORT)

    assert stub.serial_thread is None, 'reader thread leaked'


def test_verification_failure_cleans_up_the_reader_thread(patched_scan):
    """An RPC failure during verification tears the reader down.

    ``ram_free()`` is the first RPC call over the fresh connection; a device
    that fails it must not be left with a background reconnect loop attached.
    """
    patched_scan([LOOP_PORT], {LOOP_PORT: None})
    stub = _Stub(ram_free_raises=IOError('device did not answer ram_free'))

    with pytest.raises(IOError, match='Device not found'):
        stub._connect(port=LOOP_PORT)

    assert stub.ram_free_calls == 1
    assert stub.serial_thread is None, 'reader thread leaked'


def test_connect_closes_a_reader_left_over_from_a_previous_call(patched_scan):
    """Re-connecting does not leak the previous reader thread.

    Two live ``KeepAliveReader``s on the same port both hold it open and both
    keep reconnecting.
    """
    patched_scan([LOOP_PORT], {LOOP_PORT: None})
    stub = _Stub()
    stub._connect(port=LOOP_PORT)
    first = stub.serial_thread

    stub._connect(port=LOOP_PORT)
    second = stub.serial_thread

    assert second is not first
    assert first.closed.wait(20), 'previous reader was never closed'
    _shutdown(stub)


def test_close_serial_thread_is_idempotent(patched_scan):
    """Closing twice (and closing when never connected) is a no-op."""
    stub = _Stub()
    stub._close_serial_thread()             # never connected
    assert stub.serial_thread is None

    patched_scan([LOOP_PORT], {LOOP_PORT: None})
    stub._connect(port=LOOP_PORT)
    _shutdown(stub)
    stub._close_serial_thread()
    assert stub.serial_thread is None


def test_terminate_is_bounded_when_the_command_lock_is_held(patched_scan):
    """``terminate()`` tears down even if another thread holds the command lock.

    ``_command_lock`` is not reentrant and ``_close_serial_thread()`` waits
    (without a timeout) for the reader to close, so blocking indefinitely here
    would deadlock permanently if -- for example -- a ``disconnected``
    subscriber issued an RPC request.  The bounded acquire attempt is the
    deliberate trade-off.
    """
    patched_scan([LOOP_PORT], {LOOP_PORT: None})
    stub = _Stub()
    stub._connect(port=LOOP_PORT)
    reader = stub.serial_thread

    stub._command_lock.acquire()
    try:
        started = time.monotonic()
        done = threading.Event()
        threading.Thread(target=lambda: (stub.terminate(), done.set()),
                         daemon=True).start()
        assert done.wait(30), '`terminate()` deadlocked on the command lock'
        elapsed = time.monotonic() - started
    finally:
        stub._command_lock.release()

    assert elapsed < 15, f'terminate() took {elapsed:.1f}s'
    assert stub.serial_thread is None
    assert reader.closed.is_set()


def test_or_event_composition_across_packages(patched_scan):
    """``or_event.OrEvent`` and ``serial_device``'s own ``OrEvent`` compose.

    ``_connect()`` builds an :class:`or_event.OrEvent` over the *same*
    ``threading.Event`` objects that ``serial_device`` has already wrapped.
    Both callback sets must keep firing; a naive wrapper that replaced (rather
    than chained) ``Event.set`` would silence one of them, or recurse forever.
    """
    import or_event

    patched_scan([LOOP_PORT], {LOOP_PORT: None})
    stub = _Stub()
    stub._connect(port=LOOP_PORT)
    reader = stub.serial_thread

    external = or_event.OrEvent(reader.closed, reader.connected)
    assert external.is_set(), 'already-set `connected` was not observed'

    internal = sd.threaded.OrEvent(reader.closed)
    reader.closed.set()
    assert internal.is_set()
    assert external.is_set()
    reader.closed.clear()

    _shutdown(stub)
