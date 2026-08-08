# Contributing

Issues and focused pull requests are welcome.

Before opening a pull request:

```bash
cd resolver
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
cd ..
./appliance/tests/test-appliance.sh
```

Also run `git diff --check`. Changes to the installer or Compose definition
should be exercised in a disposable Linux VM using
[`docs/appliance-testing.md`](docs/appliance-testing.md).

Please keep deployment-specific addresses, wallet lists, recovery manifests,
operator overrides, and captured media out of the repository. Use synthetic or
public fixtures in tests.

Security reports should follow [SECURITY.md](SECURITY.md), not the public issue
tracker.
