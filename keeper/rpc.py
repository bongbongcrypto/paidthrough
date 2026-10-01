"""Small JSON-RPC client (stdlib urllib) with retry/backoff and chunked eth_getLogs.

Why stdlib urllib: the keeper is a short synchronous CLI that runs once per timer tick (every 5 minutes in
keeper/systemd/paidthrough-keeper.timer), so blocking calls with retry/backoff are enough, and this module
imports nothing outside the Python standard library.

Arc getLogs limits, probed read-only on 2026-10-01 against both public RPCs
(https://rpc.mainnet.arc.io, head ~23,634,929; https://rpc.testnet.arc.network, head ~64,872,333):
  * block range: 10,000 blocks inclusive (to - from + 1 = 10,000) works; 10,001 fails on both with
      {"code": -32012, "message": "requested range too large"}   (HTTP 200)
  * result count: a 10,000-block query on the busy USDC contract failed with
      {"code": -32602, "message": "request exceeded max allowed range: query exceeds max results 2000,
       retry with the range 64862334-64862849"}   (HTTP 200)
    while a 2,000-block query returned 7,569 logs without error, so the result cap is not a plain count;
    we treat the hint as advice only and keep halving.
  * blocks are ~0.51 s apart, so 10,000 blocks is ~85 minutes of chain.
  * eth_maxPriorityFeePerGas: testnet 0x12a05f200 (5 gwei), mainnet 0x23c (572 wei); base fee 20 gwei on both.
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request

DEFAULT_CHUNK = 10_000
MIN_CHUNK = 1

# Arc's two messages first, then generic fallbacks seen on other providers.
_RANGE_PATTERNS = (
    re.compile(r"requested range too large", re.I),
    re.compile(r"exceeds max results", re.I),
    re.compile(r"max allowed range", re.I),
    re.compile(r"block range", re.I),
    re.compile(r"range", re.I),
    re.compile(r"limit", re.I),
    re.compile(r"too many", re.I),
    re.compile(r"query returned more than", re.I),
    re.compile(r"response size", re.I),
)
_RANGE_CODES = (-32012, -32005)
_HINT = re.compile(r"retry with the range (\d+)\s*-\s*(\d+)", re.I)


class RpcError(RuntimeError):
    """The node answered with a JSON-RPC error object."""

    def __init__(self, method, error):
        self.method = method
        self.code = error.get("code") if isinstance(error, dict) else None
        self.message = str(error.get("message", error)) if isinstance(error, dict) else str(error)
        self.data = error.get("data") if isinstance(error, dict) else None
        super().__init__("%s: %s %s" % (method, self.code, self.message))


class RpcUnavailable(RuntimeError):
    """Transport failure after retries (timeout, 429, 5xx, bad JSON). The answer is unknown, never 'zero'."""


def is_range_error(err: RpcError) -> bool:
    if err.code in _RANGE_CODES:
        return True
    return any(p.search(err.message or "") for p in _RANGE_PATTERNS)


def range_hint(err: RpcError):
    """Span suggested by Arc's 'retry with the range A-B' message, or None."""
    m = _HINT.search(err.message or "")
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return b - a + 1 if b >= a else None


class Rpc:
    def __init__(self, url: str, timeout: float = 20.0, retries: int = 4, backoff: float = 1.0,
                 sleep=time.sleep, opener=None):
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self._open = opener or urllib.request.urlopen
        self._id = 0

    def call(self, method: str, params: list):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
        last = "no attempt"
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(min(self.backoff * (2 ** (attempt - 1)), 30.0))
            req = urllib.request.Request(self.url, data=body, headers={
                "Content-Type": "application/json", "User-Agent": "paidthrough-keeper/1"})
            try:
                with self._open(req, timeout=self.timeout) as r:
                    raw = r.read()
            except urllib.error.HTTPError as e:
                if e.code == 429 or 500 <= e.code < 600:
                    last = "HTTP %d" % e.code
                    continue
                raise RpcUnavailable("%s: HTTP %d" % (method, e.code)) from None
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                last = type(e).__name__
                continue
            try:
                out = json.loads(raw)
            except ValueError:
                last = "bad JSON"
                continue
            if not isinstance(out, dict):
                last = "bad JSON-RPC envelope"
                continue
            if out.get("error") is not None:
                err = RpcError(method, out["error"])
                # Rate limit expressed as a JSON-RPC error: retry like HTTP 429.
                if err.code == 429 or (err.code != 3 and re.search(r"rate limit|too many requests", err.message, re.I)):
                    last = "rate limited"
                    continue
                raise err
            if "result" not in out:
                last = "no result field"
                continue
            return out["result"]
        raise RpcUnavailable("%s failed after %d attempts (%s)" % (method, self.retries + 1, last))

    # --- helpers ---
    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def latest_block(self) -> dict:
        b = self.call("eth_getBlockByNumber", ["latest", False])
        if not b or "number" not in b or "timestamp" not in b:
            raise RpcUnavailable("eth_getBlockByNumber(latest) returned no block")
        return b

    def eth_call(self, to: str, data: str, frm: str = None, block="latest") -> str:
        tx = {"to": to, "data": data}
        if frm:
            tx["from"] = frm
        return self.call("eth_call", [tx, block])

    def get_logs_chunked(self, address: str, topics, from_block: int, to_block: int,
                         chunk: int = DEFAULT_CHUNK, on_chunk=None) -> list:
        """All logs in [from_block, to_block], inclusive, in chunks of at most `chunk` blocks.
        On a range/limit error the chunk halves (down to 1 block) and the same window is retried;
        after a run of successes it grows back toward `chunk`. Anything else propagates: a failed
        read is 'unknown', never 'no logs'."""
        if from_block > to_block:
            return []
        logs = []
        size = max(MIN_CHUNK, int(chunk))
        ceiling = size          # lowered for good by a pure block-range error; a result-count error is local
        start = from_block
        streak = 0
        while start <= to_block:
            end = min(to_block, start + size - 1)
            try:
                part = self.call("eth_getLogs", [{
                    "address": address, "topics": topics, "fromBlock": hex(start), "toBlock": hex(end)}])
            except RpcError as e:
                if not is_range_error(e):
                    raise
                window = end - start + 1
                if window <= MIN_CHUNK:
                    raise RpcError("eth_getLogs", {"code": e.code, "message": "range error even for one block: "
                                                   + e.message}) from None
                size = max(MIN_CHUNK, window // 2)
                hint = range_hint(e)
                if hint and hint < size:
                    size = max(MIN_CHUNK, hint)
                if not re.search(r"results", e.message or "", re.I):
                    ceiling = min(ceiling, size)
                streak = 0
                continue
            if part is None or not isinstance(part, list):
                raise RpcUnavailable("eth_getLogs %d-%d returned %r" % (start, end, type(part).__name__))
            logs.extend(part)
            if on_chunk:
                on_chunk(start, end, len(part))
            start = end + 1
            streak += 1
            if streak >= 4 and size < ceiling:
                size = min(ceiling, size * 2)
                streak = 0
        return logs
