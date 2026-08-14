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

Install Feral File's official CLI locally (Node.js 22 or newer), then validate,
sign, and play the result:

```bash
npm install -g @feralfile/cli
ff-cli config validate
ff-cli validate playlist.json
ff-cli sign playlist.json
ff-cli play playlist.json --device <device>
```

Video and audio items get `display.loop: true`: on DP-1 players, a work
reaching `ended` triggers a playlist-advance reload, so looping a lone
time-based work natively is what keeps it seamless.

## Match the display orientation

For a single image or video, reconcile the media's actual dimensions with the
FF1 before calling `ff-cli play`. This is an operator/agent concern rather than
part of DP-1 or Curio's storage model.

Read the first item's source and inspect its pixel dimensions locally:

```bash
source=$(jq -r '.items[0].source' playlist.json)
ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height:stream_side_data=rotation \
  -of json "$source"
```

Account for a 90° or 270° rotation tag when present. Width less than height is
portrait; width greater than height is landscape. Then query the FF1's local
control API:

```bash
curl -fsS 'http://<ff1-host>:1111/api/cast' \
  -H 'Content-Type: application/json' \
  -d '{"command":"getDeviceStatus","request":null}' | jq .screenRotation
```

Target the upright plain state, `portrait` or `landscape`; a matching
`*Reverse` state has the right aspect but is upside down. Choose the rotation
direction from the current state:

| Current state | Target portrait | Target landscape |
|---|---|---|
| `portrait` | no change | clockwise once |
| `portraitReverse` | rotate twice | counter-clockwise once |
| `landscape` | counter-clockwise once | no change |
| `landscapeReverse` | clockwise once | rotate twice |

Send one relative rotation with `clockwise` set to `true` or `false`, repeating
it for a two-rotation correction, then query status again:

```bash
curl -fsS 'http://<ff1-host>:1111/api/cast' \
  -H 'Content-Type: application/json' \
  -d '{"command":"rotate","request":{"clockwise":false}}'
```

Do not auto-rotate for square media, HTML/runtime works, mixed-orientation
playlists, or an inconclusive probe. An orientation explicitly requested by the
operator takes precedence. Port 1111 is an unauthenticated device-control
surface on current FF1 firmware; use it only on a trusted local network.
