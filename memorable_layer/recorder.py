"""The two calls an agent actually makes: brief before, record after.

Everything else in this package is plumbing behind these.
"""

from __future__ import annotations

from typing import Any

from .advisor import Advisor, AttemptBrief, baseline_params
from .client import MemorableClient
from .config import Settings
from .episode import Episode
from .store import EpisodeStore
from .trace import build_trace


class AttemptRecorder:
    """One agent, one scope, one race."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: EpisodeStore | None = None,
        client: MemorableClient | None = None,
    ):
        self.settings = settings or Settings.from_env()
        self.store = store or EpisodeStore(self.settings.store_root)
        self.client = client if client is not None else MemorableClient(self.settings)
        self.advisor = Advisor(self.store, self.client, self.settings)

    # -- before an attempt -------------------------------------------------

    def brief(
        self,
        strategy: str,
        trash_type: str,
        bin_distance_cm: float,
        bin_bearing_deg: float = 0.0,
        params: dict[str, float] | None = None,
    ) -> AttemptBrief:
        """Recall similar past attempts and suggest the next parameters."""
        return self.advisor.brief(
            strategy=strategy,
            trash_type=trash_type,
            distance_cm=bin_distance_cm,
            bearing_deg=bin_bearing_deg,
            baseline_params=params or baseline_params(strategy, trash_type, bin_distance_cm),
        )

    # -- after an attempt --------------------------------------------------

    def record(self, episode: Episode, ingest: bool = True) -> dict[str, Any]:
        """Write the episode, and hand a successful attempt to Memorable.

        The episode log always gets the attempt. Memorable only gets the ones it
        will accept: the extraction service refuses a trace with no verified
        postcondition, so a miss is recorded here and kept as a dead end for the
        trace of whichever attempt finally lands.
        """
        self.store.append(episode)
        result: dict[str, Any] = {
            "episode_id": episode.episode_id,
            "success": episode.success,
            "situation": episode.situation(),
            "ingested": False,
            "slug": None,
            "refusal": None,
        }

        if not ingest or not episode.success or self.client is None:
            if not episode.success:
                result["refusal"] = "attempt did not land; kept as episode evidence only"
            return result

        dead_ends = [
            past
            for past in self.store.similar(
                episode.strategy,
                episode.trash_type,
                episode.bin_distance_cm,
                scope=episode.scope,
                bearing_deg=episode.bin_bearing_deg,
            )
            if not past.success
            and past.race_id == episode.race_id
            and past.attempt < episode.attempt
        ]
        dead_ends.sort(key=lambda e: e.attempt)

        ingest_result = self.client.ingest(build_trace(episode, dead_ends=dead_ends))
        self.store.update_memorable_result(
            episode, ingest_result.slug, ingest_result.refusal
        )
        result.update(
            ingested=ingest_result.stored,
            slug=ingest_result.slug,
            refusal=ingest_result.refusal,
            dead_ends=[e.attempt for e in dead_ends],
        )
        return result


def open_recorder(**overrides: Any) -> AttemptRecorder:
    """Convenience constructor for use inside a sandbox."""
    settings = Settings.from_env()
    if overrides:
        settings = Settings(**{**settings.__dict__, **overrides})
    return AttemptRecorder(settings=settings)
