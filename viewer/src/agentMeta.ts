/**
 * Normalization helpers for agent role / reasoning_effort display.
 * Pure functions so the viewer can unit-test them without mounting React.
 */

/** Canonical role values from the worker contract. */
const ROLE_LABELS: Record<string, string> = {
  explore: "Explorer",
  implement: "Implementer",
  review: "Reviewer",
};

/**
 * Human label for a persisted agent role.
 * Unknown non-empty values fall back to a title-cased raw value;
 * empty / legacy NULL roles return null (nothing to show).
 */
export function roleLabel(role: string | null | undefined): string | null {
  const raw = (role || "").trim();
  if (!raw) return null;
  const known = ROLE_LABELS[raw.toLowerCase()];
  if (known) return known;
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

/**
 * Display value for a persisted reasoning_effort.
 * Empty values return null; configured values are shown verbatim
 * (runtime validates them, e.g. `max`).
 */
export function effortLabel(effort: string | null | undefined): string | null {
  const raw = (effort || "").trim();
  return raw || null;
}
