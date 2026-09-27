// Boundary with Memorable (teammate's side). GBrain code only talks to this
// interface, so swapping the fixture for the real client is a one-file change.

import type { Episode } from "./types.ts";
import { standingsFrom } from "./verdict.ts";

export interface MemorableClient {
  /** race_episodes(race_id): the winner's successes + the losers' failures for one race. */
  raceEpisodes(raceId: string): Promise<Episode[]>;
  /** The structured episode table (clean numbers), optionally filtered. Feeds skillopt. */
  episodeTable(filter?: { raceIds?: string[] }): Promise<Episode[]>;
}

/**
 * Map one row of Memorable's table to an Episode. Column names here are
 * placeholders until the real schema is confirmed.
 */
export function fromRow(row: Record<string, any>): Episode {
  return {
    episode_id: String(row.episode_id),
    race_id: String(row.race_id),
    agent_id: String(row.agent_id),
    strategy: String(row.strategy),
    attempt: Number(row.attempt),
    trash_type: String(row.trash_type),
    bin_position: {
      distance_m: Number(row.bin_distance_m ?? row.bin_position?.distance_m),
      angle_deg: Number(row.bin_angle_deg ?? row.bin_position?.angle_deg),
    },
    params: {
      release_height_m: Number(row.release_height_m ?? row.params?.release_height_m),
      toss_velocity_mps: Number(row.toss_velocity_mps ?? row.params?.toss_velocity_mps),
      grasp_angle_deg: Number(row.grasp_angle_deg ?? row.params?.grasp_angle_deg),
    },
    outcome: row.outcome,
    score: Number(row.score),
    reason: String(row.reason ?? ""),
  };
}

/** Offline stand-in: reads every episode from a JSON array file. */
export class FileMemorable implements MemorableClient {
  constructor(private path: string) {}

  private async all(): Promise<Episode[]> {
    const rows = (await Bun.file(this.path).json()) as Record<string, any>[];
    return rows.map(fromRow);
  }

  async raceEpisodes(raceId: string): Promise<Episode[]> {
    const race = (await this.all()).filter((e) => e.race_id === raceId);
    const winner = standingsFrom(race)[0];
    // Same contract as the real call: winner's successes + everyone else's failures.
    return race.filter((e) =>
      e.agent_id === winner?.agent_id ? e.outcome === "in_bin" : e.outcome !== "in_bin",
    );
  }

  async episodeTable(filter?: { raceIds?: string[] }): Promise<Episode[]> {
    const rows = await this.all();
    return filter?.raceIds ? rows.filter((e) => filter.raceIds!.includes(e.race_id)) : rows;
  }
}
