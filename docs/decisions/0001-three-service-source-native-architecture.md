# 0001: Keep Curio source-native and make resolution the storage operation

- Status: Accepted
- Date: 2026-08-08
- Product baseline: `feature/media-model` at `4c075f6`

## Context

Curio is a small trusted-network appliance, not a storage network or a general
media platform. During development, the appliance briefly grew to six
Arweave-related services: two AR.IO Core instances, two Redis instances,
Observer, and Envoy. Commit `c95d943` removed that topology.

A later experiment, `rewrite/async-library` at `b5bda9f`, replaced the public
resolver before reaching feature parity and duplicated resolution and static
storage paths.

The earlier product contract also separated resolution, storage, pinning, and
uploads into overlapping endpoints. That terminology obscured the appliance's
simpler purpose: submitting something to Curio means asking Curio to store it.

## Decision

Curio has three services:

1. the Curio resolver;
2. Kubo for IPFS;
3. one persistent AR.IO Core for Arweave.

`POST /resolve` is the single storage operation. It accepts either a reference
or an uploaded file, resolves the final artifact, stores it source-natively, and
records the submitted reference and playback route in Curio's existing SQLite
database.

`GET /resolve?ref=...` is playback lookup. It redirects a previously stored
reference to its source-native media path and returns 404 for an unknown
reference. The submitted reference is the identifier; Curio does not invent a
universal media ID.

Storage remains source-native:

- IPFS stays under its CID in Kubo, and POST resolution pins the CID root.
- Arweave stays under its transaction identity in the one persistent AR.IO Core.
- HTTP, `data:`, and uploaded media stay in Curio's static store.

The public status enum is `ready`, `live-dependent`, or `failed`. Curio has no
separate keep endpoint, store endpoint, pin option, transient mode, or curator
token. It is explicitly a trusted household or studio appliance.

## Consequences and guardrails

- A second AR.IO Core does not create another Arweave-network replica and is not
  part of the architecture.
- Redis, Envoy, Observer, or another Core require a concrete external need that
  cannot be met by the existing services.
- Ordinary HTTP URIs remain distinct reference identities even when their bytes
  deduplicate to the same SHA-256 object.
- IPFS and Arweave gateway spellings normalize to source-native lookup keys.
- GET never submits unknown media; it only redirects a recorded reference.
- Browsing and presentation of the appliance's contents are separate concerns.
- Curio retains remote-fetch bounds and private-target checks because remote NFT
  metadata is untrusted even on a trusted client network.
- Internal refactors must preserve this contract and should address demonstrated
  problems without replacing the resolver, storage model, and transports at once.
