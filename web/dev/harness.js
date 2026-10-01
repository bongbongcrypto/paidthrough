// Dev harness (never linked from index.html). Loads the real app.js against an in-memory Arc:
//   - window.fetch answers the simulated RPC URL from sim.js (other URLs pass through)
//   - window.ethereum is a mock EIP-1193 wallet that checks what the app asks it to sign or send
//   - the shared CONFIG object is pointed at the simulated network before app.js is imported
// URL options: ?mode=preview (leave config alone: the not-deployed preview), ?wallet=none, ?panel=0,
//   ?lang=en|fil|ko (sets the remembered language first), ?connect=biller|payer|other|smart (wallet already
//   connected, for headless screenshots). Seed bills use fixed salts so their links are reproducible:
//   bill N has salt SEED_SALT(N) below.
//   ?seed=messy: long-tail fixture instead of the six tidy bills (999 bills from another biller first, so the
//   biller's 60 bills have 4-digit ids 1000-1059; 10,000.00 and 0.000001 USDC amounts; a 140-character
//   Korean + Filipino reference). See seedMessy().
//   ?store=throw: localStorage.setItem throws (QuotaExceededError) after seeding, like a full or blocked store.
//   ?refuse=1: the RPC refuses every eth_call with a JSON-RPC error (-32005), like a rate-limited public node.
//   Both switches are also in the panel and on window (__storeThrows(on), __sim.refuseCalls) for ?panel=0 runs.

import { CONFIG } from '../config.js';
import { STRINGS } from '../i18n.js';
import { Sim, RpcFail } from './sim.js';
import { SEL } from '../abi.js';

const q = new URLSearchParams(location.search);
const PREVIEW = q.get('mode') === 'preview';
const NO_WALLET = q.get('wallet') === 'none';
const PANEL = q.get('panel') !== '0';
const CONNECT = q.get('connect');
const MESSY = q.get('seed') === 'messy';
if (q.get('lang')) { try { localStorage.setItem('pt.lang', q.get('lang')); } catch { /* fine */ } }
const SEED_SALT = (n) => ('5eed' + String(n).padStart(4, '0')).repeat(4); // 32 hex chars, dev only

// localStorage.setItem switch. Patched on Storage.prototype: assigning localStorage.setItem would only store
// an item named "setItem" and leave the real method in place.
const REAL_SET_ITEM = Storage.prototype.setItem;
let storeThrows = false;
function setStoreThrows(on) {
  storeThrows = !!on;
  Storage.prototype.setItem = storeThrows
    ? function setItem() { throw new DOMException('The quota has been exceeded.', 'QuotaExceededError'); }
    : REAL_SET_ITEM;
  sim?.note(`localStorage.setItem ${storeThrows ? 'now throws' : 'works again'}`, storeThrows);
  panel?.update();
}

const SIM_RPC = 'https://sim-rpc.paidthrough.invalid/';
const DAY = 86400;
const ACCOUNTS = {
  biller: '0xb1110c5e7f3a9d2b4c6e8f0a1b3c5d7e9f0a2b11',
  payer: '0xfa11e5d4c3b2a1908f7e6d5c4b3a29180f7e6a11',
  other: '0x5a5a1234567890abcdef1234567890abcdef5a5a',
  smart: '0x77020a0b0c0d0e0f101112131415161718197702', // has code: EIP-7702 delegate without ERC-1271
};
const FILLER = '0xf111e70000000000000000000000000000f111e7'; // another biller on the same contract (?seed=messy)
const low = (a) => (a || '').toLowerCase();
const hex = (n) => '0x' + BigInt(n).toString(16);
const delay = (ms) => new Promise((ok) => setTimeout(ok, ms));
const rand = (n) => '0x' + Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => b.toString(16).padStart(2, '0')).join('');
const usd = (u) => (Number(u) / 1e6).toFixed(2);

let sim = null;
let wallet = null;

class WalletError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

class MockWallet {
  constructor() {
    this.chain = 1;            // starts on a non-Arc chain so switch -> 4902 -> add -> switch runs
    this.known = new Set([1]);
    this.who = 'payer';
    this.connected = false;
    this.listeners = {};
    this.rejectNext = false;
    this.noTyped = false;
    this.isMetaMask = false;
  }
  on(ev, fn) { (this.listeners[ev] ||= []).push(fn); }
  removeListener(ev, fn) { this.listeners[ev] = (this.listeners[ev] || []).filter((f) => f !== fn); }
  emit(ev, arg) { for (const f of this.listeners[ev] || []) f(arg); }
  address() { return ACCOUNTS[this.who]; }
  setWho(who) {
    this.who = who;
    if (this.connected) this.emit('accountsChanged', [this.address()]);
    panel?.update();
  }
  check(cond, msg) {
    if (cond) return;
    sim.note('HARNESS CHECK FAILED: ' + msg, true);
    throw new WalletError(-32602, 'harness check failed: ' + msg);
  }
  prompt(method) {
    if (!this.rejectNext) return;
    this.rejectNext = false;
    panel?.update();
    sim.note(`wallet: user rejected ${method}`, true);
    throw new WalletError(4001, 'User rejected the request.');
  }

  async request({ method, params = [] }) {
    await delay(80);
    switch (method) {
      case 'eth_requestAccounts':
        this.prompt(method);
        this.connected = true;
        sim.note(`wallet: connected ${this.who}`);
        panel?.update();
        return [this.address()];
      case 'eth_accounts':
        return this.connected ? [this.address()] : [];
      case 'eth_chainId':
        return hex(this.chain);
      case 'wallet_switchEthereumChain': {
        this.prompt(method);
        const id = Number(BigInt(params[0].chainId));
        if (!this.known.has(id)) throw new WalletError(4902, `Unrecognized chain ID "${params[0].chainId}". Try adding the chain using wallet_addEthereumChain first.`);
        this.chain = id;
        sim.note(`wallet: switched to chain ${id}`);
        this.emit('chainChanged', hex(id));
        panel?.update();
        return null;
      }
      case 'wallet_addEthereumChain': {
        this.prompt(method);
        const p = params[0] || {};
        this.check(Number(BigInt(p.chainId)) === sim.chainId, 'addEthereumChain chainId ' + p.chainId);
        this.check(p.nativeCurrency && p.nativeCurrency.symbol === 'USDC' && p.nativeCurrency.decimals === 18, 'nativeCurrency must be USDC with 18 decimals');
        this.check(Array.isArray(p.rpcUrls) && p.rpcUrls.length > 0 && Array.isArray(p.blockExplorerUrls), 'rpcUrls / blockExplorerUrls');
        this.known.add(sim.chainId);
        this.chain = sim.chainId;
        sim.note(`wallet: added and switched to ${p.chainName}`);
        this.emit('chainChanged', hex(sim.chainId));
        panel?.update();
        return null;
      }
      case 'eth_signTypedData_v4': {
        if (this.noTyped) throw new WalletError(-32601, 'The method "eth_signTypedData_v4" does not exist / is not available.');
        this.prompt(method);
        this.check(low(params[0]) === low(this.address()), 'signer param is not the wallet account');
        this.check(typeof params[1] === 'string', 'typed data must be a JSON string');
        this.check(this.chain === sim.chainId, 'signing while the wallet is not on Arc');
        const td = JSON.parse(params[1]);
        const d = td.domain || {};
        this.check(d.name === 'USDC' && d.version === '2' && Number(d.chainId) === sim.chainId && low(d.verifyingContract) === sim.usdc, 'EIP-712 domain ' + JSON.stringify(d));
        this.check(td.primaryType === 'ReceiveWithAuthorization', 'primaryType ' + td.primaryType);
        const shape = (arr) => JSON.stringify((arr || []).map((x) => [x.name, x.type]));
        this.check(shape(td.types?.EIP712Domain) === JSON.stringify([['name', 'string'], ['version', 'string'], ['chainId', 'uint256'], ['verifyingContract', 'address']]), 'EIP712Domain type');
        this.check(shape(td.types?.ReceiveWithAuthorization) === JSON.stringify([['from', 'address'], ['to', 'address'], ['value', 'uint256'], ['validAfter', 'uint256'], ['validBefore', 'uint256'], ['nonce', 'bytes32']]), 'ReceiveWithAuthorization type');
        const m = td.message || {};
        this.check(low(m.from) === low(this.address()), 'message.from');
        this.check([m.value, m.validAfter, m.validBefore].every((x) => typeof x === 'string' && /^\d+$/.test(x)), 'uint256 values must be decimal strings');
        this.check(/^0x[0-9a-fA-F]{64}$/.test(m.nonce || ''), 'nonce must be 32-byte hex');
        this.check(BigInt(m.validBefore) <= BigInt(sim.now() + 3600 + 5), 'validBefore more than an hour ahead');
        const r = rand(32);
        const s = rand(32);
        const vRaw = [0, 1, 27, 28][Math.floor(Math.random() * 4)]; // wallets differ; the app must normalise
        sim.issued.set(r, { from: low(m.from), to: low(m.to), value: BigInt(m.value), validAfter: BigInt(m.validAfter), validBefore: BigInt(m.validBefore), nonce: low(m.nonce), v: vRaw < 27 ? vRaw + 27 : vRaw, s });
        sim.note(`wallet: signed ReceiveWithAuthorization value ${usd(m.value)} (v=${vRaw})`);
        return r + s.slice(2) + vRaw.toString(16).padStart(2, '0');
      }
      case 'eth_sendTransaction': {
        this.prompt(method);
        const tx = params[0] || {};
        this.check(low(tx.from) === low(this.address()), 'tx.from is not the wallet account');
        this.check(this.chain === sim.chainId, 'sending while the wallet is on chain ' + this.chain);
        this.check(!tx.value || BigInt(tx.value) === 0n, 'PaidThrough calls are non-payable: value must be 0');
        this.check(tx.maxFeePerGas != null && tx.maxPriorityFeePerGas != null && tx.gas != null, 'gas, maxFeePerGas and maxPriorityFeePerGas should be set');
        return sim.send(tx);
      }
      default:
        try { return sim.rpc(method, params); } catch (e) { throw new WalletError(e.code ?? -32603, e.message); }
    }
  }
}

function installFetch() {
  const real = window.fetch.bind(window);
  window.fetch = async (url, opts) => {
    if (String(url) !== SIM_RPC) return real(url, opts);
    await delay(35);
    if (sim.rpcDown) throw new TypeError('Failed to fetch');
    const body = JSON.parse(opts.body);
    const one = (req) => {
      try {
        return { jsonrpc: '2.0', id: req.id, result: sim.rpc(req.method, req.params) };
      } catch (e) {
        const err = { code: e instanceof RpcFail ? e.code : -32603, message: e.message };
        if (e.data) err.data = e.data;
        return { jsonrpc: '2.0', id: req.id, error: err };
      }
    };
    const out = Array.isArray(body) ? body.map(one) : one(body);
    return new Response(JSON.stringify(out), { status: 200, headers: { 'content-type': 'application/json' } });
  };
}

async function fingerprint(salt, text) {
  const d = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode('paidthrough:v1:' + salt + ':' + text)));
  return '0x' + [...d].map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function seed() {
  if (MESSY) return seedMessy();
  const now = sim.now();
  const P = ACCOUNTS.payer;
  const B = ACCOUNTS.biller;
  const rows = [
    { text: 'Lab fee, Grade 5', amount: 30_000_000n, status: 1, payBy: now + 10 * DAY, claimWindow: 7 * DAY },
    { text: 'Term 2 tuition, Grade 5', amount: 120_000_000n, status: 2, payBy: now + 8 * DAY, claimWindow: 7 * DAY, payer: P, claimBy: now + 5 * DAY },
    { text: 'Check-up, October', amount: 45_000_000n, status: 3, payBy: now + 2 * DAY, claimWindow: 3 * DAY, payer: P, claimBy: now + 2 * DAY },
    { text: 'X-ray follow-up', amount: 80_000_000n, status: 2, payBy: now + DAY, claimWindow: DAY, payer: P, claimBy: now - 3600 },
    { text: 'Dental cleaning', amount: 60_000_000n, status: 4, payBy: now - DAY, claimWindow: DAY, payer: P, claimBy: now - 2 * DAY },
    { text: 'Hospital deposit', amount: 200_000_000n, status: 1, payBy: now + 20 * DAY, claimWindow: 14 * DAY, allowedPayer: ACCOUNTS.other },
  ];
  for (const [i, r] of rows.entries()) {
    const salt = SEED_SALT(i + 1);
    const ref = await fingerprint(salt, r.text);
    const id = sim.seed({ payee: B, amount: r.amount, payer: r.payer, payBy: r.payBy, claimWindow: r.claimWindow, allowedPayer: r.allowedPayer, claimBy: r.claimBy || 0, status: r.status, ref });
    // Same key format app.js uses for references saved in the biller's browser.
    try { localStorage.setItem(`pt.ref.${sim.chainId}.${sim.contract}.${id}`, JSON.stringify({ text: r.text, salt })); } catch { /* fine */ }
    seeded.push({ id, text: r.text, salt, status: r.status });
  }
  tamper = seeded[1];
  sim.mint(P, 1000_000_000n);
  sim.mint(B, 50_000_000n);
  sim.mint(ACCOUNTS.other, 300_000_000n);
  sim.mint(ACCOUNTS.smart, 500_000_000n);
  sim.codeAt.add(ACCOUNTS.smart);
}
const seeded = []; // bills the panel links to (with reference + salt)
let tamper = null; // the seeded bill behind the panel's "tampered" link

// Long-tail fixture (?seed=messy). Ids 1-999: another biller's cancelled bills (never on this desk).
// Ids 1000-1059: the biller's 60 bills. 1000 = open, 10,000.00 USDC, 140-character reference;
// 1001 = open, 0.000001 USDC; 1002 = paid, 10,000.00 USDC, 140-character reference; 1003 = open, reference
// not saved in this browser (restore path). 1004-1059 cycle through every phase, the amount edges
// (0.000001, 10,000.00, 9,999.999999, ...) and the reference shapes below; every 5th has no saved reference.
const MAX_REF = '제2학기 수업료·급식비·교재비 (5학년 2반·오전) / Matrikula, pagkain at aklat — ika-2 semestre, Baitang 5, Seksiyon Sampaguita; bayaran bago ang Okt. 31, 2026!';
async function seedMessy() {
  const now = sim.now();
  const P = ACCOUNTS.payer;
  const B = ACCOUNTS.biller;
  if (MAX_REF.length !== 140) sim.note(`messy seed: reference is ${MAX_REF.length} characters, not 140`, true);
  const fillerRef = await fingerprint(SEED_SALT(0), 'other biller');
  while (sim.bills.length < 999) {
    sim.seed({ payee: FILLER, amount: 1_000_000n * BigInt(1 + (sim.bills.length % 50)), payBy: now - DAY, claimWindow: DAY, claimBy: 0, status: 6, ref: fillerRef });
  }
  const REFS = [
    'Matrikula, ika-2 semestre',
    '병원비 — 외래 진료 (10월)',
    MAX_REF,
    'Bayad sa ospital: konsulta, X-ray at gamot (Oktubre 2026)',
    '급식비',
    'INV-2026-10-000417/SANISIDRO-ELEM/GRADE5-SECTION2/TUITION-AND-BOOKS',
    'Lab fee / 실험비 / Bayad sa laboratoryo, Baitang 5–6 (Seksiyon Rosal at Sampaguita)',
  ];
  const AMOUNTS = [1n, 10_000_000_000n, 9_999_999_999n, 999_999n, 1_234_567_891n, 300_000n];
  const PHASES = ['open', 'paid', 'collected', 'returned', 'declined', 'cancelled', 'expired', 'refundDue'];
  const rows = [
    { text: MAX_REF, amount: 10_000_000_000n, phase: 'open' },
    { text: 'Bayad sa pagsusulit / 시험 응시료', amount: 1n, phase: 'open' },
    { text: MAX_REF, amount: 10_000_000_000n, phase: 'paid', claimIn: 6 * DAY },
    { text: '수학여행비 / Bayad sa field trip', amount: 2_500_500_000n, phase: 'open', unsaved: true },
  ];
  for (let i = 0; rows.length < 60; i++) {
    rows.push({ text: REFS[i % REFS.length], amount: AMOUNTS[i % AMOUNTS.length], phase: PHASES[i % PHASES.length], claimIn: (3 + (i % 9) * 7) * 3600, unsaved: i % 5 === 4 });
  }
  const fields = (r) => {
    const open = { payBy: now + 9 * DAY, claimWindow: 7 * DAY, claimBy: 0, status: 1 };
    const paid = { ...open, payer: P };
    switch (r.phase) {
      case 'expired': return { ...open, payBy: now - 2 * DAY };
      case 'paid': return { ...paid, status: 2, claimBy: now + r.claimIn };
      case 'refundDue': return { ...paid, status: 2, claimBy: now - r.claimIn };
      case 'collected': return { ...paid, status: 3, claimBy: now + DAY };
      case 'returned': return { ...paid, status: 4, claimBy: now - 2 * DAY };
      case 'declined': return { ...paid, status: 5, claimBy: now + 2 * DAY };
      case 'cancelled': return { ...open, status: 6 };
      default: return open;
    }
  };
  for (const [i, r] of rows.entries()) {
    const id = sim.bills.length + 1;
    const salt = SEED_SALT(id);
    const ref = await fingerprint(salt, r.text);
    sim.seed({ payee: B, amount: r.amount, ...fields(r), ref });
    if (!r.unsaved) { try { localStorage.setItem(`pt.ref.${sim.chainId}.${sim.contract}.${id}`, JSON.stringify({ text: r.text, salt })); } catch { /* fine */ } }
    if (i < 4) seeded.push({ id, text: r.text, salt, status: fields(r).status });
  }
  tamper = seeded[2];
  sim.mint(P, 20_000_000_000n); // enough for the 10,000.00 bill plus fees
  sim.mint(B, 50_000_000n);
  sim.mint(ACCOUNTS.other, 300_000_000n);
  sim.mint(ACCOUNTS.smart, 500_000_000n);
  sim.codeAt.add(ACCOUNTS.smart);
}

// Each load deploys a fresh random sim contract, and refs/scan caches are keyed by contract, so stale keys
// pile up. Drop keys of sim contracts this harness created earlier (keeping the last two, for a second open
// tab). Only addresses from this list are touched: the sim chain id equals mainnet's, so a prefix is not enough.
function forgetOldSimContracts(current) {
  try {
    const KEY = 'pt.dev.simContracts';
    const past = JSON.parse(localStorage.getItem(KEY) || '[]').filter((c) => /^0x[0-9a-f]{40}$/.test(c));
    const keep = past.slice(-2);
    const drop = new Set(past.slice(0, -2));
    if (drop.size) {
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k && k.startsWith('pt.') && [...drop].some((c) => k.includes('.' + c))) localStorage.removeItem(k);
      }
    }
    localStorage.setItem(KEY, JSON.stringify([...keep, current]));
  } catch { /* storage blocked: nothing piles up either */ }
}

// A second wallet pays bill `id` straight into the simulated chain (allowance + pay), as if from another
// device: the page on screen learns of it only through its own reads. Used for the F1 reproductions.
function payAs(who, id) {
  const from = ACCOUNTS[who];
  const b = sim.bills[id - 1];
  if (!from || !b) return null;
  sim.allow.set(low(from) + ':' + sim.contract, b.amount);
  return sim.send({
    from, to: sim.contract, data: SEL.pay + BigInt(id).toString(16).padStart(64, '0'),
    gas: '0x30000', maxFeePerGas: '0x' + (40e9).toString(16), maxPriorityFeePerGas: '0x1',
  });
}

/* ---------------- control panel */

let panel = null;
function buildPanel() {
  const el = document.createElement('aside');
  el.className = 'dev-panel';
  el.setAttribute('aria-label', 'Dev harness controls');
  document.body.append(el);
  const linkFor = (kind, s) => `#/${kind}/${s.id}?` + new URLSearchParams({ r: s.text, s: s.salt });
  const update = () => {
    if (!sim) {
      el.innerHTML = '<p class="dev-h">Dev harness: preview mode (no contract)</p><p>Config untouched: this is what index.html shows before deploy.</p>';
      return;
    }
    const time = new Date(sim.now() * 1000).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
    const bal = (a) => usd(sim.balance(a));
    el.innerHTML = `
      <div class="dev-row dev-top"><p class="dev-h">Dev harness &middot; simulated Arc</p><button data-act="collapse" type="button">hide</button></div>
      <p class="dev-sub">In-memory chain. Nothing here is real.</p>
      <fieldset><legend>Wallet account ${wallet ? (wallet.connected ? '(connected)' : '(not connected)') : '(no wallet)'}</legend>
        ${wallet ? ['biller', 'payer', 'other', 'smart'].map((w) => `<label><input type="radio" name="who" value="${w}" ${wallet.who === w ? 'checked' : ''}> ${w}</label>`).join(' ') : 'window.ethereum absent'}
      </fieldset>
      ${wallet ? `<p>Wallet chain: <b>${wallet.chain === sim.chainId ? 'Arc ' + sim.chainId : wallet.chain}</b> <button data-act="wrongchain" type="button">wrong chain</button></p>` : ''}
      <p>Chain time: <b>${time}</b>${sim.offset ? ` (+${(sim.offset / 3600).toFixed(0)} h)` : ''}</p>
      <div class="dev-row"><button data-act="t1h" type="button">+1 h</button><button data-act="t1d" type="button">+1 day</button><button data-act="t7d" type="button">+7 days</button></div>
      <div class="dev-row"><button data-act="otherpays" type="button">other wallet pays the bill on screen</button></div>
      ${wallet ? `<label><input type="checkbox" data-flag="reject" ${wallet.rejectNext ? 'checked' : ''}> reject next wallet prompt</label>
      <label><input type="checkbox" data-flag="notyped" ${wallet.noTyped ? 'checked' : ''}> wallet can't sign typed data</label>` : ''}
      <label><input type="checkbox" data-flag="rpcdown" ${sim.rpcDown ? 'checked' : ''}> RPC down</label>
      <label><input type="checkbox" data-flag="refuse" ${sim.refuseCalls ? 'checked' : ''}> RPC refuses eth_call (-32005)</label>
      <label><input type="checkbox" data-flag="nostore" ${storeThrows ? 'checked' : ''}> localStorage.setItem throws</label>
      <label><input type="checkbox" data-flag="blockpayee" ${sim.blocked.has(ACCOUNTS.biller) ? 'checked' : ''}> token blocks biller</label>
      <p class="dev-bal">USDC: payer ${bal(ACCOUNTS.payer)} &middot; biller ${bal(ACCOUNTS.biller)} &middot; other ${bal(ACCOUNTS.other)} &middot; smart ${bal(ACCOUNTS.smart)} &middot; contract ${bal(sim.contract)}</p>
      <p class="dev-links">${MESSY ? `<b>seed: messy</b> (${sim.bills.length} bills) ` : ''}<a href="#/desk">desk</a> ${seeded.map((s) => `<a href="${linkFor('pay', s)}">pay ${s.id}</a> <a href="${linkFor('bill', s)}">bill ${s.id}</a>`).join(' ')} ${tamper ? `<a href="#/bill/${tamper.id}?r=tampered&s=${tamper.salt}">bill ${tamper.id} tampered</a>` : ''} <a href="#/bill/${MESSY ? 9999 : 99}">bill ${MESSY ? 9999 : 99}</a></p>
      <ol class="dev-log">${sim.events.slice(0, 10).map((e) => `<li class="${e.bad ? 'bad' : ''}">${e.at.toLocaleTimeString('en-GB')} ${escapeHtml(e.msg)}</li>`).join('')}</ol>`;
  };
  el.addEventListener('click', (e) => {
    const act = e.target.dataset?.act;
    if (!act) return;
    if (act === 'collapse') { el.classList.toggle('dev-panel--min'); return; }
    if (act === 'wrongchain') { wallet.chain = 1; wallet.known = new Set([1]); wallet.emit('chainChanged', '0x1'); sim.note('wallet: moved to chain 1, Arc forgotten'); }
    if (act === 't1h') sim.travel(3600);
    if (act === 't1d') sim.travel(DAY);
    if (act === 't7d') sim.travel(7 * DAY);
    if (act === 'otherpays') { const m = location.hash.match(/#\/(?:pay|bill)\/(\d+)/); if (m) payAs('other', Number(m[1])); }
    update();
  });
  el.addEventListener('change', (e) => {
    const t = e.target;
    if (t.name === 'who') wallet.setWho(t.value);
    if (t.dataset.flag === 'reject') wallet.rejectNext = t.checked;
    if (t.dataset.flag === 'notyped') wallet.noTyped = t.checked;
    if (t.dataset.flag === 'rpcdown') sim.rpcDown = t.checked;
    if (t.dataset.flag === 'refuse') { sim.refuseCalls = t.checked; sim.note(`RPC ${t.checked ? 'refuses' : 'answers'} eth_call`, t.checked); }
    if (t.dataset.flag === 'nostore') setStoreThrows(t.checked);
    if (t.dataset.flag === 'blockpayee') { if (t.checked) sim.blocked.add(ACCOUNTS.biller); else sim.blocked.delete(ACCOUNTS.biller); }
    update();
  });
  document.addEventListener('keydown', (e) => {
    if (e.altKey && e.shiftKey && (e.key === 'D' || e.key === 'd')) el.hidden = !el.hidden;
  });
  return { el, update };
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ---------------- boot */

function checkStrings() {
  const base = Object.keys(STRINGS.en);
  for (const [code, table] of Object.entries(STRINGS)) {
    const missing = base.filter((k) => !(k in table));
    if (missing.length) sim?.note(`i18n: ${code} is missing ${missing.length} keys: ${missing.slice(0, 4).join(', ')}`, true);
  }
}

async function boot() {
  if (!PREVIEW) {
    const contract = rand(20);
    sim = new Sim({ chainId: 5042, contract, usdc: CONFIG.networks.mainnet.usdc, maxAmount: CONFIG.maxAmountUnits });
    CONFIG.networks.sim = {
      label: 'Arc (simulated)',
      chainId: sim.chainId,
      rpc: SIM_RPC,
      explorer: 'https://explorer.arc.io',
      usdc: CONFIG.networks.mainnet.usdc,
      paidThrough: contract,
      deployBlock: sim.deployBlock,
    };
    CONFIG.network = 'sim';
    installFetch();
    forgetOldSimContracts(sim.contract);
    await seed();
    // After seeding, so the seeded references are in storage and only the page's own writes fail.
    if (q.get('store') === 'throw') setStoreThrows(true);
    if (q.get('refuse') === '1') { sim.refuseCalls = true; sim.note('RPC refuses eth_call', true); }
    if (!NO_WALLET) {
      wallet = new MockWallet();
      if (CONNECT && ACCOUNTS[CONNECT]) { wallet.who = CONNECT; wallet.connected = true; }
      window.ethereum = wallet;
    }
    window.__sim = sim; // for poking from the console
    window.__payAs = payAs;
    window.__wallet = wallet;
    window.__storeThrows = setStoreThrows;
  }
  if (PANEL) {
    panel = buildPanel();
    if (sim) sim.onChange = () => panel.update();
    panel.update();
    setInterval(() => panel.update(), 5000);
  }
  checkStrings();
  await import('../app.js');
}

boot();
