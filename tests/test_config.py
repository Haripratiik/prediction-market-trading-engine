"""T-007 acceptance: config loads, env overrides YAML, and secrets stay out of the repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import (
    CONFIG_DIR,
    KalshiCredentials,
    RiskConfig,
    Settings,
    load_settings,
)


def test_risk_yaml_exists_and_parses():
    """The section 9 limits live in ONE file that the risk engine reads."""
    cfg = load_settings().risk
    assert cfg.position.cap_fraction_default == 0.02
    assert cfg.position.cap_fraction_gate4 == 0.01
    assert cfg.theme.max_exposure_fraction == 0.15
    assert cfg.theme.min_n_eff == 8.0
    assert cfg.deployment.min_cash_fraction == 0.30
    assert cfg.structures.leg_timeout_seconds == 900
    assert cfg.capacity.freeze_at_utilization == 0.50
    assert cfg.max_daily_loss_fraction == 0.05


def test_drawdown_ladder_returns_the_deepest_triggered_rung():
    cfg = RiskConfig()
    assert cfg.action_for_drawdown(0.05) is None
    assert cfg.action_for_drawdown(0.10) == "mandatory_written_review"
    assert cfg.action_for_drawdown(0.25) == "halve_all_position_caps"
    assert cfg.action_for_drawdown(0.35) == "halt_worst_sleeve_by_edge_ci"
    assert cfg.action_for_drawdown(0.60) == "full_stop_and_audit"


def test_gate4_cap_is_half_the_default():
    """Canary trades at 1%, scaled sleeves at 2% (PLAN.md section 8 G4)."""
    cfg = RiskConfig()
    assert cfg.position.cap_fraction_gate4 == cfg.position.cap_fraction_default / 2


def test_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "prod")
    monkeypatch.setenv("KALSHI_KEY_ID", "abcd-efgh")
    s = load_settings()
    assert s.kalshi.env == "prod"
    assert s.kalshi.key_id == "abcd-efgh"


def test_credentials_are_incomplete_without_a_key_file():
    c = KalshiCredentials(key_id="abc", private_key_path=Path("does/not/exist.pem"))
    assert not c.is_complete
    assert "no file at" in c.describe()


def test_describe_masks_the_key_id_and_never_prints_key_material():
    # A FAKE uuid.  The real key id identifies a live account and does not
    # belong in a public repository, even though the id alone cannot sign.
    c = KalshiCredentials(key_id="00000000-1111-2222-3333-444444444444")
    d = c.describe()
    assert "00000000" in d          # enough to identify
    assert "1111-2222-3333" not in d  # but not the whole thing
    assert "BEGIN" not in d


def test_signer_refuses_to_build_from_incomplete_credentials():
    c = KalshiCredentials(key_id="abc")
    with pytest.raises(RuntimeError, match="incomplete credentials"):
        c.signer()


def test_demo_and_prod_resolve_to_different_hosts():
    demo = KalshiCredentials(env="demo")
    prod = KalshiCredentials(env="prod")
    assert "demo" in demo.base_url and "demo" in demo.ws_url
    assert "demo" not in prod.base_url
    assert demo.base_url != prod.base_url


def test_secrets_file_is_gitignored_and_only_the_example_is_committed():
    """The private key path is configured; the key itself never enters the repo."""
    ignored = Path(".gitignore").read_text(encoding="utf-8")
    assert "config/secrets.env" in ignored
    assert (CONFIG_DIR / "secrets.env.example").is_file()
    example = (CONFIG_DIR / "secrets.env.example").read_text(encoding="utf-8")
    assert "KALSHI_PRIVATE_KEY_PATH" in example
    # the example must not contain a filled-in credential
    for line in example.splitlines():
        if line.startswith("KALSHI_KEY_ID="):
            assert line.strip() == "KALSHI_KEY_ID="


def test_settings_defaults_are_sane():
    s = Settings()
    assert s.db_path == Path("data/pm.db")
    assert s.kalshi.env == "demo"          # demo by default -- never prod by accident
    assert not s.kalshi.is_complete
