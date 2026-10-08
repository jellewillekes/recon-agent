// Number and error formatting shared by both views.

export function formatDuration(milliseconds: number | null | undefined): string {
  const value = Number(milliseconds);
  if (milliseconds == null || !Number.isFinite(value)) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

export function formatCount(value: number | null | undefined): string {
  return value != null && Number.isFinite(value) ? new Intl.NumberFormat().format(value) : "—";
}

export function formatCost(euros: number | null | undefined): string {
  if (euros == null || !Number.isFinite(euros)) return "—";
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 4,
  }).format(euros);
}

export function number(value: number | null | undefined, digits = 3): string {
  return value == null ? "—" : Number(value).toFixed(digits);
}

export function percent(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

/** The message to show for an API error body, or `fallback`. */
export function describeError(payload: unknown, fallback: string): string {
  const body = (payload ?? {}) as { detail?: unknown; fields?: unknown };
  if (typeof body.detail === "string") return body.detail;
  if (body.detail) return JSON.stringify(body.detail);
  if (Array.isArray(body.fields) && body.fields.length) {
    return `Check the question fields: ${body.fields.join(", ")}.`;
  }
  return fallback;
}
