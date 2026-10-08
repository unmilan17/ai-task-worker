"""Runtime settings, loaded from environment variables (and an optional .env file).

Everything that you might want to tweak between runs lives here so the rest
of the code never reads os.environ directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # ---- LLM -------------------------------------------------------------
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "anthropic"))
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-opus-5-5"))
    # Effort controls how hard the model thinks per step (low|medium|high|xhigh|max).
    effort: str = field(default_factory=lambda: os.getenv("LLM_EFFORT", "medium"))
    # Server-side refusal fallback (Anthropic only). Lets the API re-run a declined
    # request on a fallback model instead of failing the step.
    refusal_fallback: bool = field(default_factory=lambda: _bool("ANTHROPIC_REFUSAL_FALLBACK", True))

    # ---- budgets / loop control -----------------------------------------
    max_steps: int = field(default_factory=lambda: int(os.getenv("MAX_STEPS", "60")))
    max_minutes: float = field(default_factory=lambda: float(os.getenv("MAX_MINUTES", "20")))
    max_cost_usd: float = field(default_factory=lambda: float(os.getenv("MAX_COST_USD", "5")))
    max_verify_rounds: int = field(default_factory=lambda: int(os.getenv("MAX_VERIFY_ROUNDS", "2")))
    max_tool_retries: int = field(default_factory=lambda: int(os.getenv("MAX_TOOL_RETRIES", "2")))
    human_timeout_s: float = field(default_factory=lambda: float(os.getenv("HUMAN_TIMEOUT_S", "900")))

    # ---- browser ---------------------------------------------------------
    headless: bool = field(default_factory=lambda: _bool("HEADLESS", True))
    slow_mo_ms: int = field(default_factory=lambda: int(os.getenv("BROWSER_SLOW_MO_MS", "0")))
    record_video: bool = field(default_factory=lambda: _bool("RECORD_VIDEO", True))
    snapshot_max_chars: int = field(default_factory=lambda: int(os.getenv("SNAPSHOT_MAX_CHARS", "9000")))

    # ---- paths -----------------------------------------------------------
    runs_dir: Path = field(default_factory=lambda: Path(os.getenv("RUNS_DIR", PROJECT_ROOT / "runs")))
    workspace_dir: Path = field(default_factory=lambda: Path(os.getenv("WORKSPACE_DIR", PROJECT_ROOT / "workspace")))
    environment_file: Path = field(default_factory=lambda: Path(
        os.getenv("ENVIRONMENT_FILE", PROJECT_ROOT / "config" / "environment.yaml")))
    policy_file: Path = field(default_factory=lambda: Path(
        os.getenv("POLICY_FILE", PROJECT_ROOT / "config" / "policy.yaml")))
    secrets_file: Path = field(default_factory=lambda: Path(
        os.getenv("SECRETS_FILE", PROJECT_ROOT / "sandbox" / "test_credentials.json")))


def get_settings(**overrides) -> Settings:
    s = Settings()
    for k, v in overrides.items():
        if v is not None:
            setattr(s, k, v)
    return s
