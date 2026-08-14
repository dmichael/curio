# Agent Instructions

## Worktrees

Create temporary Git worktrees under `/tmp`, never as sibling directories under
`~/projects`. Remove temporary worktrees when the task is complete unless the
user explicitly asks to retain them.

## Validation

Follow the checks in [CONTRIBUTING.md](CONTRIBUTING.md). Installer or Compose
changes must also be exercised in a disposable Linux VM.

## Curio-driven FF1 playback

Follow [docs/dp1-players.md](docs/dp1-players.md) when sending Curio playlists to
an FF1. Before playing a single image or video, compare its actual pixel
dimensions with the device's current `screenRotation`. For unambiguous media,
target the upright `portrait` or `landscape` state, not its `Reverse` variant.
Choose clockwise versus counter-clockwise to reach that state; if the aspect
already matches but the state is reversed, rotate twice. Do not infer an
orientation for square media, HTML/runtime works, mixed-orientation playlists,
or failed probes. An orientation stated by the user always wins.
