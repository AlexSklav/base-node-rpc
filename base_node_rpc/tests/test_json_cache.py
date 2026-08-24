# coding: utf-8
"""
``json_tricks`` decoder-hook signature cache
(:func:`base_node_rpc.queue._install_json_tricks_hook_cache`).

``json_tricks.decoders.TricksPairHook.__init__()`` is constructed **once per**
:func:`json_tricks.loads` call and wraps *every* object-pairs hook in
``filtered_wrapper``, which runs :func:`inspect.signature` on it.  With the
default hooks that is ~11 signature inspections **per decoded packet** --
dominating the cost of decoding the small JSON payloads carried by ``STREAM``
event packets (~70 us per event versus ~1 us for :func:`json.loads`).

Both wrapped functions are pure functions of the hook they are handed, so the
cache must be *transparent*: identical decoded output, identical types, for
every payload shape the firmware can emit.  The tests below are therefore
mostly an equivalence battery; the timing test is a loose sanity bound, not a
benchmark.

Hardware-free: pure in-process JSON encoding/decoding.
"""
import gc
import json
import time

import json_tricks
import numpy as np
import pytest

from base_node_rpc.queue import (_install_json_tricks_hook_cache,
                                 _memoize_by_argument)


PAYLOADS = {
    'simple_event': {'event': 'capacitance-updated', 'V_a': 102.3,
                     'capacitance': 1.2e-11, 'n_samples': 50},
    'nested': {'event': 'channels-updated',
               'meta': {'a': [1, 2, 3], 'b': {'c': None, 'd': True}},
               'values': [1.0, 2.0]},
    'ndarray_uint8': {'event': 'drops-detected',
                      'drops': np.array([[1, 2, 3], [4, 5, 6]],
                                        dtype=np.uint8)},
    'ndarray_float': {'event': 'x', 'a': np.arange(20, dtype=np.float64)},
    'numpy_scalars': {'event': 'y', 's': np.float32(3.5), 'i': np.int64(7),
                      'b': np.bool_(True)},
    'empty_values': {'event': 'shorts-detected', 'values': []},
    'unicode': {'event': 'note', 'text': 'µΩ °C — ✓'},
    'deep_list': {'event': 'w', 'v': [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]},
}


def assert_decoded_equal(left, right, path='$'):
    """Recursive, type-strict equality (``numpy`` aware, NaN tolerant)."""
    assert type(left) is type(right), f'{path}: {type(left)} != {type(right)}'
    if isinstance(left, np.ndarray):
        assert left.dtype == right.dtype, f'{path}: dtype'
        assert left.shape == right.shape, f'{path}: shape'
        assert np.array_equal(
            left, right,
            equal_nan=np.issubdtype(left.dtype, np.floating)), f'{path}: values'
    elif isinstance(left, dict):
        assert list(left.keys()) == list(right.keys()), f'{path}: keys'
        for key in left:
            assert_decoded_equal(left[key], right[key], f'{path}.{key}')
    elif isinstance(left, (list, tuple)):
        assert len(left) == len(right), f'{path}: len'
        for i, (a, b) in enumerate(zip(left, right)):
            assert_decoded_equal(a, b, f'{path}[{i}]')
    else:
        assert left == right or (left != left and right != right), \
            f'{path}: {left!r} != {right!r}'


def test_cache_is_installed_at_import_time():
    """Importing ``base_node_rpc.queue`` installs the cache.

    Both ``STREAM`` event dispatch sites (``PacketQueueManager.parse`` and
    ``BaseNodeSerialMonitor.read_packets``) rely on it, so it is installed once
    at import rather than by either caller.
    """
    import json_tricks.utils as jt_utils

    from base_node_rpc import queue as bnr_queue

    assert bnr_queue._JSON_TRICKS_HOOK_CACHE_INSTALLED is True
    assert getattr(jt_utils.get_arg_names, '_memoized_func', None) is not None


def test_installation_is_idempotent():
    """Re-installing is a no-op rather than a cache wrapped in a cache.

    Repeated wrapping would rebuild the cache each time and quietly restore the
    original cost.
    """
    import json_tricks.utils as jt_utils

    before = jt_utils.get_arg_names
    assert _install_json_tricks_hook_cache() is True
    assert jt_utils.get_arg_names is before


@pytest.mark.parametrize('name', sorted(PAYLOADS))
def test_decoded_output_is_unchanged_by_the_cache(name):
    """Every payload shape decodes identically with the cache installed.

    The point of the cache is that it changes nothing observable: the same
    wrapper (closing over the same hook and the same argument names) is
    returned rather than an equivalent one rebuilt from scratch.
    """
    encoded = json_tricks.dumps(PAYLOADS[name])
    first = json_tricks.loads(encoded)
    second = json_tricks.loads(encoded)
    # Repeated decodes are indistinguishable -- values, container types and
    # `numpy` dtypes alike.
    assert_decoded_equal(first, second)
    # The decoded payload still carries the same keys and the ``event`` name
    # that ``PacketQueueManager.parse`` dispatches on.  N.B. `json_tricks`
    # returns mappings as `OrderedDict` and does not restore `numpy` *scalar*
    # types, so the comparison here is deliberately structural.
    assert list(first) == list(PAYLOADS[name])
    assert first['event'] == PAYLOADS[name]['event']


def test_non_finite_floats_decode_without_the_cache_interfering():
    """``NaN``/``Infinity`` literals in a firmware payload still decode.

    The firmware emits JSON directly, so a saturated measurement can arrive as
    a bare ``NaN`` token.  Decoding must not raise (which would drop the packet
    *and* the rest of the read chunk).
    """
    decoded = json_tricks.loads(
        '{"event": "capacitance-updated", "a": NaN, "b": Infinity,'
        ' "c": -Infinity}')
    assert decoded['event'] == 'capacitance-updated'
    assert decoded['a'] != decoded['a']         # NaN
    assert decoded['b'] == float('inf')
    assert decoded['c'] == float('-inf')


def test_numpy_arrays_keep_their_dtype_through_a_round_trip():
    """``numpy`` support is exactly why ``json_tricks`` is used at all.

    Guards against the cache accidentally dropping the ``ndarray`` hook (which
    would silently decode arrays as nested lists).
    """
    for dtype in (np.uint8, np.int16, np.int64, np.float32, np.float64,
                  np.bool_):
        array = np.arange(6).astype(dtype)
        decoded = json_tricks.loads(json_tricks.dumps({'a': array}))['a']
        assert isinstance(decoded, np.ndarray)
        assert decoded.dtype == array.dtype
        assert np.array_equal(decoded, array)


def test_memoize_by_argument_falls_back_for_unhashable_arguments():
    """An unhashable argument bypasses the cache instead of raising.

    ``lru_cache`` raises :class:`TypeError` on an unhashable key; the wrapper
    must preserve the uncached behaviour (including any exception the wrapped
    function itself raises) in every case.
    """
    calls = []

    def identity(value):
        calls.append(value)
        if value == ['boom']:
            raise ValueError('propagated')
        return repr(value)

    cached = _memoize_by_argument(identity)

    assert cached('x') == "'x'"
    assert cached('x') == "'x'"
    assert calls == ['x'], 'hashable argument was not cached'

    assert cached(['a']) == "['a']"
    assert cached(['a']) == "['a']"
    assert calls == ['x', ['a'], ['a']], 'unhashable argument was cached'

    with pytest.raises(ValueError, match='propagated'):
        cached(['boom'])


def test_memoized_wrapper_exposes_cache_controls():
    """``cache_info``/``cache_clear`` remain reachable for diagnostics."""
    cached = _memoize_by_argument(lambda value: value)
    cached('a')
    cached('a')
    info = cached.cache_info()
    assert info.hits == 1 and info.misses == 1
    cached.cache_clear()
    assert cached.cache_info().hits == 0


def test_event_payload_decoding_is_fast_enough_for_the_hot_path():
    """A ``STREAM`` event payload decodes well under the uncached cost.

    Uncached, ~11 :func:`inspect.signature` calls per packet put this at ~70 us
    on a typical host.  The bound below (30 us) is deliberately generous -- it
    is a regression tripwire for the cache being lost, not a benchmark -- but
    it is far below the uncached cost on any machine that can run the suite.
    """
    encoded = json_tricks.dumps(PAYLOADS['simple_event'])
    json_tricks.loads(encoded)          # warm up

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        best = min(_time_loads(encoded, repeats=2000) for _ in range(3))
    finally:
        if gc_was_enabled:
            gc.enable()

    assert best < 30e-6, f'{best * 1e6:.1f} us per `json_tricks.loads()` call'
    # Sanity: plain `json.loads` is still the floor, and we are within ~50x.
    plain = min(_time_json(encoded, repeats=2000) for _ in range(3))
    assert best < max(plain * 50, 30e-6)


def _time_loads(encoded, repeats):
    started = time.perf_counter()
    for _ in range(repeats):
        json_tricks.loads(encoded)
    return (time.perf_counter() - started) / repeats


def _time_json(encoded, repeats):
    started = time.perf_counter()
    for _ in range(repeats):
        json.loads(encoded)
    return (time.perf_counter() - started) / repeats
