/** Cron helpers for the pipeline schedule editor (standard 5-field cron, evaluated server-side with croniter). */

export const CRON_PRESETS = [
  { value: "0 * * * *", label: "Every hour" },
  { value: "0 6 * * *", label: "Every day at 06:00" },
  { value: "0 6 * * 1-5", label: "Every weekday at 06:00" },
  { value: "0 6 * * 1", label: "Every Monday at 06:00" },
  { value: "0 6 1 * *", label: "First day of every month at 06:00" },
  { value: "*/15 * * * *", label: "Every 15 minutes" },
];

const FIELDS = [
  { name: "minute", min: 0, max: 59 },
  { name: "hour", min: 0, max: 23 },
  { name: "day of month", min: 1, max: 31 },
  { name: "month", min: 1, max: 12 },
  { name: "day of week", min: 0, max: 7 },
];

const DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function validPart(part: string, min: number, max: number): boolean {
  return part.split(",").every((item) => {
    const [range, step] = item.split("/");
    if (step !== undefined && !/^\d+$/.test(step)) return false;
    if (step !== undefined && Number(step) === 0) return false;
    if (range === "*") return true;
    const m = /^(\d+)(?:-(\d+))?$/.exec(range);
    if (!m) return false;
    const a = Number(m[1]);
    const b = m[2] !== undefined ? Number(m[2]) : a;
    return a >= min && b <= max && a <= b;
  });
}

/** Returns null when valid, otherwise a human message. */
export function validateCron(expr: string): string | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return "Use 5 fields: minute hour day-of-month month day-of-week.";
  for (let i = 0; i < 5; i++) {
    if (!validPart(parts[i], FIELDS[i].min, FIELDS[i].max)) return `Invalid ${FIELDS[i].name} field "${parts[i]}".`;
  }
  return null;
}

const pad = (n: string) => n.padStart(2, "0");

/** Plain-language description for common shapes; falls back to the raw expression. */
export function describeCron(expr: string, timezone?: string): string {
  if (validateCron(expr)) return expr;
  const [min, hour, dom, mon, dow] = expr.trim().split(/\s+/);
  const tz = timezone ? ` (${timezone})` : "";
  const preset = CRON_PRESETS.find((p) => p.value === expr.trim());
  if (preset) return `${preset.label}${tz}`;
  const at = /^\d+$/.test(min) && /^\d+$/.test(hour) ? `at ${pad(hour)}:${pad(min)}` : null;
  if (/^\*\/\d+$/.test(min) && hour === "*" && dom === "*" && mon === "*" && dow === "*") return `Every ${min.slice(2)} minutes${tz}`;
  if (/^\d+$/.test(min) && hour === "*" && dom === "*" && mon === "*" && dow === "*") return `Every hour at :${pad(min)}${tz}`;
  if (at && dom === "*" && mon === "*" && dow === "*") return `Every day ${at}${tz}`;
  if (at && dom === "*" && mon === "*" && /^\d$/.test(dow)) return `Every ${DOW[Number(dow)]} ${at}${tz}`;
  if (at && dom === "*" && mon === "*" && dow === "1-5") return `Every weekday ${at}${tz}`;
  if (at && /^\d+$/.test(dom) && mon === "*" && dow === "*") return `Day ${dom} of every month ${at}${tz}`;
  return `${expr}${tz}`;
}
