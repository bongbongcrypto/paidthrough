// Dev harness only (never loaded by index.html): an in-memory Arc with the PaidThrough rules from
// SPEC.md, a USDC token with EIP-3009 receiveWithAuthorization, and the JSON-RPC surface the page uses.
// It is strict on purpose: wrong encodings, wrong signature fields or a fee under the 20 gwei floor fail
// the way the real chain would, so a green run in the harness says something about app.js.

import { SEL, TOPIC, ERR } from '../abi.js';

const ZERO = '0x0000000000000000000000000000000000000000';
const DAY = 86400;
const MIN_CLAIM = 3600;
const MAX_CLAIM = 365 * DAY;
const MAX_PAY = 365 * DAY;
const FLOOR = 20_000_000_000n; // 20 gwei base-fee floor
const ERR_SEL = Object.fromEntries(Object.entries(ERR).map(([sel, name]) => [name, sel]));
const GAS = { issue: 121_000n, cancel: 34_000n, pay: 96_000n, payWithAuthorization: 118_000n, claim: 58_000n, decline: 58_000n, refund: 61_000n, approve: 46_000n };

const pad = (h) => h.padStart(64, '0');
const enc = (n) => pad(BigInt(n).toString(16));
const encA = (a) => pad(a.slice(2).toLowerCase());
const low = (a) => (a || '').toLowerCase();
const hex = (n) => '0x' + BigInt(n).toString(16);

export class RpcFail extends Error {
  constructor(code, message, data) { super(message); this.code = code; this.data = data; }
}
const revert = (name) => { throw new RpcFail(3, 'execution reverted: ' + name, ERR_SEL[name]); };
function revertString(msg) {
  const bytes = new TextEncoder().encode(msg);
  const body = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
  throw new RpcFail(3, 'execution reverted: ' + msg, '0x08c379a0' + enc(32) + enc(bytes.length) + body.padEnd(Math.ceil(body.length / 64) * 64 || 64, '0'));
}

function words(data) {
  const s = data.slice(10);
  if (s.length % 64 !== 0) throw new RpcFail(3, 'execution reverted: bad calldata length', '0x');
  const out = [];
  for (let i = 0; i < s.length; i += 64) out.push(s.slice(i, i + 64));
  return out;
}
const U = (w, bits = 256) => {
  const v = BigInt('0x' + w);
  if (v >= (1n << BigInt(bits))) throw new RpcFail(3, 'execution reverted: abi decode (uint' + bits + ')', '0x');
  return v;
};
const A = (w) => {
  if (!/^0{24}/.test(w)) throw new RpcFail(3, 'execution reverted: abi decode (address)', '0x');
  return '0x' + w.slice(24);
};

export class Sim {
  constructor({ chainId, contract, usdc, maxAmount }) {
    Object.assign(this, { chainId, contract: low(contract), usdc: low(usdc), maxAmount });
    this.offset = 0;              // seconds of time travel
    this.t0 = Date.now();
    this.firstBlock = 2_000_000;
    this.deployBlock = this.firstBlock - 20_000; // "deployed" ~3 h before the harness opened
    this.bills = [];              // id - 1
    this.bal = new Map();
    this.allow = new Map();
    this.used = new Set();        // EIP-3009 nonces used, per payer
    this.issued = new Map();      // r -> signed typed-data fields
    this.blocked = new Set();     // token blocklist
    this.codeAt = new Set();      // addresses with code (smart accounts / EIP-7702 delegates)
    this.logs = [];
    this.txs = new Map();
    this.seq = 0;
    this.rpcDown = false;         // fetch itself fails (no connection)
    this.refuseCalls = false;     // node answers, but refuses eth_call with a JSON-RPC error (rate limit)
    this.events = [];
    this.onChange = () => {};
  }

  now() { return Math.floor(Date.now() / 1000 + this.offset); }
  block() { return this.firstBlock + Math.floor(((Date.now() - this.t0) / 1000 + this.offset) * 2); } // ~0.5 s blocks
  travel(sec) { this.offset += sec; this.note(`time +${sec >= DAY ? sec / DAY + ' d' : sec / 3600 + ' h'}`); }
  note(msg, bad) { this.events.unshift({ at: new Date(), msg, bad: !!bad }); this.events.length = Math.min(this.events.length, 40); this.onChange(); }
  balance(a) { return this.bal.get(low(a)) ?? 0n; }
  mint(a, units) { this.bal.set(low(a), this.balance(a) + BigInt(units)); }
  move(from, to, amt, label = 'transfer') {
    if (this.blocked.has(low(from)) || this.blocked.has(low(to))) revertString('Blacklistable: account is blacklisted');
    if (this.balance(from) < amt) revertString(`ERC20: ${label} amount exceeds balance`);
    this.bal.set(low(from), this.balance(from) - amt);
    this.bal.set(low(to), this.balance(to) + amt);
  }
  authNonce(id) {
    // Stand-in for keccak256(abi.encode(chainid, address(this), billId)): unique per chain, contract and bill.
    return '0x' + enc(this.chainId).slice(-16) + this.contract.slice(-32) + enc(id).slice(-16);
  }

  snapshot() {
    return {
      bills: this.bills.map((b) => ({ ...b })), bal: new Map(this.bal), allow: new Map(this.allow),
      used: new Set(this.used), logs: this.logs.length,
    };
  }
  restore(s) { this.bills = s.bills; this.bal = s.bal; this.allow = s.allow; this.used = s.used; this.logs.length = s.logs; }

  bill(id) {
    const n = Number(id);
    return n >= 1 && n <= this.bills.length ? this.bills[n - 1] : null;
  }

  emit(topics, data, blockNumber, txHash) {
    const log = { address: this.contract, topics, data: '0x' + data, blockNumber: hex(blockNumber), transactionHash: txHash, logIndex: hex(this.logs.length), removed: false };
    this.logs.push(log);
    return log;
  }

  // ---- state-changing calls. `ctx` = { from, block, hash }
  exec(to, data, ctx) {
    to = low(to);
    const sel = data.slice(0, 10).toLowerCase();
    const from = low(ctx.from);
    const now = this.now();
    const out = [];
    const log = (topics, d) => out.push(this.emit(topics, d, ctx.block, ctx.hash));

    if (to === this.usdc) {
      if (sel === SEL.approve) {
        const w = words(data);
        if (w.length !== 2) revertString('bad approve');
        this.allow.set(from + ':' + low(A(w[0])), U(w[1]));
        return { gas: GAS.approve, logs: out, what: `approve ${U(w[1])} to ${A(w[0]).slice(0, 8)}` };
      }
      throw new RpcFail(3, 'execution reverted (sim: USDC function not modelled)', '0x');
    }
    if (to !== this.contract) throw new RpcFail(3, 'execution reverted (sim: unknown contract ' + to + ')', '0x');

    const w = words(data);
    const id = () => U(w[0]);
    const need = (n) => { if (w.length !== n) throw new RpcFail(3, `execution reverted: ${n} words expected, got ${w.length}`, '0x'); };

    switch (sel) {
      case SEL.issue: {
        need(5);
        const amount = U(w[0], 96); const payBy = Number(U(w[1], 64)); const cw = Number(U(w[2], 32));
        const allowed = A(w[3]); const ref = '0x' + w[4];
        if (amount === 0n || amount > this.maxAmount) revert('BadAmount');
        if (!(now < payBy && payBy <= now + MAX_PAY)) revert('BadPayBy');
        if (cw < MIN_CLAIM || cw > MAX_CLAIM) revert('BadClaimWindow');
        this.bills.push({ payee: from, amount, payer: ZERO, payBy, claimWindow: cw, allowedPayer: low(allowed), claimBy: 0, status: 1, ref });
        const newId = this.bills.length;
        log([TOPIC.BillIssued, '0x' + enc(newId), '0x' + encA(from), '0x' + encA(allowed)], enc(amount) + enc(payBy) + enc(cw) + ref.slice(2));
        return { gas: GAS.issue, logs: out, what: `issue #${newId}` };
      }
      case SEL.cancel: {
        need(1);
        const b = this.bill(id()) || { payee: ZERO, status: 0 }; // unknown id = all-zero bill, as on chain
        if (b.payee !== from) revert('NotPayee');   // guard order follows contracts/src/PaidThrough.sol
        if (b.status !== 1) revert('WrongStatus');
        b.status = 6;
        log([TOPIC.BillCancelled, '0x' + enc(id())], '');
        return { gas: GAS.cancel, logs: out, what: `cancel #${id()}` };
      }
      case SEL.pay:
      case SEL.payWithAuthorization: {
        const sig = sel === SEL.payWithAuthorization;
        need(sig ? 7 : 1);
        const b = this.bill(id());
        if (!b || b.status !== 1) revert('WrongStatus');
        if (now >= b.payBy) revert('PayWindowClosed');
        const payer = sig ? low(A(w[1])) : from;
        if (b.allowedPayer !== ZERO && b.allowedPayer !== payer) revert('NotAllowedPayer');
        // State first (SPEC: written before the token call), then the token moves the money.
        b.payer = payer; b.claimBy = now + b.claimWindow; b.status = 2;
        if (sig) {
          const validAfter = U(w[2]); const validBefore = U(w[3]); const v = Number(U(w[4], 8));
          const r = '0x' + w[5]; const s = '0x' + w[6];
          const signed = this.issued.get(r);
          const nonce = this.authNonce(id());
          const fieldsOk = signed && signed.v === v && signed.s === s && signed.from === payer && signed.to === this.contract
            && signed.value === b.amount && signed.validAfter === validAfter && signed.validBefore === validBefore && signed.nonce === nonce;
          if (!fieldsOk) revertString('FiatTokenV2: invalid signature');
          // Arc USDC verifies signers with code through ERC-1271; this mock delegate doesn't implement it.
          if (this.codeAt.has(payer)) revertString('FiatTokenV2: invalid signature');
          if (!(BigInt(now) > validAfter)) revertString('FiatTokenV2: authorization is not yet valid');
          if (!(BigInt(now) < validBefore)) revertString('FiatTokenV2: authorization is expired');
          if (this.used.has(payer + ':' + nonce)) revertString('FiatTokenV2: authorization is used or canceled');
          this.used.add(payer + ':' + nonce);
          this.move(payer, this.contract, b.amount);
        } else {
          const key = payer + ':' + this.contract;
          const al = this.allow.get(key) ?? 0n;
          if (al < b.amount) revertString('ERC20: transfer amount exceeds allowance');
          this.allow.set(key, al - b.amount);
          this.move(payer, this.contract, b.amount);
        }
        log([TOPIC.BillPaid, '0x' + enc(id()), '0x' + encA(payer)], enc(b.claimBy));
        return { gas: sig ? GAS.payWithAuthorization : GAS.pay, logs: out, what: `${sig ? 'payWithAuthorization' : 'pay'} #${id()}` };
      }
      case SEL.claim:
      case SEL.decline: {
        need(1);
        const b = this.bill(id()) || { payee: ZERO, status: 0 };
        if (b.payee !== from) revert('NotPayee');
        if (b.status !== 2) revert('WrongStatus');
        if (now >= b.claimBy) revert('ClaimWindowClosed');
        const isClaim = sel === SEL.claim;
        b.status = isClaim ? 3 : 5;
        this.move(this.contract, isClaim ? b.payee : b.payer, b.amount);
        if (isClaim) log([TOPIC.BillClaimed, '0x' + enc(id()), '0x' + encA(b.payee)], enc(b.amount));
        else log([TOPIC.BillDeclined, '0x' + enc(id()), '0x' + encA(b.payer)], enc(b.amount));
        return { gas: GAS[isClaim ? 'claim' : 'decline'], logs: out, what: `${isClaim ? 'claim' : 'decline'} #${id()}` };
      }
      case SEL.refund: {
        need(1);
        const b = this.bill(id());
        if (!b || b.status !== 2) revert('WrongStatus');
        if (now < b.claimBy) revert('ClaimWindowOpen');
        b.status = 4;
        this.move(this.contract, b.payer, b.amount);
        log([TOPIC.BillRefunded, '0x' + enc(id()), '0x' + encA(b.payer)], enc(b.amount) + encA(from));
        return { gas: GAS.refund, logs: out, what: `refund #${id()} by ${from.slice(0, 8)}` };
      }
      default:
        throw new RpcFail(3, 'execution reverted (sim: unknown selector ' + sel + ')', '0x');
    }
  }

  // Atomic: on revert or dry run, state goes back.
  run(to, data, ctx, dry) {
    const snap = this.snapshot();
    try {
      const res = this.exec(to, data, ctx);
      if (dry) this.restore(snap);
      return res;
    } catch (e) {
      this.restore(snap);
      throw e;
    }
  }

  view(to, data) {
    to = low(to);
    const sel = data.slice(0, 10).toLowerCase();
    if (to === this.usdc) {
      const w = words(data);
      if (sel === SEL.balanceOf) return '0x' + enc(this.balance(A(w[0])));
      if (sel === SEL.allowance) return '0x' + enc(this.allow.get(low(A(w[0])) + ':' + low(A(w[1]))) ?? 0n);
      throw new RpcFail(3, 'execution reverted (sim: USDC view not modelled)', '0x');
    }
    if (to !== this.contract) return '0x';
    if (sel === SEL.billCount) return '0x' + enc(this.bills.length);
    const w = words(data);
    if (sel === SEL.authNonce) return this.authNonce(U(w[0]));
    if (sel === SEL.getBill) {
      const b = this.bill(U(w[0]));
      if (!b) return '0x' + enc(0).repeat(9);
      return '0x' + encA(b.payee) + enc(b.amount) + encA(b.payer) + enc(b.payBy) + enc(b.claimWindow)
        + encA(b.allowedPayer) + enc(b.claimBy) + enc(b.status) + b.ref.slice(2);
    }
    // eth_call of a write function = simulation
    this.run(to, data, { from: ZERO, block: this.block(), hash: '0x' }, true);
    return '0x';
  }

  // ---- transactions from the mock wallet
  send(tx) {
    const hash = '0x' + Array.from(crypto.getRandomValues(new Uint8Array(32)), (b) => b.toString(16).padStart(2, '0')).join('');
    const maxFee = BigInt(tx.maxFeePerGas ?? tx.gasPrice ?? 0);
    if (maxFee < FLOOR) {
      // Arc drops these without an error: the hash exists but never gets a receipt.
      this.note(`dropped ${hash.slice(0, 10)}: maxFeePerGas ${maxFee} under the 20 gwei floor`, true);
      return hash;
    }
    const block = this.block();
    let receipt;
    try {
      const res = this.run(tx.to, tx.data, { from: tx.from, block, hash }, false);
      const gasLimit = BigInt(tx.gas ?? 30_000_000);
      if (gasLimit < res.gas) throw new RpcFail(3, 'out of gas', '0x');
      const fee = res.gas * FLOOR; // 18-decimal native units
      this.bal.set(low(tx.from), this.balance(tx.from) - fee / 1_000_000_000_000n);
      receipt = { status: '0x1', logs: res.logs, gasUsed: hex(res.gas), what: res.what };
      this.note(`${res.what} ok (${hash.slice(0, 10)})`);
    } catch (e) {
      receipt = { status: '0x0', logs: [], gasUsed: hex(21000), what: e.message };
      this.note(`tx reverted: ${e.message}`, true);
    }
    this.txs.set(hash, {
      readyAt: Date.now() + 250 + Math.floor(Math.random() * 450),
      receipt: { transactionHash: hash, blockNumber: hex(block), from: low(tx.from), to: low(tx.to), status: receipt.status, gasUsed: receipt.gasUsed, effectiveGasPrice: hex(FLOOR), logs: receipt.logs, type: '0x2' },
    });
    this.onChange();
    return hash;
  }

  // ---- JSON-RPC (what https://rpc.mainnet.arc.io answers)
  rpc(method, params = []) {
    switch (method) {
      case 'eth_chainId': return hex(this.chainId);
      case 'eth_blockNumber': return hex(this.block());
      case 'eth_getBlockByNumber': return { number: hex(this.block()), timestamp: hex(this.now()), baseFeePerGas: hex(FLOOR) };
      case 'eth_gasPrice': return hex(FLOOR);
      case 'eth_maxPriorityFeePerGas': return '0x1f2';
      case 'eth_getBalance': return hex(this.balance(params[0]) * 1_000_000_000_000n);
      case 'eth_getCode': return this.codeAt.has(low(params[0])) ? '0xef0100' + '7702'.repeat(10) : '0x';
      case 'eth_call':
        // A refused read must never look like an empty bill: getBill itself cannot revert for any id.
        if (this.refuseCalls) throw new RpcFail(-32005, 'request rate limit exceeded');
        return this.view(params[0].to, params[0].data || '0x');
      case 'eth_estimateGas': {
        const res = this.run(params[0].to, params[0].data || '0x', { from: params[0].from || ZERO, block: this.block(), hash: '0x' }, true);
        return hex(res.gas);
      }
      case 'eth_getTransactionReceipt': {
        const t = this.txs.get(params[0]);
        return t && Date.now() >= t.readyAt ? t.receipt : null;
      }
      case 'eth_getLogs': {
        const f = params[0] || {};
        const latest = this.block();
        const from = f.fromBlock === 'latest' ? latest : Number(BigInt(f.fromBlock ?? 0));
        const to = f.toBlock === 'latest' || f.toBlock == null ? latest : Number(BigInt(f.toBlock));
        if (to - from + 1 > 10_000) throw new RpcFail(-32012, 'requested range too large');
        const topics = f.topics || [];
        return this.logs.filter((l) => {
          const bn = Number(BigInt(l.blockNumber));
          if (bn < from || bn > to) return false;
          if (f.address && low(f.address) !== l.address) return false;
          return topics.every((tp, i) => tp == null || (Array.isArray(tp) ? tp.map(low).includes(low(l.topics[i])) : low(tp) === low(l.topics[i])));
        });
      }
      default:
        throw new RpcFail(-32601, 'the method ' + method + ' does not exist/is not available');
    }
  }

  // ---- seeding: bills "issued earlier" so the desk and status views have every state to show
  seed(b) {
    const block = this.firstBlock - 15_000 + this.bills.length * 7;
    this.bills.push({ ...b, payee: low(b.payee), payer: low(b.payer || ZERO), allowedPayer: low(b.allowedPayer || ZERO) });
    const id = this.bills.length;
    this.emit([TOPIC.BillIssued, '0x' + enc(id), '0x' + encA(b.payee), '0x' + encA(b.allowedPayer || ZERO)], enc(b.amount) + enc(b.payBy) + enc(b.claimWindow) + b.ref.slice(2), block, '0xseed');
    if (b.status >= 2 && b.status <= 5) this.mint(this.contract, b.status === 2 ? b.amount : 0n);
    return id;
  }
}
