# PaidThrough refund keeper

A small Python CLI that finds PaidThrough bills whose claim window has passed and calls
`refund(billId)` on them, so the family gets its money back without having to do anything.

## What it can and cannot do

- The only transaction it can build is `refund(billId)` to the configured PaidThrough contract,
  with value 0, on the configured chain id. This is checked when the transaction is built and
  again right before it is signed.
- `refund` pays the bill's original payer. The keeper never receives bill money and cannot send
  money anywhere else; the contract has no owner, fee, sweep or admin function to call.
- The keeper wallet spends only gas (expected around 0.001-0.002 USDC per refund at Arc's 20 gwei
  base fee; not yet measured on a deployed contract). If it runs
  out of gas money it stops and says so; nothing is lost, anyone can still call `refund`.
- It cannot refund early: the contract only allows `refund` at or after `claimBy`, and the keeper
  simulates every call with `eth_call` first. A bill whose simulation reverts (for example a
  payer address blocklisted by USDC) is skipped and reported, then checked again on the next run.
- It skips dust: due bills below `--min-amount` (default 0.05 USDC, about 25x the expected refund gas)
  are listed as "below keeper minimum - payer can refund it themselves" and never simulated or sent.
  Due bills are handled largest amount first (ties: longest-waiting first), so bulk-created tiny bills
  cannot push a real refund behind `--max-sends`.

## Run it (dry-run, no key needed)

Requires Python 3.11+ with `eth_abi`, `eth_account` and `pycryptodome` (or `eth_hash`). No other packages.

```
python keeper/paidthrough_keeper.py scan --network testnet     # every bill, rebuilt from events
python keeper/paidthrough_keeper.py due  --network testnet     # Paid and claimBy <= latest block time
python keeper/paidthrough_keeper.py bill 7 --network testnet   # getBill + event history for one bill
python keeper/paidthrough_keeper.py run  --network testnet     # simulate refunds, print planned tx + fee
```

Until the contract is deployed, `keeper/deployments.json` is `{"mainnet": null, "testnet": null}` and
every command prints "no deployment configured" and exits 0. After deploy, fill in
`{"address": "0x...", "fromBlock": <deploy block>}` for that network, or pass
`--contract 0x... --from-block N`.

Other options: `--rpc URL`, `--state-dir DIR` (scan cache, default `~/.paidthrough-keeper`),
`--no-cache` (re-read all logs), and for `run`: `--from-address` (dry-run simulation sender),
`--max-sends 20`, `--min-amount 0.05`, `--max-fee-gwei 500`, `--receipt-timeout 90`.

Exit codes: 0 ok (skipped bills included), 1 error or unknown chain state, 2 refused (config, key, chain id).

## How the server would run it

Templates only, nothing is installed: `keeper/systemd/paidthrough-keeper.service` and `.timer`
run `run --send --notify --network mainnet` every 5 minutes with `MemoryMax=200M`.

- Key: a file outside the repo, `~/.paidthrough-keeper.env` (or `$PAIDTHROUGH_KEEPER_ENV`), one line
  `KEEPER_PRIVATE_KEY=0x...`, `chmod 600`. Use a fresh wallet holding only gas money. The key is never
  printed or logged; `--send` is refused when the file is missing.
- Telegram: `--notify` sends one Korean summary card per run when something happened (refunds sent,
  failures, new skips). Credentials from `~/.alert.env` (`$PAIDTHROUGH_ALERT_ENV`):
  `BOT_TOKEN_PAIDTHROUGH` or `BOT_TOKEN`, plus `USER_DIRECT_CHAT_ID`. Repeated skips are not re-sent
  within 24 hours.
- Fees (EIP-1559 type 2): `maxFeePerGas = max(2 x baseFee, 20 gwei, baseFee + priority)`, priority from
  `eth_maxPriorityFeePerGas`, gas limit = estimate x 1.2 rounded up, nonce from `pending`. Fees are shown
  in USDC from wei (18 decimals); bill amounts from 6-decimal units.

## How it reads the chain

- Rebuilds bills by folding `BillIssued / Paid / Claimed / Declined / Refunded` events.
  An impossible transition (for example `Paid` after `Claimed`) stops the run: that would mean a
  decoding bug, and the keeper must not act on a wrong picture.
- `BillCancelled` is not scanned: a cancelled bill was never paid, so it never matters for refunds, and
  it is cheap enough to emit thousands of times in one block. `scan` and `bill` read the true status of
  Open-looking bills with `getBill` (remembered once a bill is in a final state).
- `eth_getLogs` in chunks of 10,000 blocks (Arc's limit, measured 2026-10-01), halving on a range or
  result-count error. Raw logs are cached with the last scanned block so each 5-minute run only reads
  new blocks (plus a 100-block overlap).
- A single block with more logs than the RPC returns is re-read one event type at a time; a type that
  still overflows is recorded and the scan moves on. Missing `BillIssued` logs are filled from `getBill`
  (ids are sequential up to `billCount()`); a lost `Paid/Claimed/Declined/Refunded` log switches the
  keeper to reading every Open/Paid bill with `getBill`. A spam block cannot stop the keeper.
- Cross-checks `billCount()`: an id with no events is read with `getBill` and flagged, so an empty or
  short RPC answer is treated as unknown, never as "no bills".
- Before each refund it re-reads `getBill`, simulates, and checks the keeper's gas balance. A sent
  transaction whose receipt does not arrive is recorded; that bill is not sent again until the
  first transaction is resolved.

## Tests

```
python -m unittest discover -s keeper/tests -v
```

A fake node serves logs and `getBill` results encoded with `eth_abi`, with Arc's real error messages.
Signing tests use throwaway keys generated inside the test.
