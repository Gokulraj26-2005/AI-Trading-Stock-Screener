// frontend/src/utils/analysisStorage.ts
/**
 * Persistent Storage Layer for Multi-Timeframe Chart Analysis.
 * Uses native IndexedDB to store full multi-timeframe candles, indicators, and commentary
 * without hitting browser localStorage 5MB quota limits.
 * Falls back to localStorage with lightweight metadata when necessary.
 */

const DB_NAME = 'via_chart_analysis_db';
const STORE_NAME = 'analysis_cache';
const DB_VERSION = 1;

export interface PersistedAnalysisCache {
  timestamp: string | null;
  selectedSymbol: string | null;
  activeTimeframe: '1D' | '1W' | '1M';
  universe: string;
  stocks: Record<string, any>;
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof window === 'undefined' || !window.indexedDB) {
      return reject(new Error('IndexedDB not supported'));
    }

    const request = window.indexedDB.open(DB_NAME, DB_VERSION);

    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME);
      }
    };

    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/**
 * Save full chart analysis cache to IndexedDB + fallback metadata to localStorage.
 */
export async function saveAnalysisCache(cache: PersistedAnalysisCache): Promise<void> {
  if (typeof window === 'undefined') return;

  // 1. Primary storage: IndexedDB (handles gigabytes, no quota crash)
  try {
    const db = await openDatabase();
    const tx = db.transaction(STORE_NAME, 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    store.put(cache, 'latest');

    await new Promise((resolve, reject) => {
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  } catch (err) {
    console.warn('[analysisStorage] IndexedDB save failed, falling back to localStorage:', err);
  }

  // 2. Secondary fallback: localStorage (stores metadata + symbol list so UI always loads immediately)
  try {
    const lightweightStocks: Record<string, any> = {};
    for (const [sym, payload] of Object.entries(cache.stocks)) {
      if (!payload) continue;
      lightweightStocks[sym] = {
        symbol: payload.symbol,
        current_price: payload.current_price,
        commentary: payload.commentary,
        markers: payload.markers,
        price_lines: payload.price_lines,
        // Keep only the most recent 30 candles for instant preview if IndexedDB is slow
        timeframes: {
          '1D': {
            candles: payload.timeframes?.['1D']?.candles?.slice(-30) || [],
          },
        },
      };
    }

    localStorage.setItem(
      'via_chart_analysis_meta_v3',
      JSON.stringify({
        timestamp: cache.timestamp,
        selectedSymbol: cache.selectedSymbol,
        activeTimeframe: cache.activeTimeframe,
        universe: cache.universe,
        stocks: lightweightStocks,
      })
    );
  } catch (_quotaErr) {
    // Quota reached on localStorage — harmless since IndexedDB has full data
  }
}

/**
 * Load full chart analysis cache from IndexedDB, with graceful fallback to localStorage.
 */
export async function loadAnalysisCache(): Promise<PersistedAnalysisCache | null> {
  if (typeof window === 'undefined') return null;

  // 1. Try IndexedDB first
  try {
    const db = await openDatabase();
    const tx = db.transaction(STORE_NAME, 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const req = store.get('latest');

    const result = await new Promise<any>((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });

    if (result && result.stocks && Object.keys(result.stocks).length > 0) {
      return result as PersistedAnalysisCache;
    }
  } catch (err) {
    console.warn('[analysisStorage] IndexedDB read failed, trying localStorage:', err);
  }

  // 2. Fallback to localStorage meta
  try {
    const raw =
      localStorage.getItem('via_chart_analysis_meta_v3') ||
      localStorage.getItem('via_chart_analysis_cache_v2');

    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed.stocks && Object.keys(parsed.stocks).length > 0) {
        return {
          timestamp: parsed.timestamp || null,
          selectedSymbol: parsed.selectedSymbol || Object.keys(parsed.stocks)[0] || null,
          activeTimeframe: parsed.activeTimeframe || '1D',
          universe: parsed.universe || 'all',
          stocks: parsed.stocks,
        };
      }
    }
  } catch (_e) {
    // ignore
  }

  return null;
}
