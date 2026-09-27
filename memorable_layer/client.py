"""Thin subprocess wrapper around the ``memorable`` CLI.

Deliberately a subprocess and not an HTTP client: the CLI owns login, the
encrypted local store, consent and the backend choice (local, gbrain, or QM
Postgres), and it is what runs inside a QM sandbox via ``execute``.

Reads fail open — a missing binary, a cold store or a timeout returns empty
rather than raising, so the memory layer can never break a race or the demo.
Writes fail closed and loudly in the return value, mirroring Memorable's own
consent model.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from .config import Settings

_RECALL_LINE = re.compile(r"^\s*(?P<score>\d+\.\d+)\s+(?P<slug>\S+)\s+\[(?P<method>\w+)\]")
_STORED = re.compile(r"stored\s+(?P<slug>procedures/\S+?)(?:[,\s]|$)")
_NOT_STORED = re.compile(r"not stored:\s*(?P<reason>.+)$")


@dataclass
class Recollection:
    score: float
    slug: str
    method: str  # exact | lexical | semantic

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "slug": self.slug, "method": self.method}


@dataclass
class IngestResult:
    stored: bool
    slug: str | None = None
    refusal: str | None = None
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"stored": self.stored, "slug": self.slug, "refusal": self.refusal}


@dataclass
class CommandResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


@dataclass
class MemorableClient:
    settings: Settings
    _available: bool | None = field(default=None, init=False, repr=False)

    # -- plumbing ---------------------------------------------------------

    def _run(self, args: list[str], stdin: str | None = None) -> CommandResult:
        try:
            proc = subprocess.run(
                [*self.settings.binary, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.settings.timeout_s,
                env=self.settings.child_env(),
            )
        except FileNotFoundError:
            return CommandResult(ok=False, error="memorable CLI not on PATH")
        except subprocess.TimeoutExpired:
            return CommandResult(ok=False, error="memorable CLI timed out")
        except OSError as exc:  # pragma: no cover - environment dependent
            return CommandResult(ok=False, error=str(exc))
        return CommandResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            error=None if proc.returncode == 0 else f"exit {proc.returncode}",
        )

    def available(self) -> bool:
        if self._available is None:
            self._available = self._run(["status"]).ok
        return self._available

    def status(self) -> dict[str, Any]:
        """Parsed highlights of ``memorable status``.

        The command prints for humans and ignores ``--json`` in 0.5.x, so this
        reads the few facts the layer needs and keeps the raw text.
        """
        result = self._run(["status"])
        text = result.stdout
        stored = re.search(r"stored\s+(\d+)\s+procedures", text)
        backend = re.search(r"backend\s+(\S+)", text)
        return {
            "available": result.ok,
            "backend": backend.group(1) if backend else None,
            "write_consent": "read-write" in text,
            "extraction_api": "extraction api  configured" in text
            or "extraction  configured" in text,
            "stored": int(stored.group(1)) if stored else 0,
            "namespace": str(self.settings.memorable_home) if self.settings.memorable_home else None,
            "error": result.error,
            "raw": text.strip(),
        }

    # -- writes -----------------------------------------------------------

    def ingest(self, trace: dict[str, Any]) -> IngestResult:
        """Hand a trace over. Returns the refusal reason rather than raising."""
        if not self.settings.writes_enabled:
            return IngestResult(stored=False, refusal="writes disabled for this layer")
        result = self._run(["ingest", "-"], stdin=json.dumps(trace))
        text = f"{result.stdout}\n{result.stderr}".strip()
        stored = _STORED.search(text)
        if stored:
            return IngestResult(stored=True, slug=stored.group("slug"), raw=text)
        refused = _NOT_STORED.search(text)
        reason = refused.group("reason").strip() if refused else (result.error or text or "unknown")
        return IngestResult(stored=False, refusal=reason, raw=text)

    # -- reads ------------------------------------------------------------

    def recall(self, query: str, limit: int = 3, mode: str | None = None) -> list[Recollection]:
        args = ["recall", query]
        if mode in {"single", "chain"}:
            args.append(f"--{mode}")
        result = self._run(args)
        if not result.ok:
            return []
        hits: list[Recollection] = []
        for line in result.stdout.splitlines():
            match = _RECALL_LINE.match(line)
            if match:
                hits.append(
                    Recollection(
                        score=float(match.group("score")),
                        slug=match.group("slug"),
                        method=match.group("method"),
                    )
                )
        return hits[:limit]

    def show(self, slug: str) -> str:
        """The guarded, injection-ready rendering of one procedure."""
        result = self._run(["show", slug])
        return result.stdout.strip() if result.ok else ""

    def list_procedures(self) -> list[dict[str, Any]]:
        """``memorable list --json``: revisions, recall counts, ok/fail tallies.

        This is a free cross-race scoreboard — how often a stored approach was
        recalled and how often things went well afterwards.
        """
        result = self._run(["list", "--json"])
        if not result.ok:
            return []
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
