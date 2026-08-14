# 0002: Use indexers for discovery, not Ethereum media identity

- Status: Accepted
- Date: 2026-08-14
- Product baseline: `v0.2.9`

## Context

Ethereum JSON-RPC has no practical method for listing every ERC-721 and
ERC-1155 token held by an address. Curio therefore uses Blockscout: it is a
keyless public indexer with holdings, balances, contract/token coordinates,
pagination, and BENS name resolution.

Blockscout also returns inline token metadata. That metadata is an upstream
indexed cache, not current contract state. A live Blockscout wallet response
can retain media references from an older metadata document after a mutable
contract base URI or token URI has changed. Curio does not locally cache wallet
responses, so repeating the wallet request does not correct this condition.

This caused a concrete recovery error: Curio treated dead CIDs from indexed
metadata as the NFT's current references and installed alternate-master
overrides. Direct `tokenURI(tokenId)` calls showed that the contracts now named
live metadata and media. The overrides were unnecessary and were removed.
Changing NFT indexers would not remove this class of failure; metadata caching
is inherent to indexers.

## Decision

Indexer output is discovery data, not Ethereum media identity.

For an Ethereum wallet or contract sweep:

1. Use Blockscout to enumerate holdings and obtain the token standard,
   contract address, token ID, and balance.
2. Use Ethereum RPC at current chain state to call `tokenURI(tokenId)` for
   ERC-721 or `uri(tokenId)` for ERC-1155. Apply the ERC-1155 `{id}` expansion
   required by the standard.
3. Fetch that returned metadata reference directly and resolve its media
   source-natively through Curio.
4. Repeat the contract read on later sweeps. A token URI or collection base URI
   may be mutable, so a previously observed pointer is not permanent truth.

Blockscout's inline metadata may be used only as a disclosed fallback when the
contract call is impossible, such as an unavailable RPC endpoint or a
non-standard contract. An index-derived fallback must not silently support a
claim that canonical media is dead, and must not be the sole basis for an
override.

The existing chain-first Verse resolver is the model and should be reused for
wallet-index coordinates rather than creating a second contract-resolution
path.

## Implementation

`v0.2.9` supplied Blockscout's inline `token.metadata` directly to wallet and
seed consumers. The change accompanying this decision makes both paths read the
current contract token URI and fetch its metadata before selecting media.
Wallet records disclose `token_uri` and `metadata_source` as `chain`,
`chain-unreachable`, or `indexer-fallback`.

Once a contract returns a token URI, unreachable metadata remains a visible
chain failure and stale index metadata is discarded. Blockscout metadata is
retained only when the RPC or contract call cannot provide a URI.

## Consequences and guardrails

- Blockscout remains appropriate for holdings discovery; Curio does not need to
  scan the Ethereum log history or operate its own indexer.
- Ethereum wallet sweeps require additional RPC calls. They must use bounded
  concurrency and existing timeout controls.
- Blockscout ownership itself can lag chain state, but that is a discovery-lag
  problem distinct from media identity. Direct contract reads prevent stale
  metadata from compounding it.
- A direct current token URI is authoritative for what the contract points to
  at that block; it does not prove authorship, authenticity, or permanence.
- Tests must cover an indexer returning stale metadata while the contract
  returns a newer live token URI, and must assert that Curio chooses the chain
  result.
- Failed direct chain resolution may fall back explicitly; it must never make
  the stale index value appear chain-derived.
