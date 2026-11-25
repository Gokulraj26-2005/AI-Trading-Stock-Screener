import { describe, it, expect } from 'vitest';
import { generateFutureTradingDays } from './marketCalendar';

describe('generateFutureTradingDays', () => {
  it('generates ascending future trading days skipping weekends (timestamp input)', () => {
    // 2026-09-18 is Friday
    const friday = new Date('2026-09-18T15:30:00Z').getTime() / 1000;
    const days = generateFutureTradingDays(friday, 5) as number[];

    expect(days).toHaveLength(5);
    // All dates must be in ascending order
    for (let i = 1; i < days.length; i++) {
      expect(days[i]).toBeGreaterThan(days[i - 1]);
    }

    // Verify none of the generated days fall on Saturday or Sunday
    for (const ts of days) {
      const d = new Date(ts * 1000);
      const dayOfWeek = d.getUTCDay();
      expect(dayOfWeek).not.toBe(0); // Not Sunday
      expect(dayOfWeek).not.toBe(6); // Not Saturday
    }
  });

  it('skips statutory holidays if present in holiday set (ISO string input)', () => {
    // Friday
    const friday = '2026-09-18T00:00:00Z';
    const holidays = new Set(['2026-09-21']); // Monday is a holiday

    const days = generateFutureTradingDays(friday, 3, holidays) as string[];
    expect(days).toHaveLength(3);

    // First trading day should skip weekend (Sat 19, Sun 20) AND Mon 21 => Tue 22
    expect(days[0]).toBe('2026-09-22');
    expect(days[1]).toBe('2026-09-23');
    expect(days[2]).toBe('2026-09-24');
  });
});
