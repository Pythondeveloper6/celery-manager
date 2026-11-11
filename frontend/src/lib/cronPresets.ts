// Common crontab shapes for the schedule editor. Picking one fills the five
// fields; the fields stay editable, and a field set that matches no preset
// reads as "Custom".

export interface CronFieldValues {
  minute: string;
  hour: string;
  day_of_month: string;
  month_of_year: string;
  day_of_week: string;
}

export interface CronPreset {
  id: string;
  label: string;
  fields: CronFieldValues;
}

const f = (
  minute: string,
  hour = "*",
  day_of_month = "*",
  month_of_year = "*",
  day_of_week = "*",
): CronFieldValues => ({ minute, hour, day_of_month, month_of_year, day_of_week });

export const CRON_PRESETS: readonly CronPreset[] = [
  { id: "every-minute", label: "Every minute", fields: f("*") },
  { id: "every-5-minutes", label: "Every 5 minutes", fields: f("*/5") },
  { id: "every-15-minutes", label: "Every 15 minutes", fields: f("*/15") },
  { id: "hourly", label: "Every hour, on the hour", fields: f("0") },
  { id: "daily-midnight", label: "Every day at midnight", fields: f("0", "0") },
  { id: "daily-9", label: "Every day at 09:00", fields: f("0", "9") },
  { id: "weekdays-9", label: "Weekdays at 09:00", fields: f("0", "9", "*", "*", "mon-fri") },
  { id: "weekly-monday", label: "Every Monday at 09:00", fields: f("0", "9", "*", "*", "mon") },
  { id: "monthly-first", label: "First day of the month at 00:00", fields: f("0", "0", "1") },
];

export const CUSTOM_PRESET = "custom";

/** The preset whose fields equal `fields` (ignoring surrounding spaces), or "custom". */
export function presetIdFor(fields: CronFieldValues): string {
  const clean = (v: string) => v.trim() || "*";
  const hit = CRON_PRESETS.find((p) =>
    (Object.keys(p.fields) as (keyof CronFieldValues)[]).every(
      (k) => clean(fields[k]) === p.fields[k],
    ),
  );
  return hit ? hit.id : CUSTOM_PRESET;
}

export function presetFields(id: string): CronFieldValues | null {
  const hit = CRON_PRESETS.find((p) => p.id === id);
  return hit ? { ...hit.fields } : null;
}
