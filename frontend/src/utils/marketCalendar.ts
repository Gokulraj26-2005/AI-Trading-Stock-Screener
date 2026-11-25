/**
 * Market Calendar Utilities
 * Generates forward trading days (excluding weekends and statutory holidays)
 * for forward projection series in Lightweight Charts.
 */

// Recognized statutory NSE holidays (can be augmented dynamically)
export const KNOWN_NSE_HOLIDAYS = new Set<string>([
  '2026-01-26', // Republic Day
  '2026-03-03', // Holi
  '2026-03-20', // Id-Ul-Fitr
  '2026-04-03', // Good Friday
  '2026-04-14', // Dr. Ambedkar Jayanti
  '2026-05-01', // Maharashtra Day
  '2026-08-15', // Independence Day
  '2026-09-04', // Janmashtami
  '2026-10-02', // Mahatma Gandhi Jayanti
  '2026-10-20', // Dussehra
  '2026-11-09', // Diwali Laxmi Pujan
  '2026-11-24', // Guru Nanak Jayanti
  '2026-12-25', // Christmas
]);

/**
 * Generates N forward exchange trading days starting immediately after the reference date.
 * Returns either Unix timestamp in seconds (if input is number) or YYYY-MM-DD string (if input is string).
 */
export function generateFutureTradingDays(
  lastDateOrTimestamp: string | number,
  horizon: number = 20,
  holidays: Set<string> = KNOWN_NSE_HOLIDAYS
): Array<number | string> {
  const results: Array<number | string> = [];
  const isNumeric = typeof lastDateOrTimestamp === 'number';

  let current: Date;
  if (isNumeric) {
    current = new Date(lastDateOrTimestamp * 1000);
  } else {
    current = new Date(lastDateOrTimestamp);
  }

  while (results.length < horizon) {
    current.setDate(current.getDate() + 1);
    const dayOfWeek = current.getDay();
    const isoDate = current.toISOString().split('T')[0];

    // Exclude weekends (Saturday = 6, Sunday = 0) and exchange holidays
    if (dayOfWeek !== 0 && dayOfWeek !== 6 && !holidays.has(isoDate)) {
      if (isNumeric) {
        // Anchor to 09:15:00 IST (03:45:00 UTC)
        const tradingSessionAnchor = new Date(current);
        tradingSessionAnchor.setUTCHours(3, 45, 0, 0);
        results.push(Math.floor(tradingSessionAnchor.getTime() / 1000));
      } else {
        results.push(isoDate);
      }
    }
  }

  return results;
}
