"""Loads master.yaml + every account YAML into typed config objects.

Resolves `*_env` indirection — any key ending in `_env` is treated as the name
of an environment variable; the loader replaces it with the resolved value
under the same key minus the `_env` suffix. Missing env vars surface as
`MissingSecretError` at startup, not at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class MissingSecretError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountConfig:
    handle: str
    display_name: str
    niche: str
    enabled: bool
    persona: dict[str, Any]
    video_style: str
    post_frequency: int
    monetization: dict[str, Any]
    scout: dict[str, Any]
    hooks: dict[str, Any]
    secrets: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def output_dir(self) -> Path:
        return Path("data") / "trends" / self.handle


@dataclass(frozen=True)
class MasterConfig:
    timezone: str
    schedule: dict[str, str]
    models: dict[str, dict[str, str]]
    scout: dict[str, Any]
    scriptwriter: dict[str, Any]
    videogen: dict[str, Any]
    editor: dict[str, Any]
    monitor: dict[str, Any]
    logging: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineConfig:
    master: MasterConfig
    accounts: list[AccountConfig]

    def account(self, handle: str) -> AccountConfig:
        for a in self.accounts:
            if a.handle == handle:
                return a
        raise KeyError(handle)


def _resolve_env(node: Any, secrets: dict[str, str]) -> Any:
    """Walk a dict/list tree, replacing any `<key>_env: VAR_NAME` with the
    resolved value under `<key>`. Records the resolved secret in `secrets`.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if isinstance(k, str) and k.endswith("_env") and isinstance(v, str):
                resolved = os.environ.get(v)
                if resolved is None:
                    raise MissingSecretError(f"env var {v!r} (referenced by {k}) is not set")
                base = k[: -len("_env")]
                out[base] = resolved
                secrets[v] = resolved
            else:
                out[k] = _resolve_env(v, secrets)
        return out
    if isinstance(node, list):
        return [_resolve_env(item, secrets) for item in node]
    return node


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def load_master(path: Path) -> MasterConfig:
    raw = _load_yaml(path)
    return MasterConfig(
        timezone=raw["timezone"],
        schedule=raw["schedule"],
        models=raw["models"],
        scout=raw.get("scout", {}),
        scriptwriter=raw.get("scriptwriter", {}),
        videogen=raw.get("videogen", {}),
        editor=raw.get("editor", {}),
        monitor=raw.get("monitor", {}),
        logging=raw.get("logging", {}),
        raw=raw,
    )


def load_account(path: Path) -> AccountConfig:
    raw = _load_yaml(path)
    # Disabled accounts skip env-resolution entirely so an operator can flip
    # `enabled: false` on an account without ALSO scrubbing its env vars.
    # Otherwise startup raises MissingSecretError on a perfectly valid
    # "this account is paused" state.
    if not bool(raw.get("enabled", True)):
        return AccountConfig(
            handle=raw["handle"],
            display_name=raw.get("display_name", raw["handle"]),
            niche=raw.get("niche", ""),
            enabled=False,
            persona=raw.get("persona", {}),
            video_style=raw.get("video_style", ""),
            post_frequency=int(raw.get("post_frequency", 0)),
            monetization=raw.get("monetization", {}),
            scout=raw.get("scout", {}),
            hooks=raw.get("hooks", {}),
            secrets={},
            raw=raw,
        )

    secrets: dict[str, str] = {}
    resolved = _resolve_env(raw, secrets)
    return AccountConfig(
        handle=resolved["handle"],
        display_name=resolved["display_name"],
        niche=resolved["niche"],
        enabled=True,
        persona=resolved["persona"],
        video_style=resolved["video_style"],
        post_frequency=int(resolved["post_frequency"]),
        monetization=resolved["monetization"],
        scout=resolved.get("scout", {}),
        hooks=resolved.get("hooks", {}),
        secrets=secrets,
        raw=resolved,
    )


def load_pipeline(config_root: Path) -> PipelineConfig:
    master = load_master(config_root / "master.yaml")
    accounts_dir = config_root / "accounts"
    accounts = [
        load_account(p)
        for p in sorted(accounts_dir.glob("*.yaml"))
    ]
    enabled = [a for a in accounts if a.enabled]
    return PipelineConfig(master=master, accounts=enabled)
