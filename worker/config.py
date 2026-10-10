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


def _profile_file(profile: str, name: str, env_var: str) -> Path:
    """config/<name> for the default profile, config/<profile>/<name> for others (env var overrides)."""
    if os.getenv(env_var):
        return Path(os.environ[env_var])
    profile = (profile or "northwind").strip().lower()
    return PROJECT_ROOT / "config" / (name if profile == "northwind" else f"{profile}/{name}")


@dataclass
class Settings:
    # ---- LLM -------------------------------------------------------------
    # Provider: gemini (free tier, default) | groq | openrouter | ollama | openai | anthropic.
    # All but "anthropic" go through the OpenAI-compatible adapter (llm/openai_compat.py).
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "gemini").strip().lower())
    # Empty = the provider's default model (see DEFAULT_MODELS below).
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "").strip())
    # Effort controls how hard the model thinks per step (low|medium|high|xhigh|max).
    effort: str = field(default_factory=lambda: os.getenv("LLM_EFFORT", "medium"))
    # Client-side rate limiting for free tiers (0 = use the provider preset's default).
    llm_rpm: int = field(default_factory=lambda: int(os.getenv("LLM_RPM", "0")))
    llm_tpm: int = field(default_factory=lambda: int(os.getenv("LLM_TPM", "0")))
    # Override the provider's API base URL (e.g. a self-hosted OpenAI-compatible server).
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "").strip())
    # Server-side refusal fallback (Anthropic only). Lets the API re-run a declined
    # request on a fallback model instead of failing the step.
    refusal_fallback: bool = field(default_factory=lambda: _bool("ANTHROPIC_REFUSAL_FALLBACK", True))

    # ---- budgets / loop control -----------------------------------------
    max_steps: int = field(default_factory=lambda: int(os.getenv("MAX_STEPS", "60")))
    max_minutes: float = field(default_factory=lambda: float(os.getenv("MAX_MINUTES", "30")))
    max_cost_usd: float = field(default_factory=lambda: float(os.getenv("MAX_COST_USD", "5")))
    max_verify_rounds: int = field(default_factory=lambda: int(os.getenv("MAX_VERIFY_ROUNDS", "2")))
    max_tool_retries: int = field(default_factory=lambda: int(os.getenv("MAX_TOOL_RETRIES", "2")))
    # Pre-write review (safety/review.py): provenance check (deterministic) and ambiguity review (LLM).
    provenance_check: bool = field(default_factory=lambda: _bool("PROVENANCE_CHECK", True))
    ambiguity_review: bool = field(default_factory=lambda: _bool("AMBIGUITY_REVIEW", True))
    human_timeout_s: float = field(default_factory=lambda: float(os.getenv("HUMAN_TIMEOUT_S", "900")))

    # ---- browser ---------------------------------------------------------
    headless: bool = field(default_factory=lambda: _bool("HEADLESS", True))
    slow_mo_ms: int = field(default_factory=lambda: int(os.getenv("BROWSER_SLOW_MO_MS", "0")))
    record_video: bool = field(default_factory=lambda: _bool("RECORD_VIDEO", True))
    snapshot_max_chars: int = field(default_factory=lambda: int(os.getenv("SNAPSHOT_MAX_CHARS", "9000")))

    # ---- paths -----------------------------------------------------------
    runs_dir: Path = field(default_factory=lambda: Path(os.getenv("RUNS_DIR", PROJECT_ROOT / "runs")))
    workspace_dir: Path = field(default_factory=lambda: Path(os.getenv("WORKSPACE_DIR", PROJECT_ROOT / "workspace")))
    # Profile = which workplace the worker is onboarded to: "northwind" (default) or "centralign" (test-suite
    # fixtures). It selects the onboarding document, the policy and the dashboard samples.
    profile: str = field(default_factory=lambda: os.getenv("PROFILE", "northwind").strip().lower())
    environment_file: Path = field(default_factory=lambda: _profile_file(os.getenv("PROFILE", "northwind"),
                                                                         "environment.yaml", "ENVIRONMENT_FILE"))
    policy_file: Path = field(default_factory=lambda: _profile_file(os.getenv("PROFILE", "northwind"),
                                                                    "policy.yaml", "POLICY_FILE"))
    samples_file: Path = field(default_factory=lambda: _profile_file(os.getenv("PROFILE", "northwind"),
                                                                     "samples.yaml", "SAMPLES_FILE"))
    secrets_file: Path = field(default_factory=lambda: Path(
        os.getenv("SECRETS_FILE", PROJECT_ROOT / "sandbox" / "test_credentials.json")))


# Default model per provider when LLM_MODEL is not set. Free-tier model availability changes
# over time - check the provider's console (e.g. Google AI Studio) and override LLM_MODEL if needed.
DEFAULT_MODELS = {
    "gemini": "gemini-3.5-flash-lite",   # free tier: gemini-3.8-flash allows only 20 requests/day
    "groq": "openai/gpt-oss-120b",
    "openrouter": "",            # pick a ":free" model on openrouter.ai and set LLM_MODEL
    "ollama": "qwen3:14b",
    "openai": "gpt-5-mini",
    "anthropic": "claude-opus-5-5",
}


def get_settings(**overrides) -> Settings:
    s = Settings()
    for k, v in overrides.items():
        if v is not None:
            setattr(s, k, v)
    if overrides.get("profile"):
        for attr, name, env_var in (("environment_file", "environment.yaml", "ENVIRONMENT_FILE"),
                                    ("policy_file", "policy.yaml", "POLICY_FILE"),
                                    ("samples_file", "samples.yaml", "SAMPLES_FILE")):
            if overrides.get(attr) is None:
                setattr(s, attr, _profile_file(s.profile, name, env_var))
    if not s.model:
        s.model = DEFAULT_MODELS.get(s.llm_provider, "")
    return s
