import { config } from "./config.ts";
import type { Episode } from "./types.ts";

/** A trash type plus a snapped bin position: the unit a benchmark task is built from. */
export interface Scenario {
  key: string;
  trash_type: string;
  distance_m: number;
  angle_deg: number;
}

export const slugify = (s: string) =>
  s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

const snap = (v: number, step: number) => Math.round(v / step) * step;

export function scenarioOf(e: Episode): Scenario {
  const distance_m = snap(e.bin_position.distance_m, config.bucket.distanceM);
  const angle_deg = snap(e.bin_position.angle_deg, config.bucket.angleDeg) || 0; // no -0
  return {
    key: `${slugify(e.trash_type)}-d${distance_m.toFixed(1)}-a${angle_deg}`,
    trash_type: e.trash_type,
    distance_m,
    angle_deg,
  };
}

export function describeBin(s: Pick<Scenario, "distance_m" | "angle_deg">): string {
  const side =
    s.angle_deg === 0
      ? "straight ahead"
      : `${Math.abs(s.angle_deg)}° to the ${s.angle_deg < 0 ? "left" : "right"}`;
  return `${s.distance_m.toFixed(1)} m away, ${side}`;
}
