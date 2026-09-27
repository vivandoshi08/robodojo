"""Episode log: append-only JSONL plus a SQLite mirror.

Memorable holds the *procedure* — how a throw that worked was executed. It is
deliberately picky: the extraction service refuses a trace with no verified
postcondition, so failed attempts do not become procedures. This store is
therefore the source of truth for numbers: every attempt lands here, pass or
fail, which is what scoring, filtering and ``gbrain skillopt`` need.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .episode import Episode, Outcome, bearing_bucket, distance_bucket

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    episode_id       TEXT PRIMARY KEY,
    race_id          TEXT NOT NULL,
    scope            TEXT NOT NULL,
    strategy         TEXT NOT NULL,
    attempt          INTEGER NOT NULL,
    trash_type       TEXT NOT NULL,
    bin_distance_cm  REAL NOT NULL,
    distance_bucket  INTEGER NOT NULL,
    bin_bearing_deg  REAL NOT NULL,
    bearing_bucket   INTEGER NOT NULL,
    outcome          TEXT NOT NULL,
    success          INTEGER NOT NULL,
    bin_knocked_over INTEGER NOT NULL,
    duration_s       REAL,
    seed             INTEGER,
    sim              INTEGER NOT NULL,
    reason           TEXT NOT NULL,
    params_json      TEXT NOT NULL,
    memorable_slug   TEXT,
    memorable_refusal TEXT,
    recorded_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS episodes_race ON episodes(race_id);
CREATE INDEX IF NOT EXISTS episodes_situation
    ON episodes(strategy, trash_type, distance_bucket, bearing_bucket);
"""


class EpisodeStore:
    """Per-scope episode log. Cheap to open, safe to open concurrently."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.root / "episodes.jsonl"
        self.db_path = self.root / "episodes.db"
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        """Commit on success, always close.

        ``with sqlite3.connect(...)`` commits but leaves the handle open, which
        leaks a connection per call.
        """
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- writes -----------------------------------------------------------

    def append(self, episode: Episode) -> Episode:
        """Record one attempt. JSONL first, so a SQLite failure cannot lose it."""
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(episode.to_json() + "\n")
        self._upsert(episode)
        return episode

    def update_memorable_result(
        self, episode: Episode, slug: str | None, refusal: str | None
    ) -> None:
        """Attach the Memorable outcome once ingest has answered."""
        episode.memorable_slug = slug
        episode.memorable_refusal = refusal
        with self._connect() as conn:
            conn.execute(
                "UPDATE episodes SET memorable_slug = ?, memorable_refusal = ? "
                "WHERE episode_id = ?",
                (slug, refusal, episode.episode_id),
            )

    def _upsert(self, episode: Episode) -> None:
        data = episode.to_dict()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO episodes (
                    episode_id, race_id, scope, strategy, attempt, trash_type,
                    bin_distance_cm, distance_bucket, bin_bearing_deg, bearing_bucket,
                    outcome, success,
                    bin_knocked_over, duration_s, seed, sim, reason, params_json,
                    memorable_slug, memorable_refusal, recorded_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    data["episode_id"], data["race_id"], data["scope"],
                    data["strategy"], data["attempt"], data["trash_type"],
                    data["bin_distance_cm"], distance_bucket(episode.bin_distance_cm),
                    data["bin_bearing_deg"], bearing_bucket(episode.bin_bearing_deg),
                    data["outcome"], int(episode.success),
                    int(episode.bin_knocked_over), data["duration_s"], data["seed"],
                    int(episode.sim), data["reason"],
                    json.dumps(episode.params, sort_keys=True),
                    data["memorable_slug"], data["memorable_refusal"],
                    data["recorded_at"],
                ),
            )

    def merge_from(self, other_root: Path) -> int:
        """Absorb another scope's log. How the referee sees the whole race."""
        source = Path(other_root) / "episodes.jsonl"
        if not source.exists():
            return 0
        added = 0
        known = {row["episode_id"] for row in self._rows("SELECT episode_id FROM episodes")}
        with source.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                episode = Episode.from_dict(json.loads(line))
                if episode.episode_id in known:
                    continue
                self.append(episode)
                known.add(episode.episode_id)
                added += 1
        return added

    # -- reads ------------------------------------------------------------

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(conn.execute(sql, params))

    @staticmethod
    def _to_episode(row: sqlite3.Row) -> Episode:
        return Episode(
            race_id=row["race_id"], scope=row["scope"], strategy=row["strategy"],
            attempt=row["attempt"], trash_type=row["trash_type"],
            bin_distance_cm=row["bin_distance_cm"], params=json.loads(row["params_json"]),
            bin_bearing_deg=row["bin_bearing_deg"],
            outcome=Outcome(row["outcome"]), reason=row["reason"],
            bin_knocked_over=bool(row["bin_knocked_over"]), duration_s=row["duration_s"],
            seed=row["seed"], sim=bool(row["sim"]), recorded_at=row["recorded_at"],
            memorable_slug=row["memorable_slug"], memorable_refusal=row["memorable_refusal"],
        )

    def __iter__(self) -> Iterator[Episode]:
        for row in self._rows("SELECT * FROM episodes ORDER BY recorded_at, attempt"):
            yield self._to_episode(row)

    def race_episodes(self, race_id: str, strategy: str | None = None) -> list[Episode]:
        """Every attempt in one race — the evidence the referee links from GBrain."""
        sql = "SELECT * FROM episodes WHERE race_id = ?"
        params: tuple = (race_id,)
        if strategy is not None:
            sql += " AND strategy = ?"
            params += (strategy,)
        sql += " ORDER BY strategy, attempt"
        return [self._to_episode(row) for row in self._rows(sql, params)]

    def similar(
        self,
        strategy: str,
        trash_type: str,
        distance_cm: float,
        tolerance_cm: float = 10.0,
        scope: str | None = None,
        limit: int = 20,
        bearing_deg: float | None = None,
        bearing_tolerance_deg: float = 20.0,
    ) -> list[Episode]:
        """Past attempts at the same situation, newest first.

        Scope-filtered on purpose: mid-race, an agent may only read its own
        namespace.
        """
        sql = (
            "SELECT * FROM episodes WHERE strategy = ? AND trash_type = ? "
            "AND ABS(bin_distance_cm - ?) <= ?"
        )
        params: tuple = (strategy, trash_type, distance_cm, tolerance_cm)
        if bearing_deg is not None:
            sql += " AND ABS(bin_bearing_deg - ?) <= ?"
            params += (bearing_deg, bearing_tolerance_deg)
        if scope is not None:
            sql += " AND scope = ?"
            params += (scope,)
        sql += " ORDER BY recorded_at DESC, attempt DESC LIMIT ?"
        return [self._to_episode(row) for row in self._rows(sql, params + (limit,))]

    def attempts_so_far(self, race_id: str, scope: str, strategy: str) -> int:
        rows = self._rows(
            "SELECT COUNT(*) AS n FROM episodes WHERE race_id = ? AND scope = ? "
            "AND strategy = ?",
            (race_id, scope, strategy),
        )
        return int(rows[0]["n"]) if rows else 0

    def next_attempt_number(self, race_id: str, scope: str, strategy: str) -> int:
        return self.attempts_so_far(race_id, scope, strategy) + 1

    def score_rows(self, race_id: str) -> list[dict]:
        """Flat numeric rows: what ``gbrain skillopt`` consumes."""
        rows = self._rows(
            """
            SELECT strategy,
                   trash_type,
                   distance_bucket,
                   ROUND(distance_bucket / 100.0, 3)           AS distance_m,
                   bearing_bucket,
                   COUNT(*)                                   AS attempts,
                   SUM(success)                               AS successes,
                   ROUND(AVG(success), 4)                     AS success_rate,
                   SUM(bin_knocked_over)                      AS knockovers,
                   ROUND(AVG(duration_s), 3)                  AS mean_duration_s,
                   MIN(CASE WHEN success = 1 THEN attempt END) AS first_success_attempt
            FROM episodes
            WHERE race_id = ?
            GROUP BY strategy, trash_type, distance_bucket, bearing_bucket
            ORDER BY success_rate DESC, mean_duration_s
            """,
            (race_id,),
        )
        return [dict(row) for row in rows]

    def races(self) -> list[str]:
        return [
            row["race_id"]
            for row in self._rows(
                "SELECT race_id, MIN(recorded_at) AS t FROM episodes "
                "GROUP BY race_id ORDER BY t"
            )
        ]

    def extend(self, episodes: Iterable[Episode]) -> int:
        return sum(1 for episode in episodes if self.append(episode))
