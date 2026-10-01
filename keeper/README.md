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

Other options: `--rpc URL`, `--state-dir DIR` (state file, default `~/.paidthrough-keeper`, or
`$PAIDTHROUGH_KEEPER_STATE`), `--no-cache` (re-read all logs), and for `run`: `--from-address` (dry-run
simulation sender), `--max-sends 20`, `--min-amount 0.05`, `--max-fee-gwei 500`, `--receipt-timeout 90`,
`--time-budget 180`.

Exit codes: 0 ok (skipped bills included), 1 error, unknown chain state or state file not saved,
2 refused (config, key, chain id, or `run --send` with a state file it cannot write).

## Run time and the state file

- Time budget: `run` starts no new refund (simulation or send) once `--time-budget` seconds (default 180)
  have passed since it started. The refund already under way finishes, the rest are listed as skipped
  ("time_budget") and the next run picks them up; the run then saves its state, sends its card and exits 0.
  Chain reads before the first refund are not cut short: the log cache is saved only after a complete
  read, so cutting a long first scan would repeat it on every run.
- The state file holds the log cache and every sent-but-unconfirmed refund (the double-send guard). It is
  written right after each send, not only at the end, so a run stopped from outside cannot lose a record.
- `run --send` first checks that the state file can be written and refuses (exit 2, nothing read or sent)
  when it cannot. A save that fails later stops further sends, prints the error and exits 1. With
  `--notify`, both send a "키퍼 기록 저장 실패" card, on every run until the directory is fixed (the
  card de-dup lives in that same file). `scan`, `due` and `bill` only warn: their output is still correct.

## Install on the server

The unit and timer in `keeper/systemd/` run `run --send --notify --network mainnet` every 5 minutes
with `MemoryMax=200M`. Installing them, funding the keeper wallet and enabling the timer are the
operator's decisions. Everything lives in one directory:

| Path | What |
|---|---|
| `/home/ubuntu/bots/paidthrough-keeper/keeper/` | copy of this repo's `keeper/`, including `deployments.json` after deploy |
| `/home/ubuntu/bots/paidthrough-keeper/venv/` | Python 3.11 venv |
| `/home/ubuntu/.paidthrough-keeper/` | state file (must exist before the unit starts) |
| `/home/ubuntu/.paidthrough-keeper.env` | `KEEPER_PRIVATE_KEY=0x...`, `chmod 600` |
| `/home/ubuntu/.alert.env` | Telegram token and chat id |

1. From the repo folder on your computer, copy `keeper/` over (replace `SERVER` with your ssh host).
   Run this again after the mainnet deploy, once the deploy script has filled `keeper/deployments.json`
   (or copy just that file). A later copy of `keeper/` replaces the server's `deployments.json` with the
   repo's, so commit the deploy result first.

   ```
   ssh SERVER "mkdir -p /home/ubuntu/bots/paidthrough-keeper"
   scp -r keeper SERVER:/home/ubuntu/bots/paidthrough-keeper/
   scp keeper/deployments.json SERVER:/home/ubuntu/bots/paidthrough-keeper/keeper/deployments.json
   ```

2. On the server (bash): venv, pinned libraries, state directory. The `pip install` runs under a memory cap.
   On Ubuntu `python3.11 -m venv` needs the `python3.11-venv` package.

   ```
   cd /home/ubuntu/bots/paidthrough-keeper
   python3.11 -m venv venv
   systemd-run --user --scope -p MemoryMax=800M venv/bin/pip install eth-account==0.13.7 eth-abi==6.0.0 pycryptodome==3.23.0
   install -d -m 700 /home/ubuntu/.paidthrough-keeper
   ```

3. Key file, created by the operator. Use a fresh wallet that holds only gas money. Paste the line
   `KEEPER_PRIVATE_KEY=0x...` in the editor; never `echo` the key (it would stay in the shell history).
   The key is never printed or logged, and `--send` is refused when the file is missing.

   ```
   touch /home/ubuntu/.paidthrough-keeper.env
   chmod 600 /home/ubuntu/.paidthrough-keeper.env
   nano /home/ubuntu/.paidthrough-keeper.env
   ```

4. Dry-run from the server first (no key used, nothing sent). If it ends in `RpcUnavailable`, the public
   RPC is refusing the server: add `--rpc URL` with another Arc mainnet RPC to `ExecStart`.

   ```
   cd /home/ubuntu/bots/paidthrough-keeper
   venv/bin/python keeper/paidthrough_keeper.py due --network mainnet
   venv/bin/python keeper/paidthrough_keeper.py run --network mainnet
   ```

5. Install the unit, run it once by hand, read its log, then enable the timer.

   ```
   cd /home/ubuntu/bots/paidthrough-keeper
   sudo cp keeper/systemd/paidthrough-keeper.service keeper/systemd/paidthrough-keeper.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl start paidthrough-keeper.service
   sudo journalctl -u paidthrough-keeper.service -n 50 --no-pager
   sudo systemctl enable --now paidthrough-keeper.timer
   systemctl list-timers paidthrough-keeper.timer
   ```

Notes:

- If `/home/ubuntu/.paidthrough-keeper` is missing, the unit fails before Python starts (status
  `226/NAMESPACE` in `systemctl status paidthrough-keeper.service`) and no Telegram card is sent.
  Manual runs as `ubuntu` use the same state directory by default, so they share the double-send guard.
- `TimeoutStartSec=1200` is above the worst case after the time budget: the one refund already started
  (6 RPC calls at the client's worst 115 s each, a 90 s receipt wait and one more poll) plus the Telegram
  card, 1116 s in total (`worst_case_seconds()`; a test fails if the unit drops below it).
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
Signing tests use throwaway keys generated inside the test. `test_runtime.py` runs a slow fake node against
the time budget, points the state directory under a regular file, and checks the systemd unit's paths and
`TimeoutStartSec`.
