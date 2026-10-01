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

Arc's own [Request for Builders](https://www.arc.io/blog/the-unfinished-business-of-finance-machine-commerce-and-global-money) asks for "local-market financial platforms" built "for one specific country, corridor, or community". PaidThrough is one corridor (Korea → Philippines first) and one job (a family bill). It leans on what Arc has and a generic chain does not: USDC as gas, so the payer holds one balance; sub-second finality, so the family sees *paid* while still on the phone; and Circle's USDC with EIP-3009 on the chain itself.

## What is in this repository

| Path | What |
|---|---|
| `contracts/` | `PaidThrough.sol` and its Foundry tests (run in CI) <!-- TODO(contracts): test counts, gas --> |
| `keeper/` | Python refund keeper: dry-run by default, can only call `refund` <!-- TODO(keeper) --> |
| `web/` | Static page: bill status for family, pay, biller desk; English, Filipino (draft), Korean <!-- TODO(web) --> |
| `scripts/probe_usdc.py` | Read-only check of the Arc USDC interface this relies on |

## Run it

```
python scripts/probe_usdc.py
python -m unittest discover -s keeper/tests
python keeper/paidthrough_keeper.py scan --network testnet
python -m http.server 8761 --directory web
```

Contracts build and test in GitHub Actions (`.github/workflows/ci.yml`).

## Limits

Unaudited. A blocklisted payee cannot collect (the payer is refunded after the deadline); a blocklisted payer's refund waits until the block is lifted. Tokens sent to the contract outside `pay` are stuck. The contract proves the biller's address collected the money, not that the school credited the student. No partial payments or installments yet.

Independent project. Not affiliated with Circle or Arc.
