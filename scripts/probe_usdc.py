"""Read-only check of the Arc USDC token interface that PaidThrough relies on.

Prints chain id, base fee, the token's name/version/decimals, whether the
implementation carries the EIP-3009 / EIP-2612 / blocklist selectors, and
whether the EIP-712 domain separator we compute matches the chain's.

    python scripts/probe_usdc.py            # Arc mainnet
    python scripts/probe_usdc.py --testnet  # Arc testnet

Standard library only, except keccak (pycryptodome or eth_hash, whichever is present).
"""
import argparse
import json
import sys
import urllib.request

USDC = "0x3600000000000000000000000000000000000000"
NETWORKS = {
    "mainnet": ("https://rpc.mainnet.arc.io", 5042),
    "testnet": ("https://rpc.testnet.arc.network", 5042002),
}
# FiatToken proxies keep the implementation at keccak256("org.zeppelinos.proxy.implementation").
ZOS_IMPL_SLOT = "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"
SELECTORS = {
    "receiveWithAuthorization(v,r,s)": "ef55bec6",
    "transferWithAuthorization(v,r,s)": "e3ee160e",
    "authorizationState": "e94a0102",
    "cancelAuthorization(v,r,s)": "5a049a70",
    "permit(v,r,s)": "d505accf",
    "isBlacklisted": "fe575a87",
    "transferFrom": "23b872dd",
}


def keccak(data: bytes) -> bytes:
    try:
        from Crypto.Hash import keccak as _k

        h = _k.new(digest_bits=256)
        h.update(data)
        return h.digest()
    except ImportError:
        from eth_hash.auto import keccak as _k2

        return _k2(data)


def rpc(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "paidthrough-probe"})
    out = json.load(urllib.request.urlopen(req, timeout=20))
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]


def abi_string(hexdata: str) -> str:
    raw = bytes.fromhex(hexdata[2:])
    n = int.from_bytes(raw[32:64], "big")
    return raw[64 : 64 + n].decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--testnet", action="store_true")
    net = "testnet" if ap.parse_args().testnet else "mainnet"
    url, want_chain = NETWORKS[net]

    chain = int(rpc(url, "eth_chainId", []), 16)
    block = rpc(url, "eth_getBlockByNumber", ["latest", False])
    print(f"network      {net}  chain id {chain}  block {int(block['number'], 16)}")
    print(f"base fee     {int(block['baseFeePerGas'], 16) / 1e9:g} gwei")

    name = abi_string(rpc(url, "eth_call", [{"to": USDC, "data": "0x06fdde03"}, "latest"]))
    version = abi_string(rpc(url, "eth_call", [{"to": USDC, "data": "0x54fd4d50"}, "latest"]))
    decimals = int(rpc(url, "eth_call", [{"to": USDC, "data": "0x313ce567"}, "latest"]), 16)
    onchain_ds = rpc(url, "eth_call", [{"to": USDC, "data": "0x3644e515"}, "latest"])
    print(f"token        name={name!r} version={version!r} decimals={decimals}")

    impl = "0x" + rpc(url, "eth_getStorageAt", [USDC, ZOS_IMPL_SLOT, "latest"])[-40:]
    code = rpc(url, "eth_getCode", [impl, "latest"])
    print(f"impl         {impl} ({len(code) // 2 - 1} bytes)")
    ok = True
    for label, sel in SELECTORS.items():
        present = sel in code
        ok &= present
        print(f"  {'yes' if present else 'NO '}  {label}")

    th = keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
    ds = keccak(th + keccak(name.encode()) + keccak(version.encode()) + chain.to_bytes(32, "big") + bytes(12) + bytes.fromhex(USDC[2:]))
    match = "0x" + ds.hex() == onchain_ds
    print(f"domain sep   chain {onchain_ds}")
    print(f"             ours  0x{ds.hex()}  {'match' if match else 'MISMATCH'}")
    rwa = keccak(b"ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)")
    print(f"receive typehash 0x{rwa.hex()}")
    return 0 if (ok and match and chain == want_chain and decimals == 6) else 1


if __name__ == "__main__":
    sys.exit(main())
