"""Deploy PaidThrough to Arc (mainnet or testnet). Dry-run by default: nothing is signed or sent.

Deploy-day steps (operator at the keyboard; Windows PowerShell 5.1 works for every line):

    gh run download <ci-run-id> -n contracts-out -D contracts/out
    python scripts/deploy_mainnet.py --network mainnet                 # dry run: chain, gas, address, cost
    python scripts/deploy_mainnet.py --network mainnet --send          # asks you to type DEPLOY, then sends

What the script does
  * Creation bytecode: <out>/PaidThrough.sol/PaidThrough.json from the CI `contracts-out` artifact (the same
    build CI tested). Constructor args are fixed: usdc = 0x3600...0000 (Arc USDC on both networks) and
    maxAmount = 10_000e6 (10,000 USDC in 6-decimal base units). Another cap needs an explicit --max-amount; a cap
    above 1,000,000 USDC is refused as a likely 18-decimal mistake.
  * Dry run (default): checks the RPC's chain id, makes a fresh random sender (in memory, holds nothing, has no
    code on chain) and runs eth_estimateGas for the creation with a 1-USDC balance override for that sender,
    prints the gas, the fee in USDC at the current base fee and at maxFee, and the address the contract would get.
    It never reads a key file and never signs.
  * --send: reads DEPLOYER_PRIVATE_KEY=0x... from the file at $PAIDTHROUGH_DEPLOYER_ENV (default
    ~/.paidthrough-deployer.env; refused if it sits inside this repo). The key is never printed. Refuses when the
    sender has code (an EIP-7702 delegation), has a pending transaction, or cannot pay gas x maxFee. EIP-1559 fees:
    maxFeePerGas = max(2 x baseFee, 20 gwei, baseFee + priority) because Arc drops transactions under its 20 gwei
    floor without an error. Waits for the receipt.
  * After the receipt: reads back usdc(), maxAmount(), billCount() == 0 and compares the runtime code with the
    artifact's deployedBytecode (immutable slots masked, then the immutable values checked separately). Only if
    every check passes does it write keeper/deployments.json ({"address", "fromBlock", "txHash"} for the network)
    and web/config.js (paidThrough, deployBlock), then print the explorer link.
  * --adopt 0xADDR --from-block N: the same read-back checks and config writes for a contract that is already
    deployed (recovery if the receipt wait or the file write was interrupted). No key.

Exit codes: 0 ok, 1 a post-deploy check failed (config not written), 2 refused before sending.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "keeper"))

from Crypto.Hash import keccak  # noqa: E402
from eth_abi import decode, encode  # noqa: E402
from eth_account import Account  # noqa: E402

from rpc import Rpc, RpcError, RpcUnavailable  # noqa: E402  (keeper/rpc.py: retries, 429 backoff, User-Agent)

USDC = "0x3600000000000000000000000000000000000000"
DEFAULT_MAX_AMOUNT = 10_000 * 10**6  # 10,000 USDC, 6-decimal base units
SANITY_MAX_AMOUNT = 1_000_000 * 10**6  # anything above is almost surely an 18-decimal value
GWEI = 10**9
MIN_FEE_FLOOR = 20 * GWEI
GAS_MULT_NUM, GAS_MULT_DEN = 12, 10  # gas limit = ceil(estimate x 1.2)

NETWORKS = {
    "mainnet": {"chainId": 5042, "rpc": "https://rpc.mainnet.arc.io", "explorer": "https://explorer.arc.io"},
    "testnet": {"chainId": 5042002, "rpc": "https://rpc.testnet.arc.network",
                "explorer": "https://explorer.testnet.arc.io"},
}

DEFAULT_OUT = ROOT / "contracts" / "out"
DEFAULT_DEPLOYMENTS = ROOT / "keeper" / "deployments.json"
DEFAULT_WEB_CONFIG = ROOT / "web" / "config.js"
_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


class Refused(Exception):
    """Refused before anything was sent (exit 2)."""


class CheckFailed(Exception):
    """A post-deploy read-back check failed (exit 1); config files are not written."""


# ---------------------------------------------------------------------------------------------- small helpers


def kec(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def sel(sig: str) -> str:
    return "0x" + kec(sig.encode())[:4].hex()


def usdc18(wei: int) -> str:
    """Native USDC (18 decimals) as a decimal string with 8 places, rounded up so a cost is never understated."""
    units = -(-int(wei) // 10**10)
    return f"{units // 10**8}.{units % 10**8:08d}"


def usdc6(units: int) -> str:
    return f"{int(units) // 10**6:,}.{int(units) % 10**6:06d}"


def gwei(wei: int) -> str:
    """A fee per gas, exact: whole gwei as 'N gwei', anything else as 'N wei'."""
    wei = int(wei)
    return f"{wei // GWEI} gwei" if wei % GWEI == 0 else f"{wei:,} wei"


def checksum(addr: str) -> str:
    a = addr.lower().replace("0x", "")
    if len(a) != 40 or not re.fullmatch(r"[0-9a-f]{40}", a):
        raise ValueError("not a 20-byte address: %r" % addr)
    h = kec(a.encode()).hex()
    return "0x" + "".join(c.upper() if c.isalpha() and int(h[i], 16) >= 8 else c for i, c in enumerate(a))


def _rlp_bytes(b: bytes) -> bytes:
    if len(b) == 1 and b[0] < 0x80:
        return b
    if len(b) <= 55:
        return bytes([0x80 + len(b)]) + b
    ln = len(b).to_bytes((len(b).bit_length() + 7) // 8, "big")
    return bytes([0xB7 + len(ln)]) + ln + b


def create_address(sender: str, nonce: int) -> str:
    """Address of a contract created by `sender` at `nonce`: keccak(rlp([sender, nonce]))[12:]."""
    n = int(nonce).to_bytes((int(nonce).bit_length() + 7) // 8, "big") if nonce else b""
    payload = _rlp_bytes(bytes.fromhex(sender[2:])) + _rlp_bytes(n)
    if len(payload) > 55:
        raise ValueError("payload too long")
    return checksum("0x" + kec(bytes([0xC0 + len(payload)]) + payload)[12:].hex())


def fee_params(base_fee: int, priority: int) -> tuple[int, int]:
    priority = max(0, int(priority))
    return max(2 * int(base_fee), MIN_FEE_FLOOR, int(base_fee) + priority), priority


def gas_limit(estimate: int) -> int:
    return -(-int(estimate) * GAS_MULT_NUM // GAS_MULT_DEN)


# ---------------------------------------------------------------------------------------------- artifact


class Artifact:
    def __init__(self, out_dir: Path):
        path = Path(out_dir) / "PaidThrough.sol" / "PaidThrough.json"
        if not path.is_file():
            raise Refused("artifact not found: %s (gh run download <run-id> -n contracts-out -D contracts/out)" % path)
        art = json.loads(path.read_text(encoding="utf-8"))
        self.path = path
        self.initcode = bytes.fromhex(art["bytecode"]["object"].removeprefix("0x"))
        dep = art["deployedBytecode"]
        self.runtime = bytes.fromhex(dep["object"].removeprefix("0x"))
        self.immutables = [(int(r["start"]), int(r["length"]))
                           for refs in (dep.get("immutableReferences") or {}).values() for r in refs]
        if not self.initcode or not self.runtime:
            raise Refused("artifact has empty bytecode: %s" % path)

    def creation_data(self, max_amount: int) -> str:
        return "0x" + (self.initcode + encode(["address", "uint96"], [USDC, max_amount])).hex()

    def mask(self, code: bytes) -> bytes:
        b = bytearray(code)
        for start, length in self.immutables:
            b[start:start + length] = bytes(length)
        return bytes(b)

    def immutable_words(self, code: bytes) -> list[bytes]:
        return [code[s:s + n] for s, n in self.immutables]


# ---------------------------------------------------------------------------------------------- key


class DeployerKey:
    """Holds the signing account. repr/str never contain the key."""

    def __init__(self, account):
        self._acct = account
        self.address = account.address

    def __repr__(self):
        return "<DeployerKey %s>" % self.address

    __str__ = __repr__

    def sign(self, tx: dict) -> str:
        signed = self._acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        return "0x" + bytes(raw).hex()


def key_path() -> Path:
    return Path(os.environ.get("PAIDTHROUGH_DEPLOYER_ENV") or (Path.home() / ".paidthrough-deployer.env")).expanduser()


def load_key(path: Path, repo_root: Path = ROOT) -> DeployerKey:
    """Read DEPLOYER_PRIVATE_KEY from `path`. Error messages never quote the file's contents."""
    path = Path(path)
    try:
        resolved = path.resolve()
        if resolved == repo_root.resolve() or repo_root.resolve() in resolved.parents:
            raise Refused("--send refused: the key file must live outside the repo (%s)" % path)
    except OSError:
        pass
    if not path.is_file():
        raise Refused("--send refused: key file not found at %s (set PAIDTHROUGH_DEPLOYER_ENV)" % path)
    value = None
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("DEPLOYER_PRIVATE_KEY") and "=" in s:
            value = s.split("=", 1)[1].strip().strip('"').strip("'")
    if not value:
        raise Refused("--send refused: no DEPLOYER_PRIVATE_KEY line in %s" % path)
    if not _KEY_RE.match(value):
        raise Refused("--send refused: DEPLOYER_PRIVATE_KEY in %s is not 32 bytes of hex" % path)
    try:
        acct = Account.from_key(value if value.startswith("0x") else "0x" + value)
    except Exception:  # noqa: BLE001 - never echo the exception (it may carry the key)
        raise Refused("--send refused: DEPLOYER_PRIVATE_KEY in %s is not a valid key" % path) from None
    finally:
        value = None
    return DeployerKey(acct)


# ---------------------------------------------------------------------------------------------- chain reads


def has_code(rpc: Rpc, addr: str) -> bool:
    return rpc.call("eth_getCode", [addr, "latest"]) not in ("0x", "0x0", "", None)


def read_word(rpc: Rpc, to: str, sig: str, block="latest") -> bytes:
    return bytes.fromhex(rpc.call("eth_call", [{"to": to, "data": sel(sig)}, block])[2:])


def post_checks(rpc: Rpc, art: Artifact, addr: str, max_amount: int, p) -> None:
    """Read-back checks. Raises CheckFailed on any mismatch."""
    code = bytes.fromhex(rpc.call("eth_getCode", [addr, "latest"])[2:])
    if not code:
        raise CheckFailed("no code at %s" % addr)
    if len(code) != len(art.runtime) or art.mask(code) != art.mask(art.runtime):
        raise CheckFailed("runtime code at %s differs from the artifact (immutables masked): %d vs %d bytes, "
                          "keccak %s vs %s" % (addr, len(code), len(art.runtime), kec(art.mask(code)).hex(),
                                               kec(art.mask(art.runtime)).hex()))
    p("  runtime code matches artifact (immutables masked): %d bytes, keccak %s"
      % (len(code), "0x" + kec(art.mask(code)).hex()))
    usdc = decode(["address"], read_word(rpc, addr, "usdc()"))[0]
    cap = decode(["uint96"], read_word(rpc, addr, "maxAmount()"))[0]
    count = decode(["uint256"], read_word(rpc, addr, "billCount()"))[0]
    if usdc.lower() != USDC.lower():
        raise CheckFailed("usdc() is %s, expected %s" % (usdc, USDC))
    if int(cap) != int(max_amount):
        raise CheckFailed("maxAmount() is %d, expected %d" % (cap, max_amount))
    if int(count) != 0:
        raise CheckFailed("billCount() is %d, expected 0" % count)
    expected_words = {encode(["address"], [USDC]), encode(["uint256"], [max_amount])}
    for w in art.immutable_words(code):
        if w not in expected_words:
            raise CheckFailed("an immutable slot holds an unexpected value 0x%s" % w.hex())
    p("  usdc() = %s, maxAmount() = %d (%s USDC), billCount() = 0" % (checksum(usdc), cap, usdc6(cap)))


# ---------------------------------------------------------------------------------------------- config writes


def write_deployment(path: Path, network: str, address: str, block: int, tx_hash: str | None) -> None:
    data = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    entry = {"address": checksum(address), "fromBlock": int(block)}
    if tx_hash:
        entry["txHash"] = tx_hash
    data[network] = entry
    for net in NETWORKS:
        data.setdefault(net, None)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def write_web_config(path: Path, network: str, address: str, block: int) -> None:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"\n(\s*)%s:\s*\{" % re.escape(network), text)
    if not m:
        raise CheckFailed("web config has no '%s: {' block" % network)
    start = m.end()
    end = text.find("\n%s}" % m.group(1), start)
    if end < 0:
        raise CheckFailed("web config '%s' block is not closed" % network)
    block_text = text[start:end]
    new_block, n1 = re.subn(r"(paidThrough:\s*)[^,\n]+(,)", r"\g<1>'%s'\g<2>" % checksum(address), block_text,
                            count=1)
    new_block, n2 = re.subn(r"(deployBlock:\s*)[^,\n]+(,)", r"\g<1>%d\g<2>" % int(block), new_block, count=1)
    if n1 != 1 or n2 != 1:
        raise CheckFailed("web config '%s' block lacks paidThrough/deployBlock fields" % network)
    path.write_text(text[:start] + new_block + text[end:], encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------------------------- main flow


def parse(argv):
    ap = argparse.ArgumentParser(description="Deploy PaidThrough to Arc (dry-run by default).")
    ap.add_argument("--network", choices=sorted(NETWORKS), required=True)
    ap.add_argument("--rpc", help="RPC URL (default: the network's public RPC)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="Foundry out/ dir with the CI contracts-out artifact")
    ap.add_argument("--max-amount", type=int, help="per-bill cap in USDC base units (6 decimals); default 10_000e6")
    ap.add_argument("--send", action="store_true", help="sign and send (reads the key file)")
    ap.add_argument("--yes", action="store_true", help="skip the typed DEPLOY confirmation (for scripted use)")
    ap.add_argument("--adopt", metavar="ADDRESS", help="check an existing deployment and write the config files")
    ap.add_argument("--from-block", type=int, help="deploy block, with --adopt")
    ap.add_argument("--receipt-timeout", type=float, default=180.0)
    ap.add_argument("--deployments", default=str(DEFAULT_DEPLOYMENTS))
    ap.add_argument("--web-config", default=str(DEFAULT_WEB_CONFIG))
    return ap.parse_args(argv)


def main(argv=None, rpc=None, out=None, key_loader=load_key, confirm=input, sleep=time.sleep) -> int:
    out = out or sys.stdout

    def p(*a):
        print(*a, file=out, flush=True)

    try:
        args = parse(argv)
        net = NETWORKS[args.network]
        max_amount = DEFAULT_MAX_AMOUNT if args.max_amount is None else int(args.max_amount)
        if not 0 < max_amount <= SANITY_MAX_AMOUNT:
            raise Refused("max amount %d base units is outside (0, %d]; amounts are 6-decimal USDC base units"
                          % (max_amount, SANITY_MAX_AMOUNT))
        rpc = rpc or Rpc(args.rpc or net["rpc"])
        art = Artifact(Path(args.out))
        chain_id = int(rpc.call("eth_chainId", []), 16)
        if chain_id != net["chainId"]:
            raise Refused("chain id mismatch: RPC says %d, %s is %d" % (chain_id, args.network, net["chainId"]))
        p("network      %s (chain id %d) via %s" % (args.network, chain_id, args.rpc or net["rpc"]))
        p("artifact     %s (initcode %d bytes, runtime %d bytes)" % (art.path, len(art.initcode), len(art.runtime)))
        p("constructor  usdc = %s, maxAmount = %d (%s USDC)" % (USDC, max_amount, usdc6(max_amount)))

        if args.adopt:
            if args.from_block is None:
                raise Refused("--adopt needs --from-block N (the deploy block)")
            addr = checksum(args.adopt)
            p("adopt        %s, deploy block %d" % (addr, args.from_block))
            post_checks(rpc, art, addr, max_amount, p)
            return _write_configs(args, addr, args.from_block, None, net, p)

        data = art.creation_data(max_amount)
        head = rpc.call("eth_getBlockByNumber", ["latest", False])
        base_fee = int(head.get("baseFeePerGas") or "0x0", 16)
        priority = int(rpc.call("eth_maxPriorityFeePerGas", []), 16)
        max_fee, priority = fee_params(base_fee, priority)

        if not args.send:
            return _dry_run(rpc, data, base_fee, max_fee, priority, int(head["number"], 16), net, p)

        key = key_loader(key_path())
        sender = key.address
        p("sender       %s" % sender)
        if has_code(rpc, sender):
            raise Refused("--send refused: %s has code (an EIP-7702 delegation or a contract); use a plain EOA"
                          % sender)
        nonce = int(rpc.call("eth_getTransactionCount", [sender, "latest"]), 16)
        pending = int(rpc.call("eth_getTransactionCount", [sender, "pending"]), 16)
        if pending != nonce:
            raise Refused("--send refused: %s has %d pending transaction(s)" % (sender, pending - nonce))
        try:
            est = int(rpc.call("eth_estimateGas", [{"from": sender, "data": data}]), 16)
        except RpcError as e:
            raise Refused("--send refused: eth_estimateGas failed: %s" % e.message) from None
        gas = gas_limit(est)
        balance = int(rpc.call("eth_getBalance", [sender, "latest"]), 16)
        worst = gas * max_fee
        predicted = create_address(sender, nonce)
        p("nonce        %d -> contract address %s" % (nonce, predicted))
        p("gas          estimate %d, limit %d" % (est, gas))
        p("fees         base %s, maxFee %s, priority %s" % (gwei(base_fee), gwei(max_fee), gwei(priority)))
        p("cost         ~%s USDC (at most %s USDC); balance %s USDC" % (
            usdc18(est * (base_fee + priority)), usdc18(worst), usdc18(balance)))
        if balance < worst:
            raise Refused("--send refused: balance %s USDC < gas x maxFee %s USDC" % (usdc18(balance), usdc18(worst)))
        if not args.yes:
            answer = confirm("Type DEPLOY to sign and send this transaction to %s: " % args.network)
            if answer.strip() != "DEPLOY":
                raise Refused("not confirmed; nothing sent")
        tx = {"type": 2, "chainId": chain_id, "nonce": nonce, "value": 0, "data": data, "gas": gas,
              "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority, "accessList": []}
        if "to" in tx or tx["value"] != 0 or tx["chainId"] != net["chainId"] or tx["maxFeePerGas"] < MIN_FEE_FLOOR:
            raise AssertionError("deploy tx shape check failed")
        raw = key.sign(tx)
        tx_hash = rpc.call("eth_sendRawTransaction", [raw])
        p("sent         %s" % tx_hash)
        p("             %s/tx/%s" % (net["explorer"], tx_hash))
        receipt = _wait_receipt(rpc, tx_hash, args.receipt_timeout, sleep)
        if receipt is None:
            raise CheckFailed("no receipt after %.0fs; check %s/tx/%s, then rerun with --adopt %s --from-block <n>"
                              % (args.receipt_timeout, net["explorer"], tx_hash, predicted))
        if int(receipt.get("status", "0x0"), 16) != 1:
            raise CheckFailed("deploy transaction failed (status 0): %s/tx/%s" % (net["explorer"], tx_hash))
        addr = checksum(receipt.get("contractAddress") or "0x" + "0" * 40)
        block = int(receipt["blockNumber"], 16)
        if addr != predicted:
            raise CheckFailed("receipt contractAddress %s != predicted %s" % (addr, predicted))
        p("deployed     %s in block %d, gas used %d" % (addr, block, int(receipt.get("gasUsed", "0x0"), 16)))
        post_checks(rpc, art, addr, max_amount, p)
        return _write_configs(args, addr, block, tx_hash, net, p)
    except Refused as e:
        p("REFUSED: %s" % e)
        return 2
    except CheckFailed as e:
        p("CHECK FAILED: %s" % e)
        p("config files were NOT written")
        return 1
    except (RpcError, RpcUnavailable) as e:
        p("RPC ERROR (state unknown, nothing assumed): %s" % e)
        return 2


def _dry_run(rpc, data, base_fee, max_fee, priority, head_number, net, p) -> int:
    for _ in range(10):
        dummy = Account.create().address  # random, in memory, holds nothing; never signs
        if not has_code(rpc, dummy):
            break
    else:
        raise Refused("could not find a code-free random sender")
    nonce = int(rpc.call("eth_getTransactionCount", [dummy, "latest"]), 16)
    override = {dummy: {"balance": hex(10**18)}}
    try:
        est = int(rpc.call("eth_estimateGas", [{"from": dummy, "data": data}, "latest", override]), 16)
    except RpcError as e:
        raise Refused("eth_estimateGas of the creation failed: %s" % e.message) from None
    gas = gas_limit(est)
    p("DRY RUN      nothing signed, nothing sent (add --send to deploy)")
    p("block        %d, base fee %s, priority %s, maxFee %s" % (
        head_number, gwei(base_fee), gwei(priority), gwei(max_fee)))
    p("sender       random %s (nonce %d, 1 USDC balance override for the estimate only)" % (dummy, nonce))
    p("address      %s would be created (it depends on the real sender's nonce)" % create_address(dummy, nonce))
    p("gas          estimate %d, limit %d (x1.2)" % (est, gas))
    p("cost         ~%s USDC at base fee + priority; at most %s USDC (limit x maxFee)" % (
        usdc18(est * (base_fee + priority)), usdc18(gas * max_fee)))
    p("needs        sender balance >= %s USDC; sender must be a plain EOA (no code)" % usdc18(gas * max_fee))
    return 0


def _wait_receipt(rpc, tx_hash, timeout, sleep):
    waited = 0.0
    while True:
        try:
            r = rpc.call("eth_getTransactionReceipt", [tx_hash])
        except RpcUnavailable:
            r = None
        if r:
            return r
        if waited >= timeout:
            return None
        sleep(1.0)
        waited += 1.0


def _write_configs(args, addr, block, tx_hash, net, p) -> int:
    write_deployment(Path(args.deployments), args.network, addr, block, tx_hash)
    write_web_config(Path(args.web_config), args.network, addr, block)
    p("wrote        %s (%s: address, fromBlock)" % (args.deployments, args.network))
    p("wrote        %s (%s: paidThrough, deployBlock)" % (args.web_config, args.network))
    p("explorer     %s/address/%s" % (net["explorer"], addr))
    return 0


if __name__ == "__main__":
    sys.exit(main())
