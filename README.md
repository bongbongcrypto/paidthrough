# PaidThrough

**Pay a school or clinic bill abroad in USDC on Arc. The biller collects it, or it comes back to you.**

A worker in Seoul pays her son's tuition in Cebu. Today the money goes to a relative or through a bank transfer, and she finds out weeks later whether it reached the school. With PaidThrough the school issues a bill on Arc, and she pays it from her phone by signing for that bill and confirming one transaction, with no separate approval step. Her family follows the bill from a link: *waiting for the school*, then *collected*. Arc finalizes the payment in under a second, and an open family page re-reads the chain every 10 seconds. If the school never collects by the deadline, the money goes back to her wallet. A keeper sends that refund automatically for bills of 0.05 USDC or more, and anyone can refund a smaller bill from its page. PaidThrough has no owner or admin who could hold the money, redirect it or take a cut, though the USDC issuer can still freeze a payout through its blocklist (see [Limits](#limits)).

> **Live on Arc mainnet.** Contract [`0x05cf14cB82660942c272EaaD488C745490CB82aB`](https://explorer.arc.io/address/0x05cf14cB82660942c272EaaD488C745490CB82aB), deployed 2026-10-01 17:44 UTC in transaction [`0xb4e8a046729ae3851ca0d32c81dc2f4a432f4a946cd81ce788dfc7d971f01e2a`](https://explorer.arc.io/tx/0xb4e8a046729ae3851ca0d32c81dc2f4a432f4a946cd81ce788dfc7d971f01e2a) (block 23,746,519). The deployed runtime code was checked byte for byte against the CI build. Source verified on Sourcify with an exact match of both creation and runtime code ([lookup](https://sourcify.dev/#/lookup/0x05cf14cB82660942c272EaaD488C745490CB82aB)).
>
> **Live page:** https://bongbongcrypto.github.io/paidthrough/ (reads the chain from your browser; no wallet needed to view a bill). Its first screen shows an example bill, labelled as one. The three real bills on mainnet open from the family links [below](#real-bills-on-mainnet). All three are already settled (collected, declined, refunded), so none is open for a visitor to pay.

## Real bills on mainnet

Three test bills of 0.50 USDC each ran on mainnet between two test wallets we control: biller `0x7421346433d41fb6fA52317a8f0EEb5275bc2051`, payer `0x6fAc56a942714aEa400f521D4c870dFB12D9d83b`. No outside biller or payer took part. [`scripts/test_bills.py`](scripts/test_bills.py) sent the issue, pay, collect and decline transactions and checked every receipt's USDC transfers and both wallets' balance changes to the wei. The refund of bill 3 was sent by the keeper on its own, from the server, with no one running anything ([server log of that run](status/keeper-journal-2026-10-03.log)). The references stored with the bills read "Test bill A", "Test bill C" and "Test bill B" for bills 1, 2 and 3. Each family link opens the public page, which reads the bill from the chain.

| Bill | What happened | Family link | Transactions |
|---|---|---|---|
| 1 | Paid by signature plus one transaction from the payer (no approval), then collected by the biller | [open](https://bongbongcrypto.github.io/paidthrough/#/bill/1?r=Test+bill+A+%28mainnet+check%29&s=56bdb5623b266a7574f729c162c7c71b) | [issue](https://explorer.arc.io/tx/0xa5ded6e2020b3e98802d14282499e201c8c056cdd604e5e4eab13d5228a9f2ec) · [pay](https://explorer.arc.io/tx/0x30adafa74d41133372092983dde2d6056b02a403e31a54d2816ab598a888a617) · [collect](https://explorer.arc.io/tx/0xe0bc1bb1561a41235cecfd914b06965665e8f4290d58d219762ed597552c197d) |
| 2 | Paid with approve + pay (two transactions), then declined by the biller, so the money went back to the payer | [open](https://bongbongcrypto.github.io/paidthrough/#/bill/2?r=Test+bill+C+%28mainnet+check%29&s=1fa7b834f5db5f967ace6f4a143644fd) | [issue](https://explorer.arc.io/tx/0xecbe57311f5d0d1dcb4e43d0082e8153477d935ad8c13e6b13f38f083462833d) · [approve](https://explorer.arc.io/tx/0x1f725ead2c5e10561b58760e72ae942dd0a02e34aeea0c0938cae8493aebd0c2) · [pay](https://explorer.arc.io/tx/0x0f35bb1bfbcb938d428763d661245a757e4c61f13d192cd7621e3fcf35d9db61) · [decline](https://explorer.arc.io/tx/0x60e648502fa6ed08a12de72456822851285daeed0329364349cf68972f7ccd5b) |
| 3 | Paid the same way as bill 1 and left uncollected. Its collection window was 1 hour; the keeper refunded the payer 116 seconds after it closed | [open](https://bongbongcrypto.github.io/paidthrough/#/bill/3?r=Test+bill+B+%28mainnet+check%29&s=9780bf5cc0e9339cfa89a3e73d416b63) | [issue](https://explorer.arc.io/tx/0x86aefd2a3b89b1d548428eb6508c6174bffda2a2e9ce3a7f59a6e28e969f3f4c) · [pay](https://explorer.arc.io/tx/0xf38d003b2c19a3ee7fdce13c1296f41ff138d20acd51462333d7fc13b1bbc847) · [refund](https://explorer.arc.io/tx/0x8a2a540ac5c90a6e384dc55d7b15a4a39b98be7b2d416fcd7401b93123347932) |

Gas used on mainnet, from the receipts: issue 120,934 – 138,046, pay by signature 106,307, approve 55,438 + pay 69,406, collect 58,905, decline 65,541, refund 65,620. At the 20 gwei base fee each transaction cost between 0.0011 and 0.0028 USDC. The rehearsal's estimates are under [How we know it works](#how-we-know-it-works). The run record, with blocks and costs, is [`status/test-bills-2026-10-03.md`](status/test-bills-2026-10-03.md).

## The problem

- Migrant workers in Korea sent **USD 3.557 billion** home through banks in 2025, as reported by Maeil Business (2026-09-04) citing Bank of Korea data given to a lawmaker. The Philippines alone received **USD 35.63 billion** in cash remittances in 2025 (Bangko Sentral ng Pilipinas, preliminary).
- Sending USD 200 from Korea cost **5.59%** on average; USD 500 cost **3.06%** (World Bank, Remittance Prices Worldwide, Issue 51, Q3 2024). The global average in the same issue was 6.62%, against a G20 target of 3% by 2030 (FSB, 2021).
- Much of that money is sent for one bill: tuition, a hospital invoice, an installment. It passes through relatives, bank hours and fixed fees, and the sender cannot see whether it reached the bill.

Where PaidThrough is **not** better: for larger transfers the bank fee is lower (sending USD 500 from Korea cost 3.06% on average, against 5.59% for USD 200; World Bank RPW Q3 2024), and cashing USDC in or out still happens at an exchange (Upbit supports USDC on Arc since 2026-09-16, withdrawal fee 0.01 USDC as of notice 6579, 2026-09-16). The value here is that the money is tied to one bill and comes back if that bill is not collected. Sources for every figure: [`docs/SOURCES.md`](docs/SOURCES.md).

## How it works

Money the biller does not collect goes back to the payer by default, and anyone can trigger that refund; the chain stores the bill's reference only as a fingerprint.

```
biller ──issue(amount, reference fingerprint, pay-by, collection window)──► bill #12 (Open)
payer  ──sign for this bill (USDC EIP-3009) + one pay transaction─────────► bill #12 (Paid, money held by the contract)
biller ──collect before the deadline──► money to biller     (Collected)
       ──or decline──────────────────► money back to payer (Declined)
anyone ──after the deadline──────────► money back to payer (Refunded)
```

- **The biller issues the bill**, so the amount and reference are the school's, and collecting is the school's acknowledgement.
- **Sign, then pay.** In the browser the payer signs typed data for the bill, then confirms one transaction; there is no approval transaction first. Arc's USDC supports EIP-3009. The signature is bound to that one bill, so it cannot pay any other bill, and no token approval is left behind. Gas is also USDC, so the payer needs one balance.
- **Automatic refund.** After the collection deadline, anyone can trigger the refund, and it can only pay the original payer. A small keeper does it every 5 minutes for each overdue bill of 0.05 USDC or more; a smaller bill is left for anyone to refund from the bill page.
- **Private references.** Everything on Arc is public, so the chain stores only a fingerprint of the reference; the readable text travels in the link's `#fragment`, which browsers never send to a server.
- **No owner or admin, and no fee.** Nobody can pause, upgrade, redirect or withdraw. The USDC issuer's blocklist still applies (see [Limits](#limits)). A per-bill cap (10,000 USDC) limits the damage from an unknown bug.

Details and the threat model: [`docs/DESIGN.md`](docs/DESIGN.md). Interface of record: [`SPEC.md`](SPEC.md).

## Why Arc

Arc's own [Request for Builders](https://www.arc.io/blog/the-unfinished-business-of-finance-machine-commerce-and-global-money) asks for "local-market financial platforms" built "for one specific country, corridor, or community". PaidThrough is one corridor (Korea → Philippines first) and one job (a family bill).

What Arc changes for this job:
- **One balance.** Gas is paid in USDC, so a worker who bought USDC on an exchange can pay a bill with nothing else in the wallet. On other chains the same flow needs a second token or a paymaster.
- **Final in under a second.** Arc's docs describe deterministic finality in under one second, so a payment needs no "wait for confirmations". An open family page re-reads the chain every 10 seconds, so *paid* appears on its next refresh.
- **Both ends already connect to Arc.** Upbit in Korea supports USDC on Arc since 2026-09-16. Arc's launch post (2026-09-16) lists Coins.ph and PDAX in the Philippines among the exchanges live on Arc (we have not tested withdrawals at each).
- **Arc's protocol-level USDC blocklist** is handled and rehearsed on the real node (see below).

Paying by signature uses USDC's EIP-3009, which Circle's USDC also has on other chains; on Arc the payer pays the network fee from the same USDC balance.

## First users

- **Payer:** a Filipino worker in Korea who already sends money home for a known bill. Buys USDC on Upbit, withdraws it on Arc (fee 0.01 USDC as of 2026-09-16), pays from a phone.
- **Biller:** a school, tutoring centre or clinic that will publish one Arc address. It needs no integration: it opens the biller desk, issues a bill, sends the link, and collects. To turn USDC into pesos it uses an exchange it can already access.
- **First proof:** three test bills between two of our own wallets, sent by a script and shown on the public page ([above](#real-bills-on-mainnet)). One was collected, one declined, and one refunded by the keeper after its window. No outside biller has used PaidThrough yet.
- **Hardest part:** getting billers to hold an Arc address. That is the work the next steps are for.

## Next steps, and what the grant buys

1. **Keep it running in public.** The contract, the keeper on an always-on server and the public page are live now. Keeper gas is about 0.0013 USDC per refund at today's fee; the yearly cost depends on how many bills go uncollected. Add uptime alerts for the keeper.
2. **One pilot biller.** Onboard one biller in the Korea → Philippines corridor, publish the transactions, and write a one-page biller guide in English and Filipino.
3. **Verified biller addresses.** Let a biller publish its address at a web address it controls (e.g. `/.well-known/paidthrough.json` on the school's domain); the bill page then shows "address published by <domain>". This closes the look-alike-bill risk described under Limits.
4. **Native review** of the Filipino copy (currently a machine draft) and the biller guide.
5. **Later:** installments and multi-payer bills, which need contract changes and a new deployment.

Proposed use of 500 USDC: about 50 for gas and test bills, about 150 for paid native-speaker review and the biller guide, about 300 as a public bug bounty on the contract before amounts grow.

## How we know it works

**Rehearsed on the real Arc mainnet node, before spending anything.** `scripts/rehearse_mainnet.py` runs every path as `eth_call` / `eth_estimateGas` against `rpc.mainnet.arc.io`, with state overrides standing in for the deployed contract and the balances. The money moves through Arc's real USDC and its native-transfer and blocklist precompiles. Each step names one outcome, "ok" or a revert reason. All 37 steps matched their exact expected outcome. The CI job `arc-rehearsal` reruns the script on every push and ends with a `VERDICT:` line. The gas figures below come from the saved run (`status/rehearsal-2026-10-02.md`, block 23,738,783):

| Step | Gas (`eth_estimateGas`) | Cost at the 20 gwei base fee |
|---|---|---|
| deploy | 1,147,368 (the real deploy used 1,137,176) | 0.0229 USDC (real: 0.0227 USDC) |
| issue a bill | 122,021 – 139,189 | 0.0024 – 0.0028 USDC |
| pay by signature | 111,862 | 0.0022 USDC, one transaction |
| approve + pay | 56,253 + 74,601 | 0.0026 USDC, two transactions |
| collect | 59,722 | 0.0012 USDC |
| decline | 66,384 | 0.0013 USDC |
| refund (anyone, at the deadline) | 66,463 | 0.0013 USDC |

The negative steps include a signature for one bill replayed on another, the same signature sent straight to USDC or redirected, collecting at the deadline, refunding one second early, and payouts to an address that is really blocklisted on Arc mainnet. Each was refused with the exact expected error.

**Tests in CI on every push** ([`ci.yml`](.github/workflows/ci.yml)): 138 contract tests: 65 unit, 32 signature, 26 blocklist, 13 in the fuzz suite (11 fuzz tests at 1,000 runs each + 2 decimal unit tests), and an invariant suite (4 invariants over 256 runs × 64 calls with every revert treated as a failure, plus a handler-liveness test). Forge counts the invariant suite as 2 tests, which is how the total reaches 138. Two more tests run in a separate job on a fork of Arc mainnet, against the real USDC contract code. The keeper has 81 tests, and the scripts job runs 88 tests (18 for the deploy script, 38 for the test-bill runner, the rest for the rehearsal matcher and the fact checker).

**Reviewed.** Two internal adversarial reviews (security; spec and test quality) found no way to lose or redirect funds in the contract. They found three issues outside it (a keeper denial-of-service through log spam, dust bills that could delay real refunds, and a biller display that a look-alike address could fake) and eight test gaps. All are fixed; the list is in [`docs/DESIGN.md`](docs/DESIGN.md#review-findings-and-fixes).

## What is in this repository

| Path | What |
|---|---|
| `contracts/` | `PaidThrough.sol` (5,009-byte runtime, no libraries) and its Foundry tests |
| `keeper/` | Python refund keeper: runs every 5 minutes, can only ever call `refund` (which pays the original payer), largest bills first, skipping bills under 0.05 USDC; simulates each refund before sending, stops on a stuck transaction, alerts on failure; survives log spam |
| `web/` | Static page: bill status for family, pay, biller desk; English, Filipino (draft translation), Korean |
| `scripts/probe_usdc.py` | Read-only check of the Arc USDC interface this relies on |
| `scripts/rehearse_mainnet.py` | The real-node rehearsal above |
| `scripts/test_bills.py` | Sends and checks the mainnet test bills above (dry run by default); its record is `status/test-bills-2026-10-03.md` |

## Run it

Python 3.11+.

```
pip install -r requirements.txt
python scripts/probe_usdc.py
python -m unittest discover -s keeper/tests
python -m unittest discover -s scripts/tests
python keeper/paidthrough_keeper.py scan --network mainnet
python -m http.server 8761 --directory web
```

Contracts build and test in GitHub Actions (`.github/workflows/ci.yml`).

## Limits

Unaudited. A blocklisted payee can neither collect nor decline (the payer is refunded after the deadline); a blocklisted payer's refund waits until the block is lifted. A payer whose address has code (an EIP-7702 delegation or a smart account) can sign only if that wallet implements ERC-1271; otherwise the page switches to approve + pay. Tokens sent to the contract outside `pay` are stuck. The contract proves the biller's address collected the money, not that the school credited the student, and the page cannot yet prove an address belongs to a school (next step 3). Anyone can trigger a due refund from the bill page, which covers a keeper outage and the bills under 0.05 USDC that the keeper skips. No partial payments or installments yet.

Independent project. Not affiliated with Circle or Arc.
