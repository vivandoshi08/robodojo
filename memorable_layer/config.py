"""Environment wiring for the Memorable layer.

One QM scope == one Memorable namespace. Isolation is enforced with
``MEMORABLE_HOME``: the CLI resolves its store to
``$MEMORABLE_HOME/.memorable/procedures.jsonl``, so a distinct home per scope
means no competitor can recall another's attempts mid-race.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_S = 25.0

#: Strategies raced against each other. Adding one is adding a SKILL.md.
STRATEGIES = ("pick_place", "drop", "toss", "push_off_edge")

#: Trash types the bin position and physics are randomized over.
TRASH_TYPES = ("paper", "bottle", "can")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Resolved per-sandbox configuration."""

    scope: str
    race_id: str
    store_root: Path
    memorable_home: Path | None
    binary: tuple[str, ...]
    writes_enabled: bool
    timeout_s: float

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)

        scope = (
            env.get("ROBODOJO_SCOPE")
            or env.get("QM_SCOPE")
            or env.get("QM_SCOPE_ID")
            or socket.gethostname()
        )
        race_id = env.get("ROBODOJO_RACE_ID") or "local"

        home_raw = env.get("MEMORABLE_HOME")
        memorable_home = Path(home_raw).expanduser() if home_raw else None

        root_raw = env.get("ROBODOJO_MEMORY_ROOT")
        if root_raw:
            store_root = Path(root_raw).expanduser()
        elif memorable_home is not None:
            # Ride along with the namespace so the episode log is scoped too.
            store_root = memorable_home / ".robodojo"
        else:
            store_root = Path.cwd() / ".robodojo-memory"

        binary_raw = env.get("ROBODOJO_MEMORABLE_BIN")
        binary = tuple(binary_raw.split()) if binary_raw else ("memorable",)

        return cls(
            scope=scope,
            race_id=race_id,
            store_root=store_root,
            memorable_home=memorable_home,
            binary=binary,
            # Fail-closed like Memorable's own consent model: writes are on by
            # default here, but one env var takes the layer fully read-only.
            writes_enabled=not _truthy(env.get("ROBODOJO_MEMORABLE_READONLY")),
            timeout_s=float(env.get("ROBODOJO_MEMORABLE_TIMEOUT") or DEFAULT_TIMEOUT_S),
        )

    def child_env(self) -> dict[str, str]:
        """Environment for the ``memorable`` subprocess, namespace pinned."""
        env = dict(os.environ)
        if self.memorable_home is not None:
            env["MEMORABLE_HOME"] = str(self.memorable_home)
        # Traces are handed over explicitly; never let the CLI intercept.
        env["MEMORABLE_AUTO_INGEST"] = "0"
        return env


def scope_home(base: Path, scope: str) -> Path:
    """Namespace directory for ``scope`` under ``base``.

    Used when several scopes share a machine (local dev, or a referee that
    replays every competitor's store after the verdict).
    """
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in scope)
    return base / f"scope-{safe}"
