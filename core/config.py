"""Configuration.  T-007.  PLAN.md 0.3: no magic numbers in code.

Secrets NEVER live in this repo.  Credentials come from environment variables or
a `.env` file that `.gitignore` excludes:

    KALSHI_ENV=demo                       # demo | prod
    KALSHI_KEY_ID=<uuid from the dashboard>
    KALSHI_PRIVATE_KEY_PATH=C:\\Users\\you\\.kalshi\\demo_key.pem

The private key is read from a PATH, never embedded, never pasted, never logged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


# --------------------------------------------------------------------------- #
# Risk limits.  PLAN.md section 9 -- the ONLY place these values are defined.
# --------------------------------------------------------------------------- #
class PositionLimits(BaseModel):
    model_config = ConfigDict(frozen=True)
    cap_fraction_default: float = 0.02      # ~0.11x Kelly on the flagship sleeve
    cap_fraction_gate4: float = 0.01
    cap_fraction_max: float = 0.05


class ThemeLimits(BaseModel):
    model_config = ConfigDict(frozen=True)
    max_exposure_fraction: float = 0.15     # intra-theme rho >= 0.5 -> ~2 effective bets
    min_n_eff: float = 8.0


class DeploymentLimits(BaseModel):
    model_config = ConfigDict(frozen=True)
    max_gross_fraction: float = 0.40
    min_cash_fraction: float = 0.30
    max_fraction_per_venue: float = 0.60


class StructureLimits(BaseModel):
    model_config = ConfigDict(frozen=True)
    max_per_structure_fraction: float = 0.05
    max_sleeve_total_fraction: float = 0.15
    min_annualized_return_on_locked_capital: float = 0.15
    leg_timeout_seconds: int = 900
    max_orphan_exposure_fraction: float = 0.005


class CapacityLimits(BaseModel):
    model_config = ConfigDict(frozen=True)
    max_resting_fraction_of_touch_depth: float = 0.20
    max_taking_fraction_of_recent_volume: float = 0.05
    freeze_at_utilization: float = 0.50


class DrawdownRung(BaseModel):
    model_config = ConfigDict(frozen=True)
    at: float
    action: str


class RiskConfig(BaseModel):
    """Loaded from config/risk.yaml.  The risk engine reads ONLY this."""

    model_config = ConfigDict(frozen=True)

    position: PositionLimits = Field(default_factory=PositionLimits)
    theme: ThemeLimits = Field(default_factory=ThemeLimits)
    deployment: DeploymentLimits = Field(default_factory=DeploymentLimits)
    structures: StructureLimits = Field(default_factory=StructureLimits)
    capacity: CapacityLimits = Field(default_factory=CapacityLimits)
    max_daily_loss_fraction: float = 0.05
    drawdown_ladder: tuple[DrawdownRung, ...] = (
        DrawdownRung(at=0.10, action="mandatory_written_review"),
        DrawdownRung(at=0.20, action="halve_all_position_caps"),
        DrawdownRung(at=0.30, action="halt_worst_sleeve_by_edge_ci"),
        DrawdownRung(at=0.40, action="full_stop_and_audit"),
    )

    def action_for_drawdown(self, drawdown: float, *, tol: float = 1e-9) -> str | None:
        """The deepest rung triggered by a peak-to-trough drawdown.

        The tolerance is NOT cosmetic: 1 - 800000/1000000 evaluates to
        0.19999999999999996, so an exact `>= 0.20` comparison silently fails to
        engage the ladder at precisely the threshold it exists to catch.
        """
        triggered = [r for r in self.drawdown_ladder if drawdown >= r.at - tol]
        return triggered[-1].action if triggered else None


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
class KalshiCredentials(BaseModel):
    """Where the signing material lives.  Path only -- never the key itself."""

    model_config = ConfigDict(frozen=True)

    env: Literal["demo", "prod"] = "demo"
    key_id: str | None = None
    private_key_path: Path | None = None

    @property
    def base_url(self) -> str:
        from venues.kalshi.client import DEMO_BASE, PROD_BASE

        return DEMO_BASE if self.env == "demo" else PROD_BASE

    @property
    def ws_url(self) -> str:
        from venues.kalshi.client import DEMO_WS, PROD_WS

        return DEMO_WS if self.env == "demo" else PROD_WS

    @property
    def is_complete(self) -> bool:
        return bool(
            self.key_id
            and self.private_key_path
            and Path(self.private_key_path).is_file()
        )

    def describe(self) -> str:
        """Human-readable status.  Never prints key material."""
        if not self.key_id:
            return "no KALSHI_KEY_ID set"
        masked = f"{self.key_id[:8]}...{self.key_id[-4:]}"
        if not self.private_key_path:
            return f"key_id {masked}, but KALSHI_PRIVATE_KEY_PATH is unset"
        p = Path(self.private_key_path)
        if not p.is_file():
            return f"key_id {masked}, but no file at {p}"
        return f"{self.env}: key_id {masked}, private key at {p}"

    def signer(self):  # -> KalshiSigner
        from venues.kalshi.auth import KalshiSigner

        if not self.is_complete:
            raise RuntimeError(f"incomplete credentials: {self.describe()}")
        assert self.key_id and self.private_key_path
        return KalshiSigner.from_file(self.key_id, self.private_key_path)


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    db_path: Path = Path("data/pm.db")
    kalshi: KalshiCredentials = Field(default_factory=KalshiCredentials)
    risk: RiskConfig = Field(default_factory=RiskConfig)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _load_dotenv(path: Path) -> None:
    """Minimal .env reader -- no dependency, and it never overrides real env vars."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_settings(*, config_dir: Path | None = None) -> Settings:
    """Environment wins over YAML; YAML wins over defaults."""
    cdir = config_dir or CONFIG_DIR
    _load_dotenv(cdir / "secrets.env")

    risk = RiskConfig(**_read_yaml(cdir / "risk.yaml"))
    base = _read_yaml(cdir / "base.yaml")

    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    creds = KalshiCredentials(
        env=os.environ.get("KALSHI_ENV", base.get("kalshi_env", "demo")),  # type: ignore[arg-type]
        key_id=os.environ.get("KALSHI_KEY_ID") or base.get("kalshi_key_id"),
        private_key_path=Path(key_path).expanduser() if key_path else None,
    )
    return Settings(
        db_path=Path(os.environ.get("PM_DB_PATH", base.get("db_path", "data/pm.db"))),
        kalshi=creds,
        risk=risk,
    )
