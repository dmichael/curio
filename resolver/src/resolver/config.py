from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Resolver configuration. Override any field with RESOLVER_<FIELD> in the env."""

    model_config = SettingsConfigDict(env_prefix="RESOLVER_", env_file=".env")  # pyright: ignore[reportUnannotatedClassAttribute]

    host: str = "0.0.0.0"
    port: int = 8090

    # Gateways the resolver PROBES and fetches metadata from (on-box, localhost).
    ipfs_internal: str = "http://127.0.0.1:8080"
    # Ordinary AR.IO Core's data API. Envoy remains Core's trusted upstream,
    # but its short cold-route deadline must not sit in Curio's byte path.
    arweave_internal: str = "http://127.0.0.1:4000"
    # Private native AR.IO Core used only for explicit Arweave keep intent.
    # It has separate on-disk Core state from the ordinary evictable gateway.
    arweave_retained_internal: str = "http://127.0.0.1:4001"
    arweave_retention_db: str = ""

    # Public front-door URL for non-request callers (notably MCP).  Set this
    # behind a reverse proxy; forwarded headers are deliberately not trusted
    # from arbitrary direct clients.
    public_base_url: str = ""
    # Compatibility values for direct resolve_ref callers. HTTP requests use
    # their request origin; MCP uses public_base_url or a non-routable marker.
    ipfs_public_base: str = ""
    arweave_public_base: str = ""

    # Curio's own HTTP backend. Its SQLite catalogue is authoritative for
    # retention metadata; object files are addressed by their SHA-256 digest.
    static_root: str = "/tmp/curio-media"
    static_max_bytes: int = Field(default=128_000_000, gt=0)
    static_fetch_concurrency: int = Field(default=4, ge=1)
    data_max_bytes: int = Field(default=4_000_000, gt=0)
    redirect_max_hops: int = Field(default=5, ge=0)
    ssrf_dns_check: bool = True

    # Empty means curator mutations are disabled rather than public.
    curator_token: str = ""

    # Operator-curated exception registry (overrides.py, docs/design.md):
    # a TOML file mapping dead canonical refs to replacements. Empty disables
    # it. Reloaded whenever the file's mtime changes — edits need no restart.
    overrides_path: str = ""

    # Household favorites (favorites.py): a JSON list of owner-picked refs,
    # keyed by canonical ref. Empty disables. Mtime-reloaded like overrides.
    favorites_path: str = ""

    http_timeout: float = Field(default=20.0, gt=0)
    # Cap on any single body the resolver reads into memory (metadata JSON,
    # verse pages, directory listings). Media bytes are never buffered here.
    fetch_max_bytes: int = Field(default=2_000_000, gt=0)

    # Seeding (/seed): the box's Kubo API, and the keyless public indexers
    # used to enumerate a wallet's holdings.
    ipfs_api: str = "http://127.0.0.1:5001"
    blockscout_base: str = "https://eth.blockscout.com/api/v2"
    bens_base: str = "https://bens.services.blockscout.com/api/v1/1"
    tzkt_base: str = "https://api.tzkt.io/v1"
    seed_concurrency: int = Field(default=3, ge=1)
    seed_pin_timeout: float = Field(default=300.0, gt=0)
    # Native Core can retrieve a cold transaction on demand. Keep this separate
    # from the short general HTTP timeout used for ordinary metadata requests.
    arweave_cold_timeout: float = Field(default=300.0, gt=0)
    seed_max_active: int = Field(default=4, ge=1)  # concurrent seed jobs
    seed_max_seconds: float = Field(default=14_400.0, gt=0)  # wall-clock cap per job
    seed_jobs_kept: int = Field(default=100, ge=1)  # finished-job history retained
    seed_recover_max_bytes: int = Field(default=1_073_741_824, gt=0)
    # When set, /seed also captures plain-HTTP media (works with no content
    # address — the refs most likely to vanish, cf. Horizon) into Kubo and
    # appends provenance records to <dir>/captures.jsonl. Empty disables.
    seed_capture_dir: str = ""
    # Public gateways tried as HTTP-recovery sources for CIDs whose IPFS
    # fetch fails — their caches often outlive the original providers.
    seed_recovery_gateways: list[str] = ["https://ipfs.io/ipfs", "https://dweb.link/ipfs"]

    @model_validator(mode="after")
    def _default_public_to_internal(self) -> Settings:
        # HTTP requests replace these with their actual request origin. The
        # fallback is the resolver front door for non-request callers (MCP),
        # never an internal gateway address.
        # Never manufacture a localhost URL for an MCP/non-request result.
        fallback = self.public_base_url.rstrip("/") or "https://curio.invalid"
        self.ipfs_public_base = self.ipfs_public_base or fallback
        self.arweave_public_base = self.arweave_public_base or fallback
        self.arweave_retention_db = self.arweave_retention_db or f"{self.static_root.rstrip('/')}/arweave-retained.sqlite3"
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
