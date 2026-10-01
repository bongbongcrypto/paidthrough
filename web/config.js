// Per-network settings. The page reads chain state over the public RPC below and
// sends transactions through the visitor's own wallet. Nothing here is secret.
//
// Until `paidThrough` is set for the active network the page runs as a labelled
// preview: example bills are drawn locally and nothing is read from or sent to Arc.

export const CONFIG = {
  network: 'mainnet', // which entry of `networks` the page uses

  networks: {
    mainnet: {
      label: 'Arc mainnet',
      chainId: 5042,
      rpc: 'https://rpc.mainnet.arc.io',
      // Blockscout explorer listed on docs.arc.io "Connect to Arc" (checked 2026-10-01; /tx/<hash> returns 200).
      explorer: 'https://explorer.arc.io',
      usdc: '0x3600000000000000000000000000000000000000',
      paidThrough: null, // set after deploy
      deployBlock: null, // block of the deploy tx; the biller's list scans logs from here
    },
    testnet: {
      label: 'Arc testnet',
      chainId: 5042002,
      // SPEC.md value (probed 2026-10-01). docs.arc.io now lists https://rpc.testnet.arc.io as primary.
      rpc: 'https://rpc.testnet.arc.network',
      explorer: 'https://explorer.testnet.arc.io',
      usdc: '0x3600000000000000000000000000000000000000',
      paidThrough: null,
      deployBlock: null,
    },
  },

  // Mirrors the contract's constructor argument; used for form checks only. The deployed contract also exposes
  // it on chain as maxAmount() (public immutable, uint96), so the page could read it instead of trusting this copy.
  maxAmountUnits: 10_000_000_000n, // 10,000 USDC in 6-decimal units

  // getLogs span the public RPC accepts (measured 2026-10-01: 10,000 blocks; 10,001 is "range too large").
  logChunk: 10_000,

  // Gas used before a signature exists (payWithAuthorization can't be estimated unsigned).
  // Shown to the payer as an approximate fee; replaced by eth_estimateGas once signed.
  gasHint: { payWithAuthorization: 130_000n, approve: 60_000n, pay: 110_000n, issue: 130_000n },

  // Arc drops transactions priced under this base-fee floor without an error (SPEC.md chain facts).
  minFeePerGas: 20_000_000_000n,

  refreshMs: 10_000, // status view refresh while a bill is still moving
};

export function activeNetwork() {
  return CONFIG.networks[CONFIG.network];
}
