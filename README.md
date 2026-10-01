# PaidThrough

**Pay a school or clinic bill abroad in USDC on Arc. The biller collects it, or it comes back to you.**

A worker in Seoul pays her son's tuition in Cebu. Today the money goes to a relative or through a bank transfer, and she finds out weeks later whether it reached the school. With PaidThrough the school issues a bill on Arc, she pays that bill from her phone with one signature, and her family can open a link and see it settle in about a second: *waiting for the school*, then *collected*. If the school never collects it by the deadline, the money goes back to her wallet automatically. Nobody in the middle can hold it, redirect it or take a cut.

> Status: private build, not yet deployed. Mainnet address and live link will be added here. <!-- TODO(deploy) -->

## The problem

- Migrant workers in Korea sent **USD 3.557 billion** home through banks in 2025 (Bank of Korea figures reported by Maeil Business, 2026-09-04). The Philippines alone received **USD 35.63 billion** in cash remittances in 2025 (Bangko Sentral ng Pilipinas, preliminary).
- Sending USD 200 from Korea cost **5.59%** on average; USD 500 cost **3.06%** (World Bank, Remittance Prices Worldwide, Issue 51, Q3 2024). The global average in the same issue was 6.62%, against a G20 target of 3% by 2030 (FSB, 2021).
- Much of that money is sent for one bill: tuition, a hospital invoice, an installment. It passes through relatives, bank hours and fixed fees, and the sender cannot see whether it reached the bill.

Where PaidThrough is **not** better: above about USD 500, a Korean bank transfer is already cheap, and cashing USDC in or out still happens at an exchange (Upbit supports USDC on Arc since 2026-09-16, withdrawal fee 0.01 USDC). The value here is that the money is tied to one bill and comes back if that bill is not collected. Sources for every figure: [`docs/SOURCES.md`](docs/SOURCES.md).

## How it works

```
biller ──issue(amount, reference fingerprint, pay-by, collection window)──► bill #12 (Open)
payer  ──pay with one signature (USDC EIP-3009)───────────────────────────► bill #12 (Paid, money held by the contract)
biller ──collect before the deadline──► money to biller     (Collected)
       ──or decline──────────────────► money back to payer (Declined)
anyone ──after the deadline──────────► money back to payer (Refunded)
```

- **The biller issues the bill**, so the amount and reference are the school's, and collecting is the school's acknowledgement.
- **One signature to pay.** Arc's USDC supports EIP-3009. The signature is bound to that one bill, so it cannot pay any other bill, and no token approval is left behind. Gas is also USDC, so the payer needs one balance.
- **Automatic refund.** After the collection deadline, anyone can trigger the refund, and it can only pay the original payer. A small keeper does it for every overdue bill.
- **Private references.** Everything on Arc is public, so the chain stores only a fingerprint of the reference; the readable text travels in the link's `#fragment`, which browsers never send to a server.
- **No owner, no fee, no admin.** Nobody can pause, upgrade, redirect or withdraw. A per-bill cap (10,000 USDC) limits the damage from an unknown bug.

Details and the threat model: [`docs/DESIGN.md`](docs/DESIGN.md). Interface of record: [`SPEC.md`](SPEC.md).

## Why Arc

Arc's own [Request for Builders](https://www.arc.io/blog/the-unfinished-business-of-finance-machine-commerce-and-global-money) asks for "local-market financial platforms" built "for one specific country, corridor, or community". PaidThrough is one corridor (Korea → Philippines first) and one job (a family bill).

What Arc changes for this job:
- **One balance.** Gas is paid in USDC, so a worker who bought USDC on an exchange can pay a bill with nothing else in the wallet. On other chains the same flow needs a second token or a paymaster.
- **Final in under a second.** Arc's finality is deterministic, so the family page can say *paid* while the payer is still on the phone, with no "wait for confirmations".
- **Both ends already connect to Arc.** Upbit in Korea supports USDC on Arc since 2026-09-16. Arc's launch post lists Coins.ph and PDAX in the Philippines among the exchanges live on Arc (we have not tested withdrawals at each).
- **Arc's protocol-level USDC blocklist** is handled and rehearsed on the real node (see below).

One-signature payment uses USDC's EIP-3009, which Circle's USDC also has on other chains; on Arc it means the payer signs once and pays the network fee from the same USDC.

## First users

- **Payer:** a Filipino worker in Korea who already sends money home for a known bill. Buys USDC on Upbit, withdraws it on Arc (fee 0.01 USDC), pays from a phone.
- **Biller:** a school, tutoring centre or clinic that will publish one Arc address. It needs no integration: it opens the biller desk, issues a bill, sends the link, and collects. To turn USDC into pesos it uses an exchange it can already access.
- **First proof:** one real bill, end to end on mainnet, with a biller who agreed to take part, plus one bill left uncollected to show the automatic refund. Both transactions will be linked here.
- **Hardest part, stated plainly:** getting billers to hold an Arc address. That is the work the next steps are for.

## Next steps, and what the grant buys

1. **Run it in public.** Mainnet deploy, the keeper on an always-on server, the page on a public URL. Keeper gas for a year at today's fee (0.0013 USDC per refund) is a few USDC.
2. **One pilot biller.** Onboard one biller in the Korea → Philippines corridor, publish the transactions, and write a one-page biller guide in English and Filipino.
3. **Verified biller addresses.** Let a biller publish its address at a web address it controls (e.g. `/.well-known/paidthrough.json` on the school's domain); the bill page then shows "address published by <domain>". This closes the look-alike-bill risk described under Limits.
4. **Native review** of the Filipino copy (currently a machine draft) and the biller guide.
5. **Later:** installments and multi-payer bills, which need contract changes and a new deployment.

Proposed use of 500 USDC: about 50 for gas and test bills, about 150 for paid native-speaker review and the biller guide, about 300 as a public bug bounty on the contract before amounts grow.

## How we know it works

**Rehearsed on the real Arc mainnet node, before spending anything.** `scripts/rehearse_mainnet.py` runs every path as `eth_call` / `eth_estimateGas` against `rpc.mainnet.arc.io`, with state overrides standing in for the deployed contract and the balances. The money moves through Arc's real USDC and its native-transfer and blocklist precompiles. All 37 steps matched their expected outcome (`status/rehearsal-2026-10-01.md`, block 23,641,690; CI job `arc-rehearsal` reruns it):

| Step | Gas (`eth_estimateGas`) | Cost at the 20 gwei base fee |
|---|---|---|
| deploy | 1,147,368 | 0.0229 USDC |
| issue a bill | 122,021 – 139,189 | 0.0024 – 0.0028 USDC |
| pay with one signature | 111,850 | 0.0022 USDC, one transaction |
| approve + pay | 56,253 + 74,601 | 0.0026 USDC, two transactions |
| collect | 59,722 | 0.0012 USDC |
| decline | 66,384 | 0.0013 USDC |
| refund (anyone, at the deadline) | 66,463 | 0.0013 USDC |

The negative steps include a signature for one bill replayed on another, the same signature sent straight to USDC or redirected, collecting one second late, refunding one second early, and payouts to an address that is really blocklisted on Arc mainnet. Each was refused with the exact expected error.

**Tests in CI on every push** ([`ci.yml`](.github/workflows/ci.yml)): 138 contract tests: 65 unit, 32 signature, 26 blocklist, 11 fuzz tests at 1,000 runs each, and 4 invariants over 256 runs × 64 calls with every revert treated as a failure. A fork of Arc mainnet also runs the flows against the real USDC contract code. The keeper has 68 tests.

**Reviewed.** Two independent adversarial reviews (security; spec and test quality) found no way to lose or redirect funds in the contract. They found a keeper denial-of-service through log spam, a phishing risk in how the page showed the biller, and eight test gaps. All are fixed.

## What is in this repository

| Path | What |
|---|---|
| `contracts/` | `PaidThrough.sol` (5,009-byte runtime, no libraries) and its Foundry tests |
| `keeper/` | Python refund keeper: runs every 5 minutes, can only ever call `refund` (which pays the original payer), largest bills first; simulates each refund before sending, stops on a stuck transaction, alerts on failure; survives log spam (68 tests) |
| `web/` | Static page: bill status for family, pay, biller desk; English, Filipino (draft translation), Korean |
| `scripts/probe_usdc.py` | Read-only check of the Arc USDC interface this relies on |
| `scripts/rehearse_mainnet.py` | The real-node rehearsal above |

## Run it

```
python scripts/probe_usdc.py
python -m unittest discover -s keeper/tests
python keeper/paidthrough_keeper.py scan --network testnet
python -m http.server 8761 --directory web
```

Contracts build and test in GitHub Actions (`.github/workflows/ci.yml`).

## Limits

Unaudited. A blocklisted payee can neither collect nor decline (the payer is refunded after the deadline); a blocklisted payer's refund waits until the block is lifted. A payer whose address has code (an EIP-7702 delegation or a smart account) can sign only if that wallet implements ERC-1271; otherwise the page switches to approve + pay. Tokens sent to the contract outside `pay` are stuck. The contract proves the biller's address collected the money, not that the school credited the student, and the page cannot yet prove an address belongs to a school (next step 3). If the keeper is down, any refund can still be triggered from the bill page by anyone. No partial payments or installments yet.

Independent project. Not affiliated with Circle or Arc.
