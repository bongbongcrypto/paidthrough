// PaidThrough web page. Static, no build step, no libraries.
// Reads: Arc public JSON-RPC over fetch. Writes: the visitor's own wallet (EIP-1193).

import { CONFIG, activeNetwork } from './config.js';
import { SEL, TOPIC, ERR, ERROR_STRING } from './abi.js';
import { LANGS, STRINGS } from './i18n.js';

const net = activeNetwork();
const PREVIEW = !net.paidThrough;
const ZERO = '0x0000000000000000000000000000000000000000';
const DAY = 86400;
const STATUS = { None: 0, Open: 1, Paid: 2, Claimed: 3, Refunded: 4, Declined: 5, Cancelled: 6 };
const WINDOWS = [3600, DAY, 3 * DAY, 7 * DAY, 14 * DAY, 30 * DAY];
const REF_MAX = 140;

/* ------------------------------------------------------------------ i18n */

let lang = pickLang();

function pickLang() {
  try {
    const saved = localStorage.getItem('pt.lang');
    if (LANGS.some((l) => l.code === saved)) return saved;
  } catch { /* storage blocked: fall through */ }
  const nav = (navigator.language || '').toLowerCase();
  if (nav.startsWith('ko')) return 'ko';
  if (nav.startsWith('fil') || nav.startsWith('tl')) return 'fil';
  return 'en';
}

function setLang(code, fromId) {
  if (!LANGS.some((l) => l.code === code) || code === lang) return;
  lang = code;
  try { localStorage.setItem('pt.lang', code); } catch { /* not remembered, still switches */ }
  route({ keepFocus: true, focusId: fromId }); // focus returns to the select the visitor used
}

const locale = () => LANGS.find((l) => l.code === lang).locale;

function t(key, vars) {
  let s = STRINGS[lang][key] ?? STRINGS.en[key] ?? key;
  if (vars) s = s.replace(/\{(\w+)\}/g, (_, k) => (vars[k] ?? ''));
  return s;
}

/* ------------------------------------------------------------------ DOM */

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

let liveEl = null;
function announce(msg) {
  if (!liveEl) return;
  liveEl.textContent = '';
  setTimeout(() => { liveEl.textContent = msg; }, 30);
}

async function copyText(text, btn) {
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch {
    const ta = h('textarea', { class: 'sr-only', 'aria-hidden': 'true' });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
  }
  if (btn) {
    const old = btn.dataset.label || btn.textContent;
    btn.dataset.label = old;
    btn.textContent = ok ? t('common.copied') : t('common.copyFailed');
    setTimeout(() => { btn.textContent = old; }, 1800);
  }
  announce(ok ? t('common.copied') : t('common.copyFailed'));
}

/* ------------------------------------------------------------------ hex, ABI, units */

const strip = (x) => (x.startsWith('0x') || x.startsWith('0X') ? x.slice(2) : x);
const pad64 = (hex) => hex.padStart(64, '0');
const encUint = (n) => pad64(BigInt(n).toString(16));
const encAddr = (a) => pad64(strip(a).toLowerCase());
const encB32 = (b) => strip(b).toLowerCase().padStart(64, '0');
const toHex = (n) => '0x' + BigInt(n).toString(16);
const isAddr = (a) => /^0x[0-9a-fA-F]{40}$/.test(a || '');
const sameAddr = (a, b) => !!a && !!b && a.toLowerCase() === b.toLowerCase();
const isZero = (a) => !a || /^0x0{40}$/i.test(a);
const short = (a) => (a ? a.slice(0, 6) + '…' + a.slice(-4) : '');

// keccak-256 (for EIP-55 address checksums only; selectors come pre-computed from abi.js).
// Checked against pycryptodome and the EIP-55 spec vectors (scratch test, 2026-10-01).
const KRC = [
  0x0000000000000001n, 0x0000000000008082n, 0x800000000000808an, 0x8000000080008000n,
  0x000000000000808bn, 0x0000000080000001n, 0x8000000080008081n, 0x8000000000008009n,
  0x000000000000008an, 0x0000000000000088n, 0x0000000080008009n, 0x000000008000000an,
  0x000000008000808bn, 0x800000000000008bn, 0x8000000000008089n, 0x8000000000008003n,
  0x8000000000008002n, 0x8000000000000080n, 0x000000000000800an, 0x800000008000000an,
  0x8000000080008081n, 0x8000000000008080n, 0x0000000080000001n, 0x8000000080008008n,
];
const KROT = [0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14];
const M64 = (1n << 64n) - 1n;
const rotl = (v, n) => (n ? ((v << BigInt(n)) | (v >> BigInt(64 - n))) & M64 : v);
function keccakF(s) {
  const C = new Array(5);
  const B = new Array(25);
  for (let round = 0; round < 24; round++) {
    for (let x = 0; x < 5; x++) C[x] = s[x] ^ s[x + 5] ^ s[x + 10] ^ s[x + 15] ^ s[x + 20];
    for (let x = 0; x < 5; x++) {
      const d = C[(x + 4) % 5] ^ rotl(C[(x + 1) % 5], 1);
      for (let y = 0; y < 25; y += 5) s[y + x] ^= d;
    }
    for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) B[y + 5 * ((2 * x + 3 * y) % 5)] = rotl(s[x + 5 * y], KROT[x + 5 * y]);
    for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) s[x + 5 * y] = B[x + 5 * y] ^ (~B[((x + 1) % 5) + 5 * y] & M64 & B[((x + 2) % 5) + 5 * y]);
    s[0] ^= KRC[round];
  }
}
function keccakHex(bytes) {
  const rate = 136;
  const s = new Array(25).fill(0n);
  const p = new Uint8Array(Math.floor(bytes.length / rate) * rate + rate);
  p.set(bytes);
  p[bytes.length] ^= 0x01;
  p[p.length - 1] ^= 0x80;
  for (let off = 0; off < p.length; off += rate) {
    for (let i = 0; i < rate / 8; i++) {
      let lane = 0n;
      for (let b = 7; b >= 0; b--) lane = (lane << 8n) | BigInt(p[off + i * 8 + b]);
      s[i] ^= lane;
    }
    keccakF(s);
  }
  let out = '';
  for (let i = 0; i < 4; i++) for (let b = 0; b < 8; b++) out += Number((s[i] >> BigInt(8 * b)) & 0xffn).toString(16).padStart(2, '0');
  return out;
}
// EIP-55 mixed-case checksum address.
function checksumAddr(addr) {
  const lower = strip(addr).toLowerCase();
  const hash = keccakHex(new TextEncoder().encode(lower));
  return '0x' + [...lower].map((c, i) => (c > '9' && parseInt(hash[i], 16) >= 8 ? c.toUpperCase() : c)).join('');
}
// A pasted address with both cases must carry a valid checksum (catches typos); one-case input is accepted.
const checksumOk = (a) => a === a.toLowerCase() || a.slice(2) === a.slice(2).toUpperCase() || checksumAddr(a) === a;

function words(hex) {
  const s = strip(hex || '');
  const out = [];
  for (let i = 0; i + 64 <= s.length; i += 64) out.push(s.slice(i, i + 64));
  return out;
}
const wUint = (w) => BigInt('0x' + w);
const wAddr = (w) => '0x' + w.slice(24);

// 6-decimal USDC units -> "1,234.50"
function fmtUnits(units, decimals = 6, minFrac = 2) {
  const neg = units < 0n;
  const u = neg ? -units : units;
  const base = 10n ** BigInt(decimals);
  let frac = (u % base).toString().padStart(decimals, '0').replace(/0+$/, '');
  if (frac.length < minFrac) frac = frac.padEnd(minFrac, '0');
  const whole = new Intl.NumberFormat(locale()).format(u / base);
  return (neg ? '-' : '') + whole + (frac ? '.' + frac : '');
}

// "1,234.5" -> 1234500000n (6 decimals), or null
function parseUnits(str) {
  const s = String(str || '').replace(/[\s,]/g, '');
  if (!/^\d+(\.\d{0,6})?$/.test(s) && !/^\.\d{1,6}$/.test(s)) return null;
  const [w, f = ''] = s.split('.');
  return BigInt(w || '0') * 1_000_000n + BigInt((f + '000000').slice(0, 6));
}

// Native gas is 18 decimals (wei-like); USDC token amounts are 6. Never mix: convert here only.
function feeText(gas, feePerGas) {
  const wei = BigInt(gas) * BigInt(feePerGas);
  const units = (wei + 999_999_999_999n) / 1_000_000_000_000n; // round up to 6 decimals
  if (units === 0n) return '0.000001';
  return fmtUnits(units, 6, 2);
}

/* ------------------------------------------------------------------ time */

let skew = 0; // chain time minus wall clock, seconds
let baseFee = CONFIG.minFeePerGas;
const chainNow = () => Math.floor(Date.now() / 1000 + skew);

// Date and time each stay on one line ("Oct 13, 2026," / "11:40 GMT+9"); a wrap may only fall between them.
function fmtDate(ts, opts = {}) {
  const parts = new Intl.DateTimeFormat(locale(), {
    day: 'numeric', month: 'short', year: opts.noYear ? undefined : 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false,
    timeZoneName: opts.noZone ? undefined : 'short',
  }).formatToParts(new Date(ts * 1000));
  const cut = parts.findIndex((p) => p.type === 'hour' || p.type === 'dayPeriod');
  const glue = (ps) => ps.map((p) => p.value).join('').trim().replace(/\s+/g, '\u00a0');
  return cut > 0 ? glue(parts.slice(0, cut)) + ' ' + glue(parts.slice(cut)) : glue(parts);
}

function fmtDuration(sec) {
  const s = Math.max(0, sec);
  const nf = (n, unit) => new Intl.NumberFormat(locale(), { style: 'unit', unit, unitDisplay: 'long' }).format(n);
  if (s >= 2 * DAY) return nf(Math.floor(s / DAY), 'day');
  if (s >= 2 * 3600) return nf(Math.floor(s / 3600), 'hour');
  if (s >= 120) return nf(Math.floor(s / 60), 'minute');
  return nf(Math.max(1, Math.floor(s)), 'second');
}

function windowLabel(sec) {
  return sec < DAY ? fmtDuration(sec) : new Intl.NumberFormat(locale(), { style: 'unit', unit: 'day', unitDisplay: 'long' }).format(sec / DAY);
}

/* ------------------------------------------------------------------ JSON-RPC (public, read-only) */

class AppError extends Error {
  constructor(code, message, data) { super(message || code); this.code = code; this.data = data; }
}

let rpcSeq = 0;
async function rpcFetch(body) {
  let res;
  try {
    res = await fetch(net.rpc, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  } catch {
    throw new AppError('NETWORK');
  }
  if (!res.ok) throw new AppError('NETWORK', 'HTTP ' + res.status);
  try { return await res.json(); } catch { throw new AppError('NETWORK', 'bad JSON'); }
}

async function rpc(method, params = []) {
  const j = await rpcFetch({ jsonrpc: '2.0', id: ++rpcSeq, method, params });
  if (j.error) throw new AppError(j.error.code, j.error.message, j.error.data);
  return j.result;
}

// Batch request; returns results in call order, each either a value or an AppError.
async function rpcBatch(calls) {
  if (!calls.length) return [];
  const first = rpcSeq + 1;
  const body = calls.map(([method, params]) => ({ jsonrpc: '2.0', id: ++rpcSeq, method, params }));
  const j = await rpcFetch(body);
  if (!Array.isArray(j)) throw new AppError(j?.error?.code ?? 'NETWORK', j?.error?.message ?? 'batch refused');
  const byId = new Map(j.map((x) => [x.id, x]));
  return calls.map((_, i) => {
    const x = byId.get(first + i);
    if (!x) return new AppError('NETWORK', 'missing batch reply');
    return x.error ? new AppError(x.error.code, x.error.message, x.error.data) : x.result;
  });
}

const ethCall = (to, data) => rpc('eth_call', [{ to, data }, 'latest']);

async function syncClock() {
  const b = await rpc('eth_getBlockByNumber', ['latest', false]);
  skew = Number(BigInt(b.timestamp)) - Date.now() / 1000;
  if (b.baseFeePerGas) baseFee = BigInt(b.baseFeePerGas);
  return BigInt(b.number);
}

function decodeBill(id, raw) {
  const w = words(raw);
  if (w.length < 9) throw new AppError('BAD_REPLY', 'getBill returned ' + w.length + ' words');
  return {
    id: BigInt(id),
    payee: wAddr(w[0]),
    amount: wUint(w[1]),
    payer: wAddr(w[2]),
    payBy: Number(wUint(w[3])),
    claimWindow: Number(wUint(w[4])),
    allowedPayer: wAddr(w[5]),
    claimBy: Number(wUint(w[6])),
    status: Number(wUint(w[7])),
    ref: '0x' + w[8],
  };
}

// getBill never reverts: an unknown id comes back as an all-zero bill (status None). So every error here is
// the RPC refusing or failing (rate limit, internal error, empty reply), and it is thrown, never read as
// "no such bill"; the views show it as a network error and retry.
async function readBill(id) {
  const [raw] = await Promise.all([ethCall(net.paidThrough, SEL.getBill + encUint(id)), syncClock()]);
  const bill = decodeBill(id, raw); // a null or '0x' reply throws BAD_REPLY
  return bill.status === STATUS.None ? null : bill;
}

async function usdcBalance(addr) {
  const raw = await ethCall(net.usdc, SEL.balanceOf + encAddr(addr));
  return wUint(words(raw)[0] || '0');
}

/* ------------------------------------------------------------------ bill phase (chain state + chain time) */

function phaseOf(bill, now) {
  switch (bill.status) {
    case STATUS.Open: return now < bill.payBy ? 'open' : 'expired';
    case STATUS.Paid: return now < bill.claimBy ? 'paid' : 'refundDue';
    case STATUS.Claimed: return 'collected';
    case STATUS.Refunded: return 'returned';
    case STATUS.Declined: return 'declined';
    case STATUS.Cancelled: return 'cancelled';
    default: return 'unknown';
  }
}
const MOVING = new Set(['open', 'paid', 'refundDue']);

/* ------------------------------------------------------------------ reference fingerprint (SPEC.md) */

// ref = sha256(utf8("paidthrough:v1:" + saltHex + ":" + text)); saltHex = 32 lowercase hex chars, no 0x.
async function fingerprint(saltHex, text) {
  const data = new TextEncoder().encode('paidthrough:v1:' + saltHex + ':' + text);
  const d = new Uint8Array(await crypto.subtle.digest('SHA-256', data));
  return '0x' + [...d].map((b) => b.toString(16).padStart(2, '0')).join('');
}
function newSalt() {
  return [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, '0')).join('');
}
async function checkRef(bill, params) {
  const text = params.get('r');
  const salt = (params.get('s') || '').toLowerCase();
  if (text == null) return { state: 'missing' };
  if (!/^[0-9a-f]{32}$/.test(salt)) return { state: 'unchecked', text };
  const fp = await fingerprint(salt, text);
  return { state: fp === bill.ref.toLowerCase() ? 'ok' : 'bad', text };
}

function shareLink(kind, id, ref) {
  const base = location.href.split('#')[0];
  const q = ref ? '?' + new URLSearchParams({ r: ref.text, s: ref.salt }).toString() : '';
  return `${base}#/${kind}/${id}${q}`;
}

/* ------------------------------------------------------------------ local notes (this browser only) */

// set() returns false when the browser refuses the write (private mode, storage blocked or full), so the
// page never says a reference is kept here when it is not.
const store = {
  get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
  set(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); return true; } catch { return false; } },
  del(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
};
const nsKey = (kind, extra) => `pt.${kind}.${net.chainId}.${(net.paidThrough || 'none').toLowerCase()}${extra ? '.' + extra : ''}`;
const loadRef = (id) => store.get(nsKey('ref', String(id)));
const saveRef = (id, ref) => store.set(nsKey('ref', String(id)), { text: ref.text, salt: ref.salt });

function addPending(ref) {
  const list = store.get(nsKey('pending')) || [];
  list.push(ref);
  return store.set(nsKey('pending'), list.slice(-20));
}
// Moves a pending reference to its bill once the id is known. Returns true only if the reference was saved.
function settlePending(fp, id) {
  const list = store.get(nsKey('pending')) || [];
  const hit = list.find((p) => p.fp === fp);
  if (!hit) return false;
  const saved = saveRef(id, hit);
  if (saved) store.set(nsKey('pending'), list.filter((p) => p !== hit));
  return saved;
}

/* ------------------------------------------------------------------ wallet (EIP-1193) */

let account = null;
const wallet = () => window.ethereum || null;

function codeOf(e) {
  return e?.code ?? e?.data?.originalError?.code ?? e?.error?.code;
}

async function connect() {
  const p = wallet();
  if (!p) throw new AppError('NO_WALLET');
  const accs = await p.request({ method: 'eth_requestAccounts' });
  account = accs && accs[0] ? accs[0] : null;
  if (!account) throw new AppError('NO_ACCOUNT');
  await ensureChain();
  return account;
}

async function ensureChain() {
  const p = wallet();
  const want = '0x' + net.chainId.toString(16);
  const cur = String(await p.request({ method: 'eth_chainId' })).toLowerCase();
  if (cur === want) return;
  try {
    await p.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: want }] });
  } catch (e) {
    if (codeOf(e) !== 4902) throw e;
    // Arc reports native gas as USDC with 18 decimals; the token view used for bills has 6.
    await p.request({
      method: 'wallet_addEthereumChain',
      params: [{
        chainId: want,
        chainName: net.label,
        nativeCurrency: { name: 'USDC', symbol: 'USDC', decimals: 18 },
        rpcUrls: [net.rpc],
        blockExplorerUrls: [net.explorer],
      }],
    });
    const now = String(await p.request({ method: 'eth_chainId' })).toLowerCase();
    if (now !== want) await p.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: want }] });
  }
  const after = String(await p.request({ method: 'eth_chainId' })).toLowerCase();
  if (after !== want) throw new AppError('WRONG_CHAIN');
}

async function restoreAccount() {
  const p = wallet();
  if (!p) return;
  try {
    const accs = await p.request({ method: 'eth_accounts' });
    account = accs && accs[0] ? accs[0] : null;
  } catch { account = null; }
  if (typeof p.on === 'function') {
    p.on('accountsChanged', (accs) => {
      const next = accs && accs[0] ? accs[0] : null;
      if (sameAddr(next, account)) return;
      account = next;
      route({ keepFocus: true });
    });
    p.on('chainChanged', () => { /* re-checked before every write */ });
  }
}

// Fee fields set explicitly: Arc drops transactions priced under its 20 gwei floor without an error.
async function feeFields() {
  await syncClock();
  let prio;
  try { prio = BigInt(await rpc('eth_maxPriorityFeePerGas')); } catch { prio = 1n; }
  let maxFee = baseFee * 2n;
  if (maxFee < CONFIG.minFeePerGas) maxFee = CONFIG.minFeePerGas;
  if (maxFee < baseFee + prio) maxFee = baseFee + prio;
  return { maxFeePerGas: maxFee, maxPriorityFeePerGas: prio };
}

async function estimate(to, data) {
  const gas = BigInt(await rpc('eth_estimateGas', [{ from: account, to, data }]));
  return gas + gas / 4n; // 25% headroom
}

async function sendTx(to, data, onStep, preEstimated) {
  const gas = preEstimated ?? await estimate(to, data);
  const fees = await feeFields();
  onStep?.('confirm', { fee: feeText(gas, baseFee) });
  const hash = await wallet().request({
    method: 'eth_sendTransaction',
    params: [{ from: account, to, data, gas: toHex(gas), maxFeePerGas: toHex(fees.maxFeePerGas), maxPriorityFeePerGas: toHex(fees.maxPriorityFeePerGas) }],
  });
  onStep?.('wait', { hash });
  const t0 = performance.now();
  const receipt = await waitReceipt(hash);
  const ms = performance.now() - t0;
  if (receipt.status !== '0x1') throw new AppError('REVERTED', 'reverted', { hash });
  return { hash, receipt, ms };
}

async function waitReceipt(hash, timeoutMs = 90_000) {
  const end = performance.now() + timeoutMs;
  while (performance.now() < end) {
    try {
      const r = await rpc('eth_getTransactionReceipt', [hash]);
      if (r) return r;
    } catch (e) { if (!(e instanceof AppError) || e.code !== 'NETWORK') throw e; }
    await new Promise((ok) => setTimeout(ok, 200));
  }
  throw new AppError('TIMEOUT', 'no receipt', { hash });
}

function findRevertData(e, depth = 0) {
  if (e == null || depth > 5) return null;
  if (typeof e === 'string') return /^0x[0-9a-fA-F]{8}/.test(e) ? e : null;
  if (typeof e !== 'object') return null;
  for (const k of ['data', 'error', 'originalError', 'cause', 'info']) {
    const r = findRevertData(e[k], depth + 1);
    if (r) return r;
  }
  return null;
}

function decodeErrorString(data) {
  try {
    const w = words('0x' + strip(data).slice(8));
    const len = Number(wUint(w[1]));
    const bytesHex = strip(data).slice(8 + 128, 8 + 128 + len * 2);
    return new TextDecoder().decode(new Uint8Array(bytesHex.match(/../g).map((x) => parseInt(x, 16))));
  } catch { return ''; }
}

// USDC (FiatToken) revert strings in plain words. ctx = { action: 'pay'|'claim'|'decline'|'refund'|'cancel', bill }.
function explainTokenString(s, ctx = {}) {
  if (/blacklist|blocked/i.test(s)) {
    const date = ctx.bill?.claimBy ? fmtDate(ctx.bill.claimBy) : '';
    if (ctx.action === 'claim') return t('err.blockedClaim', { date });
    if (ctx.action === 'pay') return t('err.blockedPay');
    if (ctx.action === 'refund' || ctx.action === 'decline') return t('err.blockedRefund');
    return t('err.blocked');
  }
  if (/invalid signature/i.test(s)) return t('err.badSig');
  if (/expired/i.test(s)) return t('err.sigExpired');
  if (/exceeds balance/i.test(s)) return t('err.noFunds');
  if (/exceeds allowance/i.test(s)) return t('err.noAllowance');
  return t('err.token', { msg: s });
}

// Error text, plus a link to the transaction on the explorer when one was sent (timeouts, failed receipts).
function showError(el, e, ctx) {
  el.classList.add('msg--bad');
  el.replaceChildren(explain(e, ctx));
  const hash = e?.data?.hash;
  if (hash) el.append(' ', h('a', { href: `${net.explorer}/tx/${hash}`, target: '_blank', rel: 'noopener' }, t('pay.viewTx')));
}

function explain(e, ctx) {
  const code = codeOf(e);
  if (code === 4001 || code === 'ACTION_REJECTED') return t('err.rejected');
  if (code === 'NETWORK') return t('err.network');
  if (code === 'NO_WALLET') return t('err.noWallet');
  if (code === 'NO_ACCOUNT') return t('err.noAccount');
  if (code === 'WRONG_CHAIN') return t('err.wrongChain', { network: net.label });
  if (code === 'TIMEOUT') return t('err.timeout');
  if (code === 'NO_SIGN_TYPED') return t('err.noSignTyped');
  if (code === 'REVERTED') return t('err.reverted');
  if (code === 4100) return t('err.noAccount');
  const data = findRevertData(e);
  if (data) {
    const sel = data.slice(0, 10).toLowerCase();
    if (ERR[sel]) return t('err.' + ERR[sel]);
    if (sel === ERROR_STRING) return explainTokenString(decodeErrorString(data), ctx);
  }
  const msg = String(e?.message || e || '').slice(0, 160);
  return t('err.generic', { msg });
}

/* ------------------------------------------------------------------ the bill on paper (signature element) */

const STAMP = {
  open: { word: 'stamp.open', look: 'outline muted' },
  expired: { word: 'stamp.expired', sub: 'stamp.expiredSub', look: 'dashed muted' },
  paid: { word: 'stamp.paid', sub: 'stamp.paidSub', look: 'outline blue' },
  refundDue: { word: 'stamp.refundDue', sub: 'stamp.refundDueSub', look: 'dashed blue' },
  collected: { word: 'stamp.collected', look: 'filled' },
  returned: { word: 'stamp.returned', sub: 'stamp.returnedSub', look: 'dashed ink' },
  declined: { word: 'stamp.returned', sub: 'stamp.declinedSub', look: 'dashed ink' },
  cancelled: { word: 'stamp.cancelled', look: 'dashed muted' },
};

function stampEl(phase, land) {
  const s = STAMP[phase] || STAMP.open;
  return h('p', { class: 'stamp ' + s.look.split(' ').map((x) => 'stamp--' + x).join(' ') + (land ? ' stamp--land' : '') },
    h('span', { class: 'stamp-word' }, t(s.word)),
    s.sub ? h('span', { class: 'stamp-sub' }, t(s.sub)) : null);
}

// The biller's full address, EIP-55 checksummed, in 4-character groups. A short "0x5b7c…d5a4" is easy to
// imitate with a vanity address, so the full form is shown wherever someone decides whether to pay.
function fullAddrEl(addr, { copyable } = {}) {
  const cs = checksumAddr(addr);
  const groups = cs.slice(2).match(/.{4}/g);
  const el = h('span', { class: 'addr', translate: 'no' },
    h('span', { class: 'addr-g' }, '0x' + groups[0]),
    groups.slice(1).map((g) => h('span', { class: 'addr-g' }, g)));
  if (!copyable) return el;
  return h('span', { class: 'addr-wrap' }, el,
    h('button', { type: 'button', class: 'btn-text btn-text--sm', onclick: (e) => copyText(cs, e.currentTarget) }, t('common.copyAddr')));
}

function paperEl(bill, { phase, refCheck, example, land, you } = {}) {
  const paidAt = bill.claimBy ? bill.claimBy - bill.claimWindow : 0;
  const rows = [
    ['bill.biller', fullAddrEl(bill.payee, { copyable: !example })],
    ['bill.payBy', fmtDate(bill.payBy)],
  ];
  if (!isZero(bill.allowedPayer)) rows.push(['bill.onlyPayer', h('span', { class: 'mono' }, short(bill.allowedPayer))]);
  if (paidAt) {
    rows.push(['bill.paid', h('span', null, fmtDate(paidAt), h('span', { class: 'row-sub' }, t(you ? 'bill.paidFromYou' : 'bill.paidFrom', { addr: short(bill.payer) })))]);
    rows.push(['bill.collectBy', fmtDate(bill.claimBy)]);
  } else {
    rows.push(['bill.window', t('bill.windowValue', { time: windowLabel(bill.claimWindow) })]);
  }

  let refBlock;
  const rc = refCheck || { state: 'missing' };
  if (rc.text != null) {
    const note = {
      ok: h('p', { class: 'ref-check ref-check--ok' }, h('span', { class: 'ref-mark', 'aria-hidden': 'true' }), t('bill.refOk')),
      bad: h('p', { class: 'ref-check ref-check--bad' }, t('bill.refBad')),
      unchecked: h('p', { class: 'ref-check' }, t('bill.refUnchecked')),
    }[rc.state];
    refBlock = [h('p', { class: 'ref-text' + (rc.state === 'bad' ? ' ref-text--bad' : '') }, rc.text), note];
  } else {
    refBlock = [h('p', { class: 'ref-text ref-text--none' }, t('bill.refNone'))];
  }

  return h('article', { class: 'paper', 'data-phase': phase, 'aria-label': t('bill.no', { id: String(bill.id) }) },
    example ? h('p', { class: 'paper-tag' }, t('example.tag')) : null,
    h('div', { class: 'paper-top' },
      h('p', { class: 'eyebrow' }, t('bill.no', { id: String(bill.id) })),
      h('p', { class: 'eyebrow eyebrow--quiet' }, example ? t('example.net') : net.label)),
    h('div', { class: 'paper-main' },
      h('p', { class: 'paper-amount' }, h('span', { class: 'amt' }, fmtUnits(bill.amount)), h('span', { class: 'cur' }, 'USDC')),
      stampEl(phase, land)),
    h('div', { class: 'paper-ref' },
      h('p', { class: 'label' }, t('bill.ref')),
      refBlock,
      h('p', { class: 'fp' }, h('span', { class: 'label label--inline' }, t('bill.fingerprint')), h('span', { class: 'mono' }, bill.ref.slice(0, 10) + '…' + bill.ref.slice(-6)))),
    h('div', { class: 'perf', 'aria-hidden': 'true' }),
    h('dl', { class: 'paper-rows' }, rows.map(([k, v]) => h('div', { class: 'paper-row' }, h('dt', null, t(k)), h('dd', null, v)))));
}

// Thin line under the paper: how much of the biller's time to collect is gone.
function deadlineEl(bill, phase, now) {
  if (phase === 'paid' || phase === 'refundDue') {
    const paidAt = bill.claimBy - bill.claimWindow;
    const frac = Math.min(1, Math.max(0, (now - paidAt) / bill.claimWindow));
    const pct = Math.round(frac * 100);
    const left = bill.claimBy - now;
    const fill = h('span', { class: 'deadline-fill' });
    fill.style.width = pct + '%'; // CSSOM, not a style attribute: the page CSP has no 'unsafe-inline'
    return h('div', { class: 'deadline deadline--' + phase },
      h('div', { class: 'deadline-track', role: 'img', 'aria-label': t('dl.aria', { pct: String(pct) }) },
        fill,
        h('span', { class: 'deadline-end', 'aria-hidden': 'true' })),
      h('p', { class: 'deadline-ends' },
        h('span', null, t('dl.paid', { date: fmtDate(paidAt, { noZone: true, noYear: true }) })),
        h('span', { class: 'deadline-left' }, left > 0 ? t('dl.left', { time: fmtDuration(left) }) : t('dl.over'))));
  }
  if (phase === 'open') {
    return h('div', { class: 'deadline deadline--open' },
      h('p', { class: 'deadline-ends' },
        h('span', null, t('dl.payUntil', { date: fmtDate(bill.payBy, { noZone: true, noYear: true }) })),
        h('span', { class: 'deadline-left' }, t('dl.payLeft', { time: fmtDuration(bill.payBy - now) }))));
  }
  return null;
}

// you: the connected wallet is this bill's payer (from the chain). payView: the page is the payment page.
function statusCopy(bill, phase, { you, payView } = {}) {
  const amount = fmtUnits(bill.amount);
  const map = {
    open: ['st.open.title', payView ? 'st.open.bodyYou' : 'st.open.body', { date: fmtDate(bill.payBy) }],
    expired: ['st.expired.title', 'st.expired.body', { date: fmtDate(bill.payBy) }],
    paid: ['st.paid.title', you ? 'st.paid.bodyYou' : 'st.paid.body', { date: fmtDate(bill.claimBy) }],
    refundDue: ['st.refundDue.title', 'st.refundDue.body', { date: fmtDate(bill.claimBy) }],
    collected: ['st.collected.title', 'st.collected.body', { amount }],
    returned: ['st.returned.title', you ? 'st.returned.bodyYou' : 'st.returned.body', { amount }],
    declined: ['st.declined.title', you ? 'st.declined.bodyYou' : 'st.declined.body', { amount }],
    cancelled: ['st.cancelled.title', 'st.cancelled.body', {}],
  };
  const [title, body, vars] = map[phase] || map.open;
  return { title: t(title), body: t(body, vars) };
}

function skeletonPaper() {
  return h('div', { class: 'paper paper--skeleton', 'aria-hidden': 'true' },
    h('div', { class: 'sk sk--short' }),
    h('div', { class: 'sk sk--amount' }),
    h('div', { class: 'sk sk--line' }),
    h('div', { class: 'sk sk--line sk--mid' }),
    h('div', { class: 'perf' }),
    h('div', { class: 'sk sk--line' }),
    h('div', { class: 'sk sk--line' }),
    h('div', { class: 'sk sk--line sk--mid' }));
}

/* ------------------------------------------------------------------ example bills (preview + landing) */

const EX = {
  text: 'Term 2 tuition, Grade 5',
  salt: '3b9f2c71d04e8a65c1f0b7d29e4a6c83',
  payee: '0x5b7c3a0e9d2f4b61a8c4e0f3d2b1a9c8e7f6d5a4',
  payer: '0x8c1d40b2e7a9f3c5d6e8b0a1f2c3d4e5f6a7b8c9',
  fp: null,
};
const EX_STATES = ['open', 'paid', 'collected', 'returned'];

function exampleBill(state) {
  const now = Math.floor(Date.now() / 1000);
  const base = { id: 42n, payee: EX.payee, amount: 120_000_000n, payer: ZERO, payBy: now + 12 * DAY, claimWindow: 7 * DAY, allowedPayer: ZERO, claimBy: 0, status: STATUS.Open, ref: EX.fp };
  if (state === 'open') return base;
  const paidAt = state === 'returned' ? now - 9 * DAY : now - 2 * DAY - 5 * 3600;
  const paid = { ...base, payer: EX.payer, claimBy: paidAt + 7 * DAY, status: STATUS.Paid };
  if (state === 'paid') return paid;
  if (state === 'collected') return { ...paid, status: STATUS.Claimed };
  return { ...paid, status: STATUS.Refunded };
}

function exampleCard(initial = 'paid', { onState } = {}) {
  let state = initial;
  const slot = h('div', { class: 'paper-slot' });
  const lineSlot = h('div', { class: 'deadline-slot' });
  const draw = (land) => {
    const bill = exampleBill(state);
    const now = Math.floor(Date.now() / 1000);
    const phase = phaseOf(bill, now);
    slot.replaceChildren(paperEl(bill, { phase, refCheck: { state: 'ok', text: EX.text }, example: true, land }));
    const dl = deadlineEl(bill, phase, now);
    lineSlot.replaceChildren(...(dl ? [dl] : []));
    onState?.(bill, phase);
  };
  const buttons = EX_STATES.map((s) => h('button', {
    type: 'button', class: 'seg-btn', 'aria-pressed': String(s === state),
    onclick: (ev) => {
      state = s;
      for (const b of buttons) b.setAttribute('aria-pressed', String(b === ev.currentTarget));
      draw(true);
    },
  }, t('ex.' + s)));
  draw(false);
  return h('div', { class: 'example' },
    h('div', { class: 'seg', role: 'group', 'aria-label': t('ex.switch') }, buttons),
    slot, lineSlot);
}

/* ------------------------------------------------------------------ chrome: header, banner, footer */

function langSelect(id) {
  const sel = h('select', { id, class: 'lang-select', onchange: (e) => setLang(e.target.value, id) },
    LANGS.map((l) => h('option', { value: l.code, selected: l.code === lang, lang: l.code }, l.label)));
  return h('div', { class: 'lang' }, h('label', { for: id, class: 'sr-only' }, t('lang.label')), sel);
}

function chrome(r) {
  const main = h('main', { id: 'main', tabindex: '-1', class: 'main' });
  const header = h('header', { class: 'top' },
    h('div', { class: 'wrap top-in' },
      h('a', { class: 'brand', href: '#/' }, h('span', { class: 'brand-mark', 'aria-hidden': 'true' }), 'PaidThrough'),
      h('nav', { class: 'top-nav', 'aria-label': t('nav.label') },
        h('a', { href: '#/desk', class: 'top-link', 'aria-current': r.name === 'desk' ? 'page' : null }, t('nav.desk'))),
      langSelect('lang-top')));
  const skip = h('a', { class: 'skip', href: '#main', onclick: (e) => { e.preventDefault(); main.focus(); } }, t('skip'));
  const banner = PREVIEW
    ? h('div', { class: 'preview', role: 'note' }, h('div', { class: 'wrap' }, h('p', null, h('strong', null, t('preview.label')), ' ', t('preview.body'))))
    : null;
  const footer = h('footer', { class: 'foot' },
    h('div', { class: 'wrap foot-in' },
      h('p', { class: 'foot-legal' }, t('footer.legal')),
      h('p', { class: 'foot-links' }, h('a', { href: '#/notes' }, t('footer.notes'))),
      h('div', { class: 'foot-lang' },
        langSelect('lang-foot'),
        h('p', { class: 'foot-note', lang: 'en' }, 'Filipino: machine-drafted\u00a0translation, awaiting review by\u00a0a\u00a0native\u00a0speaker'))));
  liveEl = h('div', { class: 'sr-only', 'aria-live': 'polite', role: 'status' });
  document.getElementById('app').replaceChildren(skip, header, ...(banner ? [banner] : []), main, footer, liveEl);
  return main;
}

/* ------------------------------------------------------------------ router */

let viewSeq = 0;
let disposers = [];
const onLeave = (fn) => disposers.push(fn);

function parseRoute() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const qAt = raw.indexOf('?');
  const path = qAt >= 0 ? raw.slice(0, qAt) : raw;
  const params = new URLSearchParams(qAt >= 0 ? raw.slice(qAt + 1) : '');
  const parts = path.split('/').filter(Boolean);
  return { name: parts[0] || 'home', id: parts[1], params };
}

async function route({ keepFocus, focusId } = {}) {
  for (const fn of disposers) { try { fn(); } catch { /* ignore */ } }
  disposers = [];
  const seq = ++viewSeq;
  const r = parseRoute();
  document.documentElement.lang = lang;
  document.title = TITLES[r.name] ? `${t(TITLES[r.name])} · PaidThrough` : 'PaidThrough'; // bill views refine it
  const main = chrome(r);
  const view = VIEWS[r.name] || viewMissing;
  const alive = () => seq === viewSeq;
  try {
    await view(main, r, alive);
  } catch (e) {
    if (alive()) main.replaceChildren(h('div', { class: 'wrap narrow' }, h('h1', { class: 'h-view' }, t('err.title')), h('p', { class: 'lede' }, explain(e))));
  }
  if (alive() && focusId) document.getElementById(focusId)?.focus();
  if (alive() && !keepFocus && navigated) main.focus({ preventScroll: false });
  if (!keepFocus && navigated) window.scrollTo(0, 0);
}

let navigated = false;
window.addEventListener('hashchange', () => { navigated = true; route(); });

function billIdFrom(r) {
  return /^[1-9]\d{0,17}$/.test(r.id || '') ? BigInt(r.id) : null;
}

/* ------------------------------------------------------------------ view: landing */

function goForm(kind) {
  const id = 'go-' + kind;
  const err = h('p', { class: 'field-err', id: id + '-err', hidden: true });
  const input = h('input', { id, name: id, class: 'input', inputmode: 'text', autocomplete: 'off', placeholder: t('home.input.ph'), 'aria-describedby': id + '-err' });
  return h('form', {
    class: 'go', novalidate: true,
    onsubmit: (e) => {
      e.preventDefault();
      const v = input.value.trim();
      const m = v.match(/#\/(?:bill|pay)\/(\d+)(\?.*)?$/) || v.match(/^(?:no\.?\s*)?(\d{1,18})$/i);
      if (!m || m[1] === '0') {
        err.textContent = t('home.input.bad');
        err.hidden = false;
        input.setAttribute('aria-invalid', 'true');
        input.focus();
        return;
      }
      location.hash = `#/${kind}/${m[1]}${m[2] || ''}`;
    },
  },
  h('label', { for: id, class: 'label' }, t('home.input.label')),
  h('div', { class: 'go-row' }, input, h('button', { type: 'submit', class: 'btn btn--primary' }, t(kind === 'pay' ? 'home.payer.go' : 'home.family.go'))),
  err);
}

async function viewHome(main) {
  const entries = h('section', { class: 'entries', 'aria-labelledby': 'start-h' },
    h('h2', { id: 'start-h', class: 'eyebrow' }, t('home.start')),
    h('div', { class: 'entry' },
      h('div', { class: 'entry-text' }, h('h3', { class: 'entry-title' }, t('home.biller.title')), h('p', null, t('home.biller.body'))),
      h('a', { class: 'btn btn--primary', href: '#/desk' }, t('home.biller.go'))),
    h('div', { class: 'entry entry--form' },
      h('div', { class: 'entry-text' }, h('h3', { class: 'entry-title' }, t('home.payer.title')), h('p', null, t('home.payer.body'))),
      goForm('pay')),
    h('div', { class: 'entry entry--form' },
      h('div', { class: 'entry-text' }, h('h3', { class: 'entry-title' }, t('home.family.title')), h('p', null, t('home.family.body'))),
      goForm('bill')));

  main.append(h('div', { class: 'wrap home' },
    h('div', { class: 'home-text' },
      h('p', { class: 'eyebrow' }, t('home.eyebrow')),
      h('h1', { class: 'h-display' }, t('home.title')),
      h('p', { class: 'lede' }, t('home.lede')),
      h('p', { class: 'home-limits' }, t('home.limits'))),
    h('figure', { class: 'desk-panel home-bill' },
      exampleCard('paid'),
      h('figcaption', { class: 'caption' }, t('home.caption'))),
    entries));
}

/* ------------------------------------------------------------------ view: family status (no wallet) */

async function viewBill(main, r, alive) {
  return billPage(main, r, alive, 'status');
}
async function viewPay(main, r, alive) {
  return billPage(main, r, alive, 'pay');
}

async function billPage(main, r, alive, mode) {
  const id = billIdFrom(r);
  const payView = mode === 'pay';
  // "You" comes from the chain, never from the route: the connected wallet is the payer only if the bill says so.
  const isYou = (bill) => !!account && !isZero(bill.payer) && sameAddr(bill.payer, account);
  // Three areas: head (title), panel (the paper), side (actions). Phones stack them in that order.
  const head = h('div', { class: 'bill-head' });
  const panel = h('div', { class: 'desk-panel bill-panel' });
  const side = h('div', { class: 'bill-side' });
  const eyebrow = () => h('p', { class: 'eyebrow' }, t(payView ? 'view.payEyebrow' : 'view.statusEyebrow'));
  const setTitle = (text) => { document.title = `${t('bill.no', { id: String(id) })}: ${text} · PaidThrough`; };
  main.append(h('div', { class: 'wrap bill-layout' }, head, panel, side));

  if (id == null) {
    panel.remove();
    head.append(h('h1', { class: 'h-view' }, t('view.badId')), h('p', { class: 'lede' }, h('a', { href: '#/' }, t('view.home'))));
    document.title = `${t('view.badId')} · PaidThrough`;
    return;
  }

  if (PREVIEW) {
    const card = exampleCard(payView ? 'open' : 'paid', {
      onState: (bill, phase) => {
        const c = statusCopy(bill, phase, { you: false, payView });
        head.replaceChildren(eyebrow(), h('h1', { class: 'h-view' }, c.title), h('p', { class: 'lede' }, c.body));
        setTitle(c.title);
        // The preview pay button only appears with the example in its unpaid state.
        side.replaceChildren(h('p', { class: 'note' }, t('preview.view', { id: String(id) })), ...(payView && phase === 'open' ? [payPanelPreview()] : []));
      },
    });
    panel.append(card);
    return;
  }

  panel.append(skeletonPaper());
  head.append(eyebrow(), h('h1', { class: 'h-view' }, t('view.loading')));
  setTitle(t('view.loading'));
  let last = null; // { bill, phase }
  let lastKey = '';
  let refCheck = null;
  let timer = null;
  // busy: a wallet prompt is open; done: this page's own payment receipt; tried: Pay was pressed but the bill
  // was no longer open; flash: one-off line shown after the next redraw (e.g. a refund that just went through).
  const payState = { busy: false, done: null, tried: false, flash: null };
  const billKey = (b) => [b.status, b.payer.toLowerCase(), b.claimBy, b.payBy].join('|');

  const actions = h('div', { class: 'status-actions' });
  const meta = h('p', { class: 'status-meta' });

  async function load(land) {
    let bill;
    try {
      bill = await readBill(id);
    } catch (e) {
      if (!alive()) return;
      meta.textContent = t('view.rpcDown');
      if (!last) {
        panel.replaceChildren(skeletonPaper());
        head.replaceChildren(eyebrow(), h('h1', { class: 'h-view' }, t('err.title')), h('p', { class: 'lede' }, explain(e)));
        setTitle(t('err.title')); // the tab title no longer says "loading"
        side.replaceChildren(meta);
      }
      schedule();
      return;
    }
    if (!alive()) return;
    if (!bill) {
      panel.remove();
      head.replaceChildren(eyebrow(), h('h1', { class: 'h-view' }, t('view.notFound', { id: String(id), network: net.label })), h('p', { class: 'lede' }, t('view.notFoundBody')));
      setTitle(t('view.notFoundShort'));
      side.replaceChildren();
      return;
    }
    if (!refCheck) refCheck = await checkRef(bill, r.params);
    if (!alive()) return;
    // Under a wallet prompt nothing is touched, `last` included, so the next read after the prompt sees the change.
    if (payState.busy) { schedule(); return; }
    const phase = phaseOf(bill, chainNow());
    const key = billKey(bill);
    const first = !last;
    const phaseChanged = !first && last.phase !== phase;
    const billChanged = !first && lastKey !== key;
    last = { bill, phase };
    lastKey = key;
    if (first || phaseChanged || billChanged || land) draw(land || phaseChanged);
    else drawDeadline(); // nothing changed on chain: keep the paper (and keyboard focus, reading position) as it is
    if (phaseChanged) announce(statusCopy(bill, phase, { you: isYou(bill), payView }).title);
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    if (last && !MOVING.has(last.phase)) return;
    timer = setTimeout(() => { if (alive() && !document.hidden) load(false); else if (alive()) schedule(); }, CONFIG.refreshMs);
  }
  const onVis = () => { if (!document.hidden && alive() && (!last || MOVING.has(last.phase))) load(false); };
  document.addEventListener('visibilitychange', onVis);
  onLeave(() => { clearTimeout(timer); document.removeEventListener('visibilitychange', onVis); });

  const metaText = (phase) => (MOVING.has(phase) ? t('view.auto', { time: fmtDate(chainNow(), { noYear: true, noZone: true }) }) : '');

  function drawDeadline() {
    const { bill, phase } = last;
    const dl = deadlineEl(bill, phase, chainNow());
    const old = panel.querySelector('.deadline');
    if (old && dl) old.replaceWith(dl);
    else if (old) old.remove();
    else if (dl) panel.append(dl);
    meta.textContent = metaText(phase);
  }

  function draw(land) {
    const { bill, phase } = last;
    const you = isYou(bill);
    const dl = deadlineEl(bill, phase, chainNow());
    panel.replaceChildren(paperEl(bill, { phase, refCheck, land, you }), ...(dl ? [dl] : []));
    const c = statusCopy(bill, phase, { you, payView });
    head.replaceChildren(eyebrow(), h('h1', { class: 'h-view' }, c.title), h('p', { class: 'lede' }, c.body));
    setTitle(c.title);
    meta.textContent = metaText(phase);
    actions.replaceChildren(...sideActions(bill, phase, you));
    const flash = payState.flash ? h('p', { class: 'done-line', id: 'flash', tabindex: '-1' }, payState.flash) : null;
    payState.flash = null;
    side.replaceChildren(...[flash, actions, refNote(), linksEl(bill), meta].filter(Boolean));
  }

  function refNote() {
    if (!refCheck) return null;
    if (refCheck.state === 'bad') return h('p', { class: 'note note--bad' }, t('bill.refBadLong'));
    if (refCheck.state === 'missing') return h('p', { class: 'note' }, t('bill.refMissing'));
    return null;
  }

  const linkRef = () => (refCheck?.text != null && /^[0-9a-f]{32}$/i.test(r.params.get('s') || '') ? { text: refCheck.text, salt: r.params.get('s').toLowerCase() } : null);

  function linksEl(bill) {
    const ref = linkRef();
    const statusUrl = shareLink('bill', bill.id, ref);
    const items = [];
    if (payView && last.phase !== 'open') items.push(h('li', null, h('a', { href: statusUrl.slice(statusUrl.indexOf('#')) }, t('view.toStatus'))));
    if (!payView && last.phase === 'open') items.push(h('li', null, h('a', { href: shareLink('pay', bill.id, ref).replace(/^[^#]*/, '') }, t('view.toPay'))));
    items.push(h('li', null, h('a', { href: `${net.explorer}/address/${net.paidThrough}`, target: '_blank', rel: 'noopener' }, t('view.explorer'))));
    return h('ul', { class: 'links' }, items);
  }

  function sideActions(bill, phase, you) {
    if (phase === 'refundDue') return refundBox(bill);
    if (!payView) return [];
    if (phase === 'open') return [payBox(bill)];
    if (you) return [shareBox(bill, payState.done)];
    // Pay view of a bill this wallet did not pay: say so plainly, and that nothing was taken from it.
    const byOther = !isZero(bill.payer);
    if (!byOther && !payState.tried) return [];
    const text = byOther
      ? t(payState.tried ? 'pay.takenNotCharged' : 'pay.paidByOther', { addr: short(bill.payer) })
      : t('pay.closedNotCharged');
    return [h('div', { class: 'action-box' }, h('p', { class: 'pay-other', id: 'pay-note', tabindex: '-1' }, text))];
  }

  function refundBox(bill) {
    const msg = h('p', { class: 'msg', role: 'status' });
    const btn = h('button', { type: 'button', class: 'btn btn--primary', disabled: !wallet() }, t('view.refundBtn'));
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      let ok = false;
      try {
        if (!account) await connect(); else await ensureChain();
        const res = await sendTx(net.paidThrough, SEL.refund + encUint(bill.id), (step, info) => {
          msg.textContent = step === 'confirm' ? t('tx.confirm', { fee: info.fee }) : t('tx.wait');
        });
        payState.flash = t('view.refundDone', { secs: (res.ms / 1000).toFixed(1) });
        ok = true;
      } catch (e) {
        showError(msg, e, { action: 'refund', bill });
        btn.disabled = false;
      }
      if (ok) {
        await load(true);
        document.getElementById('flash')?.focus();
      }
    });
    return [h('div', { class: 'action-box' },
      btn,
      !wallet() ? h('p', { class: 'hint' }, t('view.refundNoWallet')) : h('p', { class: 'hint' }, t('view.refundAnyone')),
      msg)];
  }

  // After paying (or on a later visit by the wallet that paid): the family link, with what it reveals.
  function shareBox(bill, d) {
    const statusUrl = shareLink('bill', bill.id, linkRef());
    const copyBtn = h('button', { type: 'button', class: 'btn btn--quiet', onclick: (e) => copyText(statusUrl, e.currentTarget) }, t('common.copyLink'));
    return h('div', { class: 'action-box action-box--done' },
      d ? h('p', { class: 'done-line', id: 'pay-done', tabindex: '-1' }, t('pay.done', { secs: (d.ms / 1000).toFixed(1) })) : null,
      d ? h('p', null, h('a', { href: `${net.explorer}/tx/${d.hash}`, target: '_blank', rel: 'noopener' }, t('pay.viewTx'))) : null,
      h('p', { class: 'label', id: 'share-l' }, t('pay.shareFamily')),
      h('p', { class: 'pay-public' }, t('pay.public')),
      h('div', { class: 'copy-row' }, h('input', { class: 'input input--link', readonly: true, value: statusUrl, 'aria-labelledby': 'share-l' }), copyBtn));
  }

  function payBox(bill) {
    const box = h('div', { class: 'action-box' });
    const msg = h('p', { class: 'msg', role: 'status', tabindex: '-1' });
    const info = h('div', { class: 'pay-info' });
    const amount = fmtUnits(bill.amount);
    const sum = () => h('p', { class: 'pay-sum' }, t('pay.sum', { amount, fee: feeText(CONFIG.gasHint.payWithAuthorization, baseFee) }));
    const publicNote = () => h('p', { class: 'pay-public' }, t('pay.public'));

    if (!wallet()) {
      // Typical case: the link was opened inside a chat app. Give the amount and a way to carry the link over.
      const here = location.href;
      box.append(sum(), h('p', null, t('pay.noWallet')),
        h('button', { type: 'button', class: 'btn btn--primary btn--wide', onclick: (e) => copyText(here, e.currentTarget) }, t('pay.copyThisLink')));
      return box;
    }
    if (!account) {
      const connectBtn = h('button', { type: 'button', class: 'btn btn--primary btn--wide' }, t('pay.connect'));
      connectBtn.addEventListener('click', async () => {
        connectBtn.disabled = true;
        try {
          await connect();
          draw(false);
          document.getElementById('pay-btn')?.focus();
        } catch (err) { showError(msg, err, { action: 'pay', bill }); connectBtn.disabled = false; }
      });
      box.append(sum(), publicNote(), connectBtn, h('p', { class: 'hint' }, t('pay.oneSig')), msg);
      return box;
    }

    const allowedOk = isZero(bill.allowedPayer) || sameAddr(bill.allowedPayer, account);
    // 'sig' = one EIP-3009 signature + one transaction; 'approve' = allowance + pay (two transactions).
    // A payer address with code (smart account, EIP-7702 delegate) gets 'approve': Arc's USDC checks such
    // signatures through ERC-1271, so a plain typed-data signature would be rejected as invalid.
    let mode = 'sig';
    let noFunds = false;
    const payBtn = h('button', { type: 'button', id: 'pay-btn', class: 'btn btn--primary btn--wide', disabled: !allowedOk }, t('pay.btn', { amount }));
    const how = h('p', { class: 'hint' }, t('pay.oneSig'));
    const altBtn = h('button', { type: 'button', class: 'btn-text', disabled: !allowedOk }, t('pay.fallback'));
    const alt = h('div', { class: 'alt' }, altBtn, h('p', { class: 'hint' }, t('pay.fallbackWhy', { amount })));
    const feeLine = h('p', { class: 'pay-fee' }, t('pay.fee', { fee: feeText(CONFIG.gasHint.payWithAuthorization, baseFee) }));
    info.append(h('p', { class: 'pay-from' }, t('pay.from', { addr: short(account) })), feeLine);
    // Neutral, local-only hint: has this browser paid this biller address before? (No claim about the biller.)
    const paidToKey = `pt.paidTo.${net.chainId}`;
    const seenBefore = (store.get(paidToKey) || []).includes(bill.payee.toLowerCase());

    function twoStep(why) {
      mode = 'approve';
      payBtn.textContent = t(why === 'finish' ? 'pay.finish' : 'pay.btn2', { amount });
      how.textContent = t(why === 'finish' ? 'pay.finishHow' : 'pay.smart', { amount });
      alt.hidden = true;
      feeLine.textContent = t('pay.fee', { fee: feeText(why === 'finish' ? CONFIG.gasHint.pay : CONFIG.gasHint.approve + CONFIG.gasHint.pay, baseFee) });
    }
    const codeReady = rpc('eth_getCode', [account, 'latest']).catch(() => '0x').then((code) => {
      if (alive() && code && code !== '0x' && !/^0x0*$/.test(code)) twoStep('smart');
    });

    usdcBalance(account).then((bal) => {
      if (!alive()) return;
      const need = bill.amount + parseUnits(feeText(CONFIG.gasHint.payWithAuthorization, baseFee));
      const shown = fmtUnits(bal - (bal % 10_000n)); // cents, rounded down
      info.append(h('p', { class: 'pay-bal' + (bal < need ? ' pay-bal--low' : '') },
        bal < need ? t('pay.low', { have: shown, need: fmtUnits(need) }) : t('pay.balance', { amount: shown })));
      if (bal < bill.amount) { payBtn.disabled = true; altBtn.disabled = true; noFunds = true; }
    }).catch(() => { /* balance is informative only */ });

    const step = (s, i) => {
      const k = { sign: 'pay.step.sign', confirm: 'tx.confirm', wait: 'tx.wait', approve: 'pay.step.approve', pay2: 'pay.step.pay2' }[s];
      msg.classList.remove('msg--bad');
      msg.textContent = t(k, i || {});
      if (s === 'confirm' && i?.fee) feeLine.textContent = t('pay.feeSigned', { fee: i.fee });
    };
    const enable = () => {
      if (!payBtn.isConnected) return;
      payBtn.disabled = !allowedOk || noFunds;
      altBtn.disabled = !allowedOk || noFunds;
    };

    async function run(kind) {
      if (payState.busy) return;
      payState.busy = true;
      payBtn.disabled = true; altBtn.disabled = true;
      let outcome = null; // 'paid' | 'taken' | null (error shown, buttons back on)
      let drawnFromReceipt = false;
      let focusAlt = false;
      try {
        await codeReady;
        const how2 = kind === 'auto' ? mode : kind;
        await ensureChain();
        const fresh = await readBill(id);
        if (!fresh || phaseOf(fresh, chainNow()) !== 'open') {
          payState.tried = true;
          outcome = 'taken';
        } else {
          if (how2 === 'approve') how.textContent = t('pay.twoStepHow', { amount });
          const res = how2 === 'sig' ? await payWithSignature(fresh, step) : await payWithApproval(fresh, step);
          payState.done = res;
          store.set(paidToKey, [...new Set([fresh.payee.toLowerCase(), ...(store.get(paidToKey) || [])])].slice(0, 50));
          // Show the stamp from the receipt at once; the follow-up read confirms it.
          const paidLog = res.receipt.logs.find((l) => l.topics[0] === TOPIC.BillPaid && sameAddr(l.address, net.paidThrough));
          if (paidLog) {
            const claimBy = Number(wUint(words(paidLog.data)[0]));
            last = { bill: { ...fresh, status: STATUS.Paid, payer: account, claimBy }, phase: 'paid' };
            lastKey = billKey(last.bill);
            draw(true);
            announce(t('pay.done', { secs: (res.ms / 1000).toFixed(1) }));
            document.getElementById('pay-done')?.focus();
            drawnFromReceipt = true;
          }
          outcome = 'paid';
        }
      } catch (e) {
        if (codeOf(e) === 'APPROVED_NOT_PAID') {
          // Step 1 (allowance) is on Arc, step 2 (payment) is not. Say exactly that, and make Pay finish it.
          twoStep('finish');
          const why = codeOf(e.cause) === 4001 ? '' : explain(e.cause, { action: 'pay', bill }) + ' ';
          msg.classList.add('msg--bad');
          msg.textContent = why + t('pay.approvedNotPaid', { amount });
        } else {
          showError(msg, e, { action: 'pay', bill });
        }
        focusAlt = codeOf(e) === 'NO_SIGN_TYPED';
      } finally {
        payState.busy = false;
        enable(); // every exit path leaves the buttons usable (or the box replaced)
      }
      if (focusAlt) altBtn.focus();
      else if (outcome === null && payBtn.isConnected) msg.focus();
      if (outcome) {
        // After busy is cleared. A receipt-drawn page is only confirmed (no second redraw, focus stays put).
        await load(!drawnFromReceipt);
        if (outcome === 'paid' && !drawnFromReceipt) document.getElementById('pay-done')?.focus();
        if (outcome === 'taken') {
          const note = document.getElementById('pay-note');
          if (note) { announce(note.textContent); note.focus(); }
        }
      }
    }
    payBtn.addEventListener('click', () => run('auto'));
    altBtn.addEventListener('click', () => run('approve'));

    const kids = [info];
    if (!seenBefore) kids.push(h('p', { class: 'pay-first' }, t('pay.firstTime')));
    kids.push(publicNote());
    if (!allowedOk) kids.push(h('p', { class: 'msg msg--bad' }, t('pay.notAllowed', { addr: short(bill.allowedPayer) })));
    kids.push(h('p', { class: 'pay-check' }, t('pay.checkBiller')), payBtn, how, msg, alt);
    box.append(...kids);
    return box;
  }

  await load(false);
}

function payPanelPreview() {
  return h('div', { class: 'action-box' },
    h('button', { type: 'button', class: 'btn btn--primary btn--wide', disabled: true }, t('pay.btn', { amount: fmtUnits(120_000_000n) })),
    h('p', { class: 'hint' }, t('pay.previewOff')));
}

// One signature (EIP-3009 ReceiveWithAuthorization), then one transaction from the payer's own wallet.
async function payWithSignature(bill, step) {
  const nonce = '0x' + (words(await ethCall(net.paidThrough, SEL.authNonce + encUint(bill.id)))[0] || '');
  if (nonce.length !== 66) throw new AppError('BAD_REPLY', 'authNonce');
  await syncClock();
  const validBefore = Math.min(bill.payBy, chainNow() + 3600);
  const typed = {
    types: {
      EIP712Domain: [
        { name: 'name', type: 'string' },
        { name: 'version', type: 'string' },
        { name: 'chainId', type: 'uint256' },
        { name: 'verifyingContract', type: 'address' },
      ],
      ReceiveWithAuthorization: [
        { name: 'from', type: 'address' },
        { name: 'to', type: 'address' },
        { name: 'value', type: 'uint256' },
        { name: 'validAfter', type: 'uint256' },
        { name: 'validBefore', type: 'uint256' },
        { name: 'nonce', type: 'bytes32' },
      ],
    },
    primaryType: 'ReceiveWithAuthorization',
    domain: { name: 'USDC', version: '2', chainId: net.chainId, verifyingContract: net.usdc },
    message: {
      from: account,
      to: net.paidThrough,
      value: bill.amount.toString(),
      validAfter: '0',
      validBefore: String(validBefore),
      nonce,
    },
  };
  step('sign');
  let sig;
  try {
    sig = await wallet().request({ method: 'eth_signTypedData_v4', params: [account, JSON.stringify(typed)] });
  } catch (e) {
    const c = codeOf(e);
    if (c === 4200 || c === -32601 || /not supported|unsupported|does not exist|not found/i.test(String(e?.message))) throw new AppError('NO_SIGN_TYPED');
    throw e;
  }
  const s = strip(String(sig));
  if (s.length !== 130) throw new AppError('BAD_SIG', 'signature length ' + s.length);
  let v = parseInt(s.slice(128, 130), 16);
  if (v < 27) v += 27;
  const data = SEL.payWithAuthorization + encUint(bill.id) + encAddr(account) + encUint(0) + encUint(validBefore)
    + encUint(v) + s.slice(0, 64) + s.slice(64, 128);
  return sendTx(net.paidThrough, data, step);
}

// Fallback for wallets without typed-data signing: approve exactly the amount, then pay.
async function payWithApproval(bill, step) {
  const allowance = wUint(words(await ethCall(net.usdc, SEL.allowance + encAddr(account) + encAddr(net.paidThrough)))[0] || '0');
  if (allowance < bill.amount) {
    step('approve');
    await sendTx(net.usdc, SEL.approve + encAddr(net.paidThrough) + encUint(bill.amount), (s, i) => step(s === 'confirm' ? 'approve' : s, i));
  }
  step('pay2');
  try {
    return await sendTx(net.paidThrough, SEL.pay + encUint(bill.id), (s, i) => step(s === 'confirm' ? 'pay2' : s, i));
  } catch (e) {
    // A sent-but-unconfirmed or failed payment keeps its own error (with the tx link). Anything else means the
    // allowance is on Arc but no payment went out, which the page must say instead of "nothing was sent".
    if (codeOf(e) === 'TIMEOUT' || codeOf(e) === 'REVERTED') throw e;
    throw Object.assign(new AppError('APPROVED_NOT_PAID', 'allowance set, payment not sent'), { cause: e });
  }
}

/* ------------------------------------------------------------------ view: biller desk */

const draft = { amount: '', ref: '', payBy: '', window: String(7 * DAY), only: '' };

function dateInputValue(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function payByFromDate(v) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v || '');
  if (!m) return null;
  return Math.floor(new Date(+m[1], +m[2] - 1, +m[3], 23, 59, 59).getTime() / 1000);
}

async function viewDesk(main, r, alive) {
  if (!draft.payBy) draft.payBy = dateInputValue(Math.floor(Date.now() / 1000) + 14 * DAY);
  const formCol = h('section', { class: 'desk-form', 'aria-labelledby': 'issue-h' });
  const listCol = h('section', { class: 'desk-list', 'aria-labelledby': 'mine-h' });
  main.append(h('div', { class: 'wrap desk' },
    h('div', { class: 'desk-head' },
      h('p', { class: 'eyebrow' }, t('desk.eyebrow')),
      h('h1', { class: 'h-view' }, t('desk.title')),
      h('p', { class: 'lede' }, t('desk.lede'))),
    formCol, listCol));

  drawForm();
  drawList();

  function drawForm(issued) {
    if (issued) { formCol.replaceChildren(...issuedView(issued)); return; }
    formCol.replaceChildren(...issueForm());
  }

  function field(id, labelKey, control, hintKey, extra) {
    const err = h('p', { class: 'field-err', id: id + '-err', hidden: true });
    control.setAttribute('aria-describedby', `${id}-hint ${id}-err`);
    return {
      el: h('div', { class: 'field' },
        h('label', { for: id, class: 'label' }, t(labelKey)),
        control,
        hintKey ? h('p', { class: 'hint', id: id + '-hint' }, t(hintKey)) : null,
        extra || null,
        err),
      setErr(msg) {
        err.textContent = msg || '';
        err.hidden = !msg;
        const input = control.matches('input,select') ? control : control.querySelector('input,select');
        if (msg) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
      },
      input: control.matches('input,select') ? control : control.querySelector('input,select'),
    };
  }

  function issueForm() {
    const now = Math.floor(Date.now() / 1000);
    const amountIn = h('input', { id: 'f-amount', class: 'input input--amount', inputmode: 'decimal', autocomplete: 'off', placeholder: '0.00', value: draft.amount });
    const amount = field('f-amount', 'desk.amount', h('div', { class: 'input-unit' }, amountIn, h('label', { class: 'unit', for: 'f-amount' }, 'USDC')), 'desk.amountHint');
    const refIn = h('input', { id: 'f-ref', class: 'input', maxlength: String(REF_MAX), autocomplete: 'off', placeholder: t('desk.refPh'), value: draft.ref });
    const ref = field('f-ref', 'desk.ref', refIn, 'desk.refHint',
      h('p', { class: 'public-warn', id: 'f-ref-warn' }, h('strong', null, t('desk.publicA')), ' ', t('desk.publicB')));
    refIn.setAttribute('aria-describedby', 'f-ref-hint f-ref-warn f-ref-err');
    const payByIn = h('input', { id: 'f-payby', class: 'input', type: 'date', value: draft.payBy, min: dateInputValue(now), max: dateInputValue(now + 364 * DAY) });
    const payBy = field('f-payby', 'desk.payBy', payByIn, 'desk.payByHint');
    const winIn = h('select', { id: 'f-window', class: 'input select' }, WINDOWS.map((s) => h('option', { value: String(s), selected: String(s) === draft.window }, windowLabel(s))));
    const win = field('f-window', 'desk.window', winIn, 'desk.windowHint');
    const onlyIn = h('input', { id: 'f-only', class: 'input mono', autocomplete: 'off', spellcheck: 'false', placeholder: '0x…', value: draft.only });
    const only = field('f-only', 'desk.only', onlyIn, 'desk.onlyHint');

    for (const [inp, key] of [[amountIn, 'amount'], [refIn, 'ref'], [payByIn, 'payBy'], [winIn, 'window'], [onlyIn, 'only']]) {
      inp.addEventListener('input', () => { draft[key] = inp.value; });
    }

    const msg = h('p', { class: 'msg', role: 'status' });
    const submit = h('button', { type: 'submit', class: 'btn btn--primary btn--wide' },
      PREVIEW ? t('desk.submit') : account ? t('desk.submit') : t('desk.connectFirst'));
    if (PREVIEW) submit.disabled = true;
    const fee = h('p', { class: 'hint' }, PREVIEW ? t('desk.previewOff') : t('desk.fee', { fee: feeText(CONFIG.gasHint.issue, baseFee) }));

    function validate() {
      let ok = true;
      const units = parseUnits(amountIn.value);
      if (units == null || units <= 0n || units > CONFIG.maxAmountUnits) { amount.setErr(t('f.amount')); ok = false; } else amount.setErr('');
      const text = refIn.value.trim().normalize('NFC');
      if (!text || text.length > REF_MAX) { ref.setErr(t('f.ref', { max: String(REF_MAX) })); ok = false; } else ref.setErr('');
      const pb = payByFromDate(payByIn.value);
      const cn = chainNow();
      if (!pb || pb <= cn + 60 || pb > cn + 365 * DAY) { payBy.setErr(t('f.payBy')); ok = false; } else payBy.setErr('');
      const o = onlyIn.value.trim();
      if (o && (!isAddr(o) || isZero(o))) { only.setErr(t('f.only')); ok = false; }
      else if (o && !checksumOk(o)) { only.setErr(t('f.onlyChecksum')); ok = false; }
      else only.setErr('');
      if (!ok) {
        const first = [amount, ref, payBy, only].find((f) => f.input.getAttribute('aria-invalid') === 'true');
        first?.input.focus();
        return null;
      }
      return { units, text, payBy: pb, window: Number(winIn.value), only: o || ZERO };
    }

    const form = h('form', {
      class: 'issue', novalidate: true,
      onsubmit: async (e) => {
        e.preventDefault();
        if (PREVIEW) return;
        const v = validate();
        if (!v) return;
        submit.disabled = true;
        msg.classList.remove('msg--bad');
        try {
          if (!account) { await connect(); }
          await ensureChain();
          const salt = newSalt();
          const fp = await fingerprint(salt, v.text);
          const data = SEL.issue + encUint(v.units) + encUint(v.payBy) + encUint(v.window) + encAddr(v.only) + encB32(fp);
          const gas = await estimate(net.paidThrough, data);
          addPending({ text: v.text, salt, fp });
          const res = await sendTx(net.paidThrough, data, (s, i) => {
            msg.textContent = s === 'confirm' ? t('tx.confirm', { fee: i.fee }) : t('tx.wait');
          }, gas);
          const log = res.receipt.logs.find((l) => l.topics[0] === TOPIC.BillIssued && sameAddr(l.address, net.paidThrough));
          if (!log) throw new AppError('BAD_REPLY', 'no BillIssued log');
          const billId = wUint(strip(log.topics[1]));
          // The pending entry may never have been written (storage refused), so save directly as a fallback.
          const saved = settlePending(fp, billId) || saveRef(billId, { text: v.text, salt });
          Object.assign(draft, { amount: '', ref: '', only: '' });
          if (!alive()) return;
          drawForm({ id: billId, ref: { text: v.text, salt }, ms: res.ms, hash: res.hash, saved });
          formCol.querySelector('#issue-h')?.focus();
          announce(t('desk.done', { id: String(billId) }) + (saved ? '' : ' ' + t('store.notSaved')));
          drawList();
        } catch (err) {
          showError(msg, err, { action: 'issue' });
          submit.disabled = false;
          submit.textContent = account ? t('desk.submit') : t('desk.connectFirst');
        }
      },
    },
    amount.el, ref.el,
    h('div', { class: 'field-pair' }, payBy.el, win.el),
    only.el,
    h('div', { class: 'issue-foot' }, submit, fee, msg));

    return [h('h2', { id: 'issue-h', class: 'h-section' }, t('desk.issue')), form];
  }

  function issuedView({ id, ref, ms, hash, saved }) {
    const payUrl = shareLink('pay', id, ref);
    const statusUrl = shareLink('bill', id, ref);
    const linkRow = (labelKey, url, idAttr) => h('div', { class: 'field' },
      h('label', { class: 'label', for: idAttr }, t(labelKey)),
      h('div', { class: 'copy-row' },
        h('input', { id: idAttr, class: 'input input--link', readonly: true, value: url }),
        h('button', { type: 'button', class: 'btn btn--quiet', onclick: (e) => copyText(url, e.currentTarget) }, t('common.copyLink'))));
    return [
      h('h2', { id: 'issue-h', class: 'h-section', tabindex: '-1' }, t('desk.done', { id: String(id) })),
      h('p', { class: 'done-line' }, t('desk.doneIn', { secs: (ms / 1000).toFixed(1) }), ' ', h('a', { href: `${net.explorer}/tx/${hash}`, target: '_blank', rel: 'noopener' }, t('pay.viewTx'))),
      linkRow('desk.payLink', payUrl, 'l-pay'),
      linkRow('desk.statusLink', statusUrl, 'l-status'),
      saved ? h('p', { class: 'note' }, t('desk.keepLinks')) : h('p', { class: 'note note--bad', id: 'not-saved' }, t('store.notSaved')),
      h('button', { type: 'button', class: 'btn btn--quiet', onclick: () => drawForm() }, t('desk.another')),
    ];
  }

  async function drawList() {
    const head = h('div', { class: 'list-head' }, h('h2', { id: 'mine-h', class: 'h-section' }, t('desk.mine')));
    if (PREVIEW) {
      const ex = ['paid', 'open', 'collected', 'returned'].map((s) => exampleBill(s));
      listCol.replaceChildren(head, h('p', { class: 'note' }, t('desk.previewList')), needsLine(ex, Math.floor(Date.now() / 1000)),
        h('ul', { class: 'rows' }, ex.map((b) => billRow(b, { example: true, ref: { text: EX.text, salt: EX.salt } }))));
      return;
    }
    if (!wallet()) { listCol.replaceChildren(head, h('p', { class: 'note' }, t('desk.noWallet'))); return; }
    if (!account) {
      const btn = h('button', {
        type: 'button', class: 'btn btn--quiet',
        onclick: async () => {
          try { await connect(); route({ keepFocus: true }); } catch (e) { note.textContent = explain(e); note.classList.add('msg--bad'); }
        },
      }, t('desk.connect'));
      const note = h('p', { class: 'msg', role: 'status' });
      listCol.replaceChildren(head, h('p', { class: 'note' }, t('desk.mineConnect')), btn, note);
      return;
    }
    head.append(h('p', { class: 'list-who' }, t('desk.connectedAs', { addr: short(account) })));
    const progress = h('p', { class: 'note', role: 'status' }, t('desk.mineLoading', { pct: '0' }));
    listCol.replaceChildren(head, progress, h('ul', { class: 'rows rows--skeleton', 'aria-hidden': 'true' }, [1, 2, 3].map(() => h('li', { class: 'row' }, h('div', { class: 'sk sk--line' }), h('div', { class: 'sk sk--line sk--mid' })))));
    let bills;
    try {
      bills = await myBills(account, (pct) => { if (alive()) progress.textContent = t('desk.mineLoading', { pct: String(pct) }); });
    } catch (e) {
      if (!alive()) return;
      listCol.replaceChildren(head, h('p', { class: 'msg msg--bad' }, explain(e)),
        h('button', { type: 'button', class: 'btn btn--quiet', onclick: () => drawList() }, t('common.retry')));
      return;
    }
    if (!alive()) return;
    if (!bills.length) { listCol.replaceChildren(head, h('p', { class: 'note' }, t('desk.mineEmpty'))); return; }
    // Bills waiting to be collected come first, soonest deadline on top; the rest stay newest first.
    const now = chainNow();
    const waiting = bills.filter((b) => phaseOf(b, now) === 'paid').sort((a, b) => a.claimBy - b.claimBy);
    const others = bills.filter((b) => phaseOf(b, now) !== 'paid');
    listCol.replaceChildren(head, needsLine(bills, now), h('ul', { class: 'rows' }, [...waiting, ...others].map((b) => billRow(b, { ref: loadRef(b.id) }))));
  }

  // One line that says what needs doing: how many bills to collect, how much, and the nearest deadline.
  function needsLine(bills, now) {
    const waiting = bills.filter((b) => phaseOf(b, now) === 'paid').sort((a, b) => a.claimBy - b.claimBy);
    if (!waiting.length) return h('p', { class: 'needs' }, t('desk.needsNone'));
    const total = waiting.reduce((sum, b) => sum + b.amount, 0n);
    const first = waiting[0];
    return h('p', { class: 'needs needs--on' }, t(waiting.length === 1 ? 'desk.needs1' : 'desk.needsN', {
      n: String(waiting.length), total: fmtUnits(total), date: fmtDate(first.claimBy, { noZone: true }), time: fmtDuration(first.claimBy - now),
    }));
  }

  function billRow(bill, { example, ref } = {}) {
    const now = example ? Math.floor(Date.now() / 1000) : chainNow();
    const phase = phaseOf(bill, now);
    const msg = h('p', { class: 'msg', role: 'status', tabindex: '-1' });
    const s = STAMP[phase];
    const when = {
      open: t('row.payBy', { date: fmtDate(bill.payBy, { noZone: true }) }),
      expired: t('row.expired', { date: fmtDate(bill.payBy, { noZone: true }) }),
      paid: t('row.collectBy', { date: fmtDate(bill.claimBy, { noZone: true }), time: fmtDuration(bill.claimBy - now) }),
      refundDue: t('row.refundDue'),
    }[phase] || '';

    const act = (labelKey, fn, cls = 'btn--quiet', confirmKey, action) => {
      const b = h('button', { type: 'button', class: 'btn btn--sm ' + cls, disabled: example || PREVIEW }, t(labelKey));
      b.addEventListener('click', async () => {
        if (confirmKey && b.dataset.armed !== '1') {
          b.dataset.armed = '1';
          b.textContent = t(confirmKey, { amount: fmtUnits(bill.amount) });
          b.classList.add('btn--armed');
          msg.textContent = t('row.confirmHint');
          setTimeout(() => { if (b.isConnected && b.dataset.armed === '1') { b.dataset.armed = ''; b.textContent = t(labelKey); b.classList.remove('btn--armed'); msg.textContent = ''; } }, 6000);
          return;
        }
        for (const x of row.querySelectorAll('button')) x.disabled = true;
        msg.classList.remove('msg--bad');
        try {
          await ensureChain();
          const res = await sendTx(net.paidThrough, fn + encUint(bill.id), (st, i) => { msg.textContent = st === 'confirm' ? t('tx.confirm', { fee: i.fee }) : t('tx.wait'); });
          const fresh = await readBill(bill.id);
          const next = billRow(fresh, { ref });
          row.replaceWith(next);
          const done = next.querySelector('.msg');
          done.textContent = t('row.done', { secs: (res.ms / 1000).toFixed(1) });
          done.focus(); // keyboard and screen-reader position stay on the row that changed
        } catch (e) {
          showError(msg, e, { action, bill });
          for (const x of row.querySelectorAll('button')) x.disabled = false;
          msg.focus();
        }
      });
      return b;
    };

    const actions = [];
    if (phase === 'paid') {
      actions.push(act('row.collect', SEL.claim, 'btn--primary', null, 'claim'));
      actions.push(act('row.decline', SEL.decline, 'btn--quiet', 'row.declineConfirm', 'decline'));
    } else if (phase === 'open' || phase === 'expired') {
      actions.push(act('row.cancel', SEL.cancel, 'btn--quiet', 'row.cancelConfirm', 'cancel'));
    } else if (phase === 'refundDue') {
      actions.push(act('row.refund', SEL.refund, 'btn--quiet', null, 'refund'));
    }

    const payUrl = shareLink('pay', bill.id, ref);
    const statusHref = example ? '#/bill/' + bill.id : shareLink('bill', bill.id, ref).replace(/^[^#]*/, '');
    const links = [h('a', { href: statusHref, class: 'row-link' }, t('row.view'))];
    let restoreForm = null;
    if (phase === 'open') {
      links.push(h('button', { type: 'button', class: 'btn-text', disabled: example, onclick: (e) => copyText(payUrl, e.currentTarget) }, t(ref ? 'row.copyPay' : 'row.copyPayNoRef')));
      if (!ref && !example) {
        restoreForm = restoreEl(bill, (restored, saved) => {
          const next = billRow(bill, { ref: restored });
          row.replaceWith(next);
          const done = next.querySelector('.msg');
          // Not saved: the row still uses the reference for now, but it is gone once the page is left.
          done.textContent = saved ? t('row.restored') : t('store.notSaved');
          done.classList.toggle('msg--bad', !saved);
          done.focus();
        });
        links.push(h('button', {
          type: 'button', class: 'btn-text', 'aria-expanded': 'false',
          onclick: (e) => {
            restoreForm.hidden = !restoreForm.hidden;
            e.currentTarget.setAttribute('aria-expanded', String(!restoreForm.hidden));
            if (!restoreForm.hidden) restoreForm.querySelector('input').focus();
          },
        }, t('row.restore')));
      }
    }

    const row = h('li', { class: 'row', 'data-phase': phase },
      h('div', { class: 'row-main' },
        h('p', { class: 'row-no' }, t('bill.no', { id: String(bill.id) }), example ? h('span', { class: 'row-ex' }, t('row.example')) : null),
        h('p', { class: 'row-amt' }, h('span', { class: 'amt' }, fmtUnits(bill.amount)), ' USDC'),
        h('p', { class: 'row-ref' + (ref ? '' : ' row-ref--none') }, ref ? ref.text : t('row.noRef'))),
      h('div', { class: 'row-state' },
        h('span', { class: 'chip ' + s.look.split(' ').map((x) => 'stamp--' + x).join(' ') }, t(s.word)),
        h('p', { class: 'row-when' }, when)),
      h('div', { class: 'row-actions' }, actions, h('div', { class: 'row-links' }, links)),
      restoreForm,
      msg);
    return row;
  }

  // Rebuild a row's reference from a link the biller saved elsewhere: the link carries text + salt, and the
  // fingerprint on Arc proves they belong to this bill before anything is stored.
  function restoreEl(bill, onDone) {
    const fid = 'restore-' + bill.id;
    const input = h('input', { id: fid, class: 'input input--link', autocomplete: 'off', spellcheck: 'false', 'aria-describedby': fid + '-err' });
    const err = h('p', { class: 'field-err', id: fid + '-err', hidden: true });
    const fail = (key) => { err.textContent = t(key); err.hidden = false; input.setAttribute('aria-invalid', 'true'); input.focus(); };
    return h('form', {
      class: 'restore', hidden: true, novalidate: true,
      onsubmit: async (e) => {
        e.preventDefault();
        const m = input.value.trim().match(/#\/(?:pay|bill)\/(\d+)\?(.*)$/);
        if (!m || m[1] !== String(bill.id)) return fail('row.restoreWrongBill');
        const q = new URLSearchParams(m[2]);
        const text = q.get('r');
        const salt = (q.get('s') || '').toLowerCase();
        if (text == null || !/^[0-9a-f]{32}$/.test(salt)) return fail('row.restoreNoRef');
        if (await fingerprint(salt, text) !== bill.ref.toLowerCase()) return fail('row.restoreMismatch');
        return onDone({ text, salt }, saveRef(bill.id, { text, salt }));
      },
    },
    h('label', { for: fid, class: 'label' }, t('row.restoreLabel')),
    h('div', { class: 'copy-row' }, input, h('button', { type: 'submit', class: 'btn btn--quiet btn--sm' }, t('row.restoreGo'))),
    err);
  }
}

// Bills issued by `payee`: BillIssued logs filtered by the payee topic, scanned in 10,000-block
// chunks (the public RPC's cap) sent as JSON-RPC batches; a chunk halves when the node refuses it.
// Scanned ranges are remembered in this browser so the next visit only reads new blocks.
async function myBills(payee, onProgress) {
  const key = nsKey('scan', payee.toLowerCase());
  const latest = Number(BigInt(await rpc('eth_blockNumber')));
  let ids = [];
  if (net.deployBlock == null) {
    ids = await idsByIteration(payee);
  } else {
    const cache = store.get(key);
    let from = cache && Number.isFinite(cache.to) ? cache.to + 1 : net.deployBlock;
    const known = new Set(cache?.ids || []);
    const total = Math.max(1, latest - from + 1);
    const queue = [];
    for (let f = from; f <= latest; f += CONFIG.logChunk) queue.push([f, Math.min(latest, f + CONFIG.logChunk - 1)]);
    let done = 0;
    try {
      while (queue.length) {
        const batch = queue.splice(0, 8);
        const res = await rpcBatch(batch.map(([a, b]) => ['eth_getLogs', [{
          address: net.paidThrough, fromBlock: toHex(a), toBlock: toHex(b),
          topics: [TOPIC.BillIssued, null, '0x' + encAddr(payee)],
        }]]));
        res.forEach((out, i) => {
          const [a, b] = batch[i];
          if (out instanceof AppError) {
            if (b > a && (out.code === -32012 || /range|limit|too (large|many)/i.test(out.message))) {
              const mid = a + Math.floor((b - a) / 2);
              queue.unshift([a, mid], [mid + 1, b]);
              return;
            }
            throw out;
          }
          for (const log of out) known.add(String(wUint(strip(log.topics[1]))));
          done += b - a + 1;
        });
        onProgress?.(Math.min(99, Math.round((done / total) * 100)));
      }
      store.set(key, { to: latest, ids: [...known] });
      ids = [...known].map((x) => BigInt(x));
    } catch {
      ids = await idsByIteration(payee); // logs refused outright: read bills one by one instead
    }
  }
  onProgress?.(100);
  if (!ids.length) return [];
  ids.sort((a, b) => (a > b ? -1 : 1));
  const raws = [];
  for (let i = 0; i < ids.length; i += 40) {
    const part = ids.slice(i, i + 40);
    raws.push(...await rpcBatch(part.map((id) => ['eth_call', [{ to: net.paidThrough, data: SEL.getBill + encUint(id) }, 'latest']])));
  }
  await syncClock();
  const bills = [];
  raws.forEach((raw, i) => {
    // getBill never reverts, so an error is a refused read: fail the list (it offers a retry), never drop the bill.
    if (raw instanceof AppError) throw raw;
    const b = decodeBill(ids[i], raw);
    if (!sameAddr(b.payee, payee)) return;
    if (!loadRef(b.id)) settlePending(b.ref.toLowerCase(), b.id);
    bills.push(b);
  });
  return bills;
}

async function idsByIteration(payee, cap = 400) {
  const count = Number(wUint(words(await ethCall(net.paidThrough, SEL.billCount))[0] || '0'));
  const ids = [];
  for (let id = count; id >= 1 && ids.length < cap; id--) ids.push(BigInt(id));
  const mine = [];
  for (let i = 0; i < ids.length; i += 40) {
    const part = ids.slice(i, i + 40);
    const res = await rpcBatch(part.map((id) => ['eth_call', [{ to: net.paidThrough, data: SEL.getBill + encUint(id) }, 'latest']]));
    res.forEach((raw, j) => {
      if (raw instanceof AppError) throw raw; // a refused read, not "someone else's bill"
      if (sameAddr(wAddr(words(raw)[0] || ''), payee)) mine.push(part[j]);
    });
  }
  return mine;
}

/* ------------------------------------------------------------------ view: privacy, risks and terms */

async function viewNotes(main) {
  const sections = ['stores', 'public', 'risks', 'terms', 'contact'];
  main.append(h('div', { class: 'wrap narrow notes' },
    h('h1', { class: 'h-view' }, t('notes.title')),
    sections.map((s) => h('section', { class: 'notes-sec', 'aria-labelledby': 'n-' + s },
      h('h2', { id: 'n-' + s, class: 'h-section' }, t(`notes.${s}.h`)),
      t(`notes.${s}.body`).split('\n').map((line) => h('p', null, line))))));
}

async function viewMissing(main) {
  document.title = `${t('view.missing')} · PaidThrough`;
  main.append(h('div', { class: 'wrap narrow' },
    h('h1', { class: 'h-view' }, t('view.missing')),
    h('p', { class: 'lede' }, h('a', { href: '#/' }, t('view.home')))));
}

const VIEWS = { home: viewHome, bill: viewBill, pay: viewPay, desk: viewDesk, notes: viewNotes };
const TITLES = { home: 'title.home', desk: 'desk.title', notes: 'notes.title', bill: 'view.statusEyebrow', pay: 'view.payEyebrow' };

/* ------------------------------------------------------------------ start */

async function start() {
  // Reference fingerprints use WebCrypto, which browsers only offer on https:// (or localhost).
  if (!globalThis.crypto?.subtle) {
    document.documentElement.lang = lang;
    const main = chrome(parseRoute());
    main.append(h('div', { class: 'wrap narrow' }, h('h1', { class: 'h-view' }, t('err.title')), h('p', { class: 'lede' }, t('err.insecure'))));
    return;
  }
  EX.fp = await fingerprint(EX.salt, EX.text);
  await restoreAccount();
  if (!PREVIEW) syncClock().catch(() => { /* views report RPC trouble themselves */ });
  route({ keepFocus: true });
}

start();
