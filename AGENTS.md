# Agent Instructions

## Worktrees

Create temporary Git worktrees under `/tmp`, never as sibling directories under
`~/projects`. Remove temporary worktrees when the task is complete unless the
user explicitly asks to retain them.

## Validation

Follow the checks in [CONTRIBUTING.md](CONTRIBUTING.md). Installer or Compose
changes must also be exercised in a disposable Linux VM.
