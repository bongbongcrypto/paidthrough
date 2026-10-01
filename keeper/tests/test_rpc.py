import io
import json
import unittest
import urllib.error

import kpath  # noqa: F401

from fakechain import CONTRACT, FakeChain, FakeRpc
from rpc import Rpc, RpcError, RpcUnavailable, is_range_error, range_hint

import abi


class FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    """Scripted urlopen: each item is an exception to raise or a dict to answer with."""

    def __init__(self, script):
        self.script = list(script)
        self.n = 0

    def __call__(self, req, timeout=None):
        self.n += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResp(json.dumps(item).encode())


def http_err(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


class RpcTest(unittest.TestCase):
    def test_retry_on_429_and_5xx_then_ok(self):
        sleeps = []
        op = Opener([http_err(429), http_err(503), TimeoutError(), {"jsonrpc": "2.0", "id": 1, "result": "0x10"}])
        r = Rpc("http://x", retries=4, sleep=sleeps.append, opener=op)
        self.assertEqual(r.call("eth_blockNumber", []), "0x10")
        self.assertEqual(op.n, 4)
        self.assertEqual(sleeps, [1.0, 2.0, 4.0])   # exponential backoff

    def test_gives_up_as_unknown_not_empty(self):
        op = Opener([http_err(429)] * 3)
        r = Rpc("http://x", retries=2, sleep=lambda s: None, opener=op)
        with self.assertRaises(RpcUnavailable):
            r.call("eth_getLogs", [{}])

    def test_jsonrpc_rate_limit_error_is_retried(self):
        op = Opener([{"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limit exceeded"}},
                     {"jsonrpc": "2.0", "id": 1, "result": []}])
        r = Rpc("http://x", retries=2, sleep=lambda s: None, opener=op)
        self.assertEqual(r.call("eth_getLogs", [{}]), [])

    def test_revert_error_not_retried(self):
        err = {"code": 3, "message": "execution reverted: ERC20: transfer amount exceeds balance", "data": "0x08c379a0"}
        op = Opener([{"jsonrpc": "2.0", "id": 1, "error": err}])
        r = Rpc("http://x", retries=3, sleep=lambda s: None, opener=op)
        with self.assertRaises(RpcError) as cm:
            r.call("eth_call", [{}])
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(op.n, 1)

    def test_arc_range_messages_recognised(self):
        # exact messages from the Arc RPCs, 2026-10-01
        a = RpcError("eth_getLogs", {"code": -32012, "message": "requested range too large"})
        b = RpcError("eth_getLogs", {"code": -32602, "message": "request exceeded max allowed range: query exceeds "
                                     "max results 2000, retry with the range 64862334-64862849"})
        c = RpcError("eth_getLogs", {"code": -32000, "message": "query returned more than 10000 results"})
        self.assertTrue(is_range_error(a) and is_range_error(b) and is_range_error(c))
        self.assertEqual(range_hint(b), 516)
        self.assertFalse(is_range_error(RpcError("eth_getLogs", {"code": -32000, "message": "header not found"})))

    def test_chunk_halving_on_range_error(self):
        c = FakeChain(head=100_000, max_range=2_500)   # node tighter than our 10,000 default
        c.issue(1, 20_000)
        c.pay(1, 99_999)
        r = FakeRpc(c)
        logs = r.get_logs_chunked(CONTRACT, [abi.ALL_TOPICS], 15_000, 100_000)
        self.assertEqual(len(logs), 2)
        sizes = [t - f + 1 for f, t in c.get_logs_calls]
        self.assertEqual(sizes[:3], [10_000, 5_000, 2_500])      # halves until accepted
        self.assertTrue(all(s <= 2_500 for s in sizes[3:]))      # never grows past what the node accepts
        # coverage: accepted windows tile [15000, 100000] with no gap or overlap
        ok = [(f, t) for f, t in c.get_logs_calls if t - f + 1 <= 2_500]
        self.assertEqual(ok[0][0], 15_000)
        self.assertEqual(ok[-1][1], 100_000)
        for (f1, t1), (f2, t2) in zip(ok, ok[1:]):
            self.assertEqual(f2, t1 + 1)

    def test_chunk_default_fits_arc_limit(self):
        c = FakeChain(head=50_000)            # max_range 10,000 like Arc
        r = FakeRpc(c)
        r.get_logs_chunked(CONTRACT, [abi.ALL_TOPICS], 0, 50_000)
        self.assertTrue(all(t - f + 1 <= 10_000 for f, t in c.get_logs_calls))
        self.assertEqual(len(c.get_logs_calls), 6)   # 50,001 blocks / 10,000, no rejected calls

    def test_non_range_error_propagates(self):
        c = FakeChain()
        c.fail_methods["eth_getLogs"] = {"code": -32000, "message": "header not found"}
        with self.assertRaises(RpcError):
            FakeRpc(c).get_logs_chunked(CONTRACT, [abi.ALL_TOPICS], 0, 10)


if __name__ == "__main__":
    unittest.main()
