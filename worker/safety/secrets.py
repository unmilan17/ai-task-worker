"""Credential vault: the LLM never sees a real secret.

The model writes placeholders such as ``{{secret:erp.password}}``. Tools call
``vault.resolve(text)`` right before acting (typing into a field, sending an
HTTP header), and every piece of text that flows back to the model, the trace
or the UI goes through ``vault.redact(text)``, which turns any secret value
back into its placeholder.

Why this matters: prompts, traces and screenshots get logged, shared and
sometimes leaked. A model that never holds the raw credential cannot leak it,
cannot be tricked by a prompt-injection into revealing it, and the secret can
be rotated without touching prompts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"\{\{\s*secret:([a-zA-Z0-9_.\-]+)\s*\}\}")


class SecretVault:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = dict(secrets or {})

    @classmethod
    def from_file(cls, path: Path) -> "SecretVault":
        if not path.exists():
            return cls({})
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def names(self) -> list[str]:
        return sorted(self._secrets)

    def resolve(self, text: str) -> str:
        """Replace {{secret:name}} placeholders with real values. Unknown names raise KeyError."""
        def sub(m: re.Match) -> str:
            name = m.group(1)
            if name not in self._secrets:
                raise KeyError(f"Unknown secret '{name}'. Available: {', '.join(self.names()) or 'none'}")
            return self._secrets[name]
        return PLACEHOLDER_RE.sub(sub, text)

    def resolve_obj(self, obj):
        """Recursively resolve placeholders inside dicts/lists/strings."""
        if isinstance(obj, str):
            return self.resolve(obj)
        if isinstance(obj, dict):
            return {k: self.resolve_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.resolve_obj(v) for v in obj]
        return obj

    def redact(self, text: str) -> str:
        """Replace any real secret value appearing in text with its placeholder."""
        if not text:
            return text
        # Longest first so that overlapping values are handled correctly.
        for name, value in sorted(self._secrets.items(), key=lambda kv: -len(kv[1])):
            if value and len(value) >= 4 and value in text:
                text = text.replace(value, "{{secret:%s}}" % name)
        return text

    def redact_obj(self, obj):
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        return obj
