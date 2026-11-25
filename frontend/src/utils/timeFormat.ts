/**
 * Indian Standard Time (IST - Asia/Kolkata, UTC+5:30) Formatting Utilities
 */

export const IST_TIMEZONE = 'Asia/Kolkata';
export const IST_LOCALE = 'en-IN';

/**
 * Formats a Date, string timestamp, or numeric Unix timestamp (seconds or ms)
 * to 24-hour IST time string, e.g. "09:15:00" or "09:15".
 */
export function formatISTTime(
  value?: Date | string | number | null,
  includeSeconds: boolean = false
): string {
  if (!value) return '—';
  const ts = typeof value === 'number'
    ? (value < 1e11 ? value * 1000 : value)
    : value;
  const d = new Date(ts);
  if (isNaN(d.getTime())) return '—';

  return d.toLocaleTimeString(IST_LOCALE, {
    timeZone: IST_TIMEZONE,
    hour: '2-digit',
    minute: '2-digit',
    second: includeSeconds ? '2-digit' : undefined,
    hour12: false,
  });
}

/**
 * Formats a Date, string timestamp, or numeric Unix timestamp (seconds or ms)
 * to full date-time in IST, e.g. "07 Sep, 09:15 IST".
 */
export function formatISTDateTime(
  value?: Date | string | number | null,
  includeSeconds: boolean = false
): string {
  if (!value) return '—';
  const ts = typeof value === 'number'
    ? (value < 1e11 ? value * 1000 : value)
    : value;
  const d = new Date(ts);
  if (isNaN(d.getTime())) return '—';

  const datePart = d.toLocaleDateString(IST_LOCALE, {
    timeZone: IST_TIMEZONE,
    day: '2-digit',
    month: 'short',
  });
  const timePart = d.toLocaleTimeString(IST_LOCALE, {
    timeZone: IST_TIMEZONE,
    hour: '2-digit',
    minute: '2-digit',
    second: includeSeconds ? '2-digit' : undefined,
    hour12: false,
  });

  return `${datePart} ${timePart} IST`;
}
