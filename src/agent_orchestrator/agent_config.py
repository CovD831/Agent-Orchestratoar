"""Configurable agent profiles for provider, model, and prompt selection."""
from __future__ import annotations

# DEPS: __future__, dataclasses, json, pathlib, typing
# RESPONSIBILITY: Persist and resolve per-agent provider/model/prompt configuration.
# MODULE: interface
# ---

import json
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

AgentRoleId = Literal[
    "planner",
    "plan_reviewer",
    "adversarial_reviewer",
    "worker",
    "execution_reviewer",
    "rescue",
    "ideation_proponent",
    "ideation_skeptic",
]
RuntimeMode = Literal["cli_inherit", "cli_isolated", "direct_api"]

DEFAULT_PROMPTS: dict[str, str] = {
    "planner": "Role discipline: clarify intent, dependencies, next executable item, and stop conditions before execution.\n{default_prompt}",
    "plan_reviewer": "Role discipline: review only; identify required gaps, assumptions, and unsafe execution shortcuts.\n{default_prompt}",
    "adversarial_reviewer": "Role discipline: challenge the plan direction, hidden coupling, rollback risk, and alternative safer paths.\n{default_prompt}",
    "worker": "Role discipline: implement only the approved work unit, validate locally, and report touched files and blockers.\n{default_prompt}",
    "execution_reviewer": "Role discipline: review worker output without auto-fixing; separate findings from rescue recommendations.\n{default_prompt}",
    "rescue": "Role discipline: fix the concrete failed path with the smallest safe patch and explain validation.\n{default_prompt}",
    "ideation_proponent": "{default_prompt}",
    "ideation_skeptic": "{default_prompt}",
}


@dataclass(frozen=True, slots=True)
class AgentProfile:
    role: str
    provider: str
    model: str | None = None
    prompt_template: str = "{default_prompt}"
    reasoning_effort: str = "medium"
    sandbox: str | None = None
    runtime_mode: RuntimeMode = "cli_inherit"
    enabled: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "prompt_template": self.prompt_template,
            "reasoning_effort": self.reasoning_effort,
            "sandbox": self.sandbox,
            "runtime_mode": self.runtime_mode,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, role: str, data: dict[str, object]) -> "AgentProfile":
        return cls(
            role=str(data.get("role") or role),
            provider=_provider(data.get("provider"), default=_default_provider(role)),
            model=_optional_string(data.get("model")),
            prompt_template=str(data.get("prompt_template") or DEFAULT_PROMPTS.get(role, "{default_prompt}")),
            reasoning_effort=str(data.get("reasoning_effort") or "medium"),
            sandbox=_optional_string(data.get("sandbox")),
            runtime_mode=_runtime_mode(data.get("runtime_mode")),
            enabled=bool(data.get("enabled", True)),
        )

    def render_prompt(self, default_prompt: str, **context: object) -> str:
        template = self.prompt_template or "{default_prompt}"
        values = {
            "default_prompt": default_prompt,
            "role": self.role,
            "provider": self.provider,
            "model": self.model or "",
            **{key: str(value) for key, value in context.items()},
        }
        try:
            rendered = template.format(**values)
        except KeyError:
            rendered = template.replace("{default_prompt}", default_prompt)
        return rendered.strip() or default_prompt


@dataclass(frozen=True, slots=True)
class AgentConfig:
    profiles: dict[str, AgentProfile] = field(default_factory=dict)
    schema_version: str = "1.0"

    @classmethod
    def defaults(cls) -> "AgentConfig":
        return cls(
            profiles={
                "planner": AgentProfile(role="planner", provider="mock", model=None, prompt_template=DEFAULT_PROMPTS["planner"], runtime_mode="direct_api"),
                "plan_reviewer": AgentProfile(role="plan_reviewer", provider="claude", model="sonnet", prompt_template=DEFAULT_PROMPTS["plan_reviewer"], runtime_mode="direct_api"),
                "adversarial_reviewer": AgentProfile(role="adversarial_reviewer", provider="claude", model="opus", prompt_template=DEFAULT_PROMPTS["adversarial_reviewer"], runtime_mode="direct_api"),
                "worker": AgentProfile(role="worker", provider="codex", model="gpt-5.4", prompt_template=DEFAULT_PROMPTS["worker"]),
                "execution_reviewer": AgentProfile(role="execution_reviewer", provider="claude", model="sonnet", prompt_template=DEFAULT_PROMPTS["execution_reviewer"], runtime_mode="direct_api"),
                "rescue": AgentProfile(role="rescue", provider="claude", model="sonnet", prompt_template=DEFAULT_PROMPTS["rescue"]),
                "ideation_proponent": AgentProfile(role="ideation_proponent", provider="claude", model="sonnet", runtime_mode="direct_api"),
                "ideation_skeptic": AgentProfile(role="ideation_skeptic", provider="codex", model="gpt-5.4", runtime_mode="direct_api"),
            }
        )

    def profile(self, role: str) -> AgentProfile:
        return self.profiles.get(role) or self.defaults().profiles[role]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profiles": {role: profile.to_dict() for role, profile in self.profiles.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "AgentConfig":
        defaults = cls.defaults().profiles
        raw_profiles = data.get("profiles", {})
        configured = raw_profiles if isinstance(raw_profiles, dict) else {}
        profiles = {
            role: AgentProfile.from_dict(role, configured.get(role, {}) if isinstance(configured.get(role), dict) else {})
            for role in defaults
        }
        return cls(profiles=profiles, schema_version=str(data.get("schema_version") or "1.0"))


@dataclass(slots=True)
class AgentConfigStore:
    path: Path | str = ".agent_orchestrator/agent-config.json"

    def read(self) -> AgentConfig:
        path = Path(self.path)
        if not path.exists():
            return AgentConfig.defaults()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return AgentConfig.defaults()
        return AgentConfig.from_dict(payload if isinstance(payload, dict) else {})

    def write(self, config: AgentConfig) -> AgentConfig:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            tmp.write(json.dumps(config.to_dict(), ensure_ascii=False, indent=2))
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)
        return config


def _default_provider(role: str) -> str:
    return AgentConfig.defaults().profiles[role].provider if role in AgentConfig.defaults().profiles else "mock"


def _provider(value: object, *, default: str) -> str:
    provider = str(value or default)
    return provider if provider in {"codex", "claude", "mock"} else default


def _optional_string(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _runtime_mode(value: object) -> RuntimeMode:
    mode = str(value or "cli_inherit")
    return mode if mode in {"cli_inherit", "cli_isolated", "direct_api"} else "cli_inherit"  # type: ignore[return-value]
