# DP-1 players

[DP-1](https://github.com/display-protocol/dp1) is the open playlist format
spoken by DP-1 players such as the Feral File FF1. Curio emits a complete,
unsigned DP-1 1.0.0 playlist for catalogued refs; signing and device delivery
stay with the operator's DP-1 tooling.

```bash
curl -X POST 'http://<host>:8090/playlist/dp1' \
  -H 'Content-Type: application/json' \
  -d '{"refs": ["ipfs://bafy.../artwork"]}' > playlist.json
```

The `dp1_playlist` MCP tool takes the same `refs` (plus optional `title` and
`duration`) and returns the same playlist. Either way, every ref must already
be resolved and playable in Curio.

Sign and play the result with the operator's DP-1 tooling:

```bash
ff-cli validate playlist.json
ff-cli sign playlist.json
ff-cli play playlist.json --device <device>
```

Video and audio items get `display.loop: true`: on DP-1 players, a work
reaching `ended` triggers a playlist-advance reload, so looping a lone
time-based work natively is what keeps it seamless.
