/**
 * Compact Arabic-friendly timestamp formatter used in case sidebar
 * cards across the medic and hospital portals.
 *
 * Output examples:
 *   - same day:     "اليوم 04:03 ص"
 *   - day before:   "أمس 23:17"
 *   - older:        "23/05 04:03 ص"
 *
 * The output is intentionally short — the card has 2-line vertical
 * budget. Year is omitted because the detail header already shows
 * full ``created_at`` formatting.
 */
export function formatCaseDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";

  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();

  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const isYesterday =
    d.getFullYear() === yesterday.getFullYear() &&
    d.getMonth() === yesterday.getMonth() &&
    d.getDate() === yesterday.getDate();

  const time = d.toLocaleTimeString("ar-SA", {
    hour: "2-digit",
    minute: "2-digit",
  });
  if (sameDay) return `اليوم ${time}`;
  if (isYesterday) return `أمس ${time}`;

  const dd = String(d.getDate()).padStart(2, "0");
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  return `${dd}/${mm} ${time}`;
}
