import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertCircle,
  ArrowDown,
  ArrowDownRight,
  ArrowUp,
  ArrowUpDown,
  ArrowUpRight,
  BarChart2,
  CheckCircle2,
  Clock,
  ExternalLink,
  FileText,
  Flame,
  Info,
  Newspaper,
  Pause,
  Play,
  Power,
  RefreshCw,
  Search,
  ShieldAlert,
  TrendingDown,
  TrendingUp,
  X,
  Zap,
} from 'lucide-react';
import {
  FinBERTNewsResponse,
  LiveShortStock,
  LiveShortScreenerResponse,
  getLiveShortScreenerStocks,
  getShortScreenerNews,
  getShortScreenerPower,
  setShortScreenerPower,
} from '../api';

interface ShortScreenerLivePageProps {
  token?: string;
  onOpenChart?: (symbol: string) => void;
  onNavigateToShortsDesk?: () => void;
}

export function ShortScreenerLivePage({
  token: _token,
  onOpenChart,
  onNavigateToShortsDesk: _onNavigateToShortsDesk,
}: ShortScreenerLivePageProps) {
  // ── State ───────────────────────────────────────────────────────────────
  const [data, setData] = useState<LiveShortScreenerResponse | null>(null);
  const [isActive, setIsActive] = useState<boolean>(false);
  const [isLoading, setIsLoading] = useState<boolean>(true);

  // Sorting State
  type SortField =
    | 'rank'
    | 'symbol'
    | 'ltp'
    | 'change_pct'
    | 'rsi'
    | 'rsi_velocity'
    | 'news_catalyst_score'
    | 'ifp_score';
  type SortDirection = 'asc' | 'desc';

  const [sortField, setSortField] = useState<SortField>('rsi');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection((prev) => (prev === 'desc' ? 'asc' : 'desc'));
    } else {
      setSortField(field);
      if (field === 'symbol' || field === 'rank') {
        setSortDirection('asc');
      } else {
        setSortDirection('desc');
      }
    }
  };
  const [isPowerToggling, setIsPowerToggling] = useState<boolean>(false);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [wsConnected, setWsConnected] = useState<boolean>(false);
  const [lastTickTime, setLastTickTime] = useState<string>('');

  // Track price flash animations: sym -> 'up' | 'down'
  const [priceFlashes, setPriceFlashes] = useState<Record<string, 'up' | 'down'>>({});
  const prevPricesRef = useRef<Record<string, number>>({});
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);

  // ── 24h FinBERT News Breakdown Sidebar State ────────────────────────────
  const [selectedNewsSymbol, setSelectedNewsSymbol] = useState<string | null>(null);
  const [newsData, setNewsData] = useState<FinBERTNewsResponse | null>(null);
  const [isNewsLoading, setIsNewsLoading] = useState<boolean>(false);
  const [newsError, setNewsError] = useState<string | null>(null);

  const handleOpenNewsSidebar = useCallback(async (symbol: string) => {
    const sym = symbol.toUpperCase();
    setSelectedNewsSymbol(sym);
    setIsNewsLoading(true);
    setNewsError(null);
    setNewsData(null);
    try {
      const resp = await getShortScreenerNews(sym);
      setNewsData(resp);
    } catch (err: any) {
      console.error('Failed to fetch 24h news breakdown:', err);
      setNewsError('Could not load 24h news analysis for this stock.');
    } finally {
      setIsNewsLoading(false);
    }
  }, []);

  const handleCloseNewsSidebar = useCallback(() => {
    setSelectedNewsSymbol(null);
    setNewsData(null);
    setNewsError(null);
  }, []);

  // Keyboard shortcut: close sidebar on Escape
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && selectedNewsSymbol) {
        handleCloseNewsSidebar();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedNewsSymbol, handleCloseNewsSidebar]);

  // ── Open Chart Handler ──────────────────────────────────────────────────
  const handleSymbolClick = useCallback(
    (symbol: string) => {
      if (onOpenChart) {
        onOpenChart(symbol);
      }
      if (typeof window !== 'undefined') {
        window.dispatchEvent(
          new CustomEvent('open_chart', {
            detail: { symbol: symbol.toUpperCase() },
          })
        );
      }
    },
    [onOpenChart]
  );

  // ── Master Power Switch: Live Streaming ──────────────────────────────────
  const handleToggleLive = useCallback(async () => {
    setIsPowerToggling(true);
    const target = !isActive;
    setIsActive(target);

    if (target) {
      setIsLoading(true);
      try {
        const res = await setShortScreenerPower(true);
        setIsActive(res.is_active);
        const snapshot = await getLiveShortScreenerStocks();
        setData(snapshot);
      } catch (err) {
        console.error('Failed to activate live streaming:', err);
      } finally {
        setIsLoading(false);
        setIsPowerToggling(false);
      }
    } else {
      try {
        await setShortScreenerPower(false);
      } catch (err) {
        console.error('Failed to turn off screener:', err);
      } finally {
        setIsPowerToggling(false);
      }
    }
  }, [isActive]);

  // ── Initial State Fetch ─────────────────────────────────────────────────
  useEffect(() => {
    let isMounted = true;
    async function init() {
      try {
        const results = await Promise.allSettled([
          getShortScreenerPower(),
          getLiveShortScreenerStocks(),
        ]);

        if (!isMounted) return;

        if (results[0].status === 'fulfilled') {
          setIsActive(results[0].value.is_active);
        }
        if (results[1].status === 'fulfilled') {
          setData(results[1].value);
        }
      } catch (err) {
        console.error('Failed to initialize live short screener:', err);
      } finally {
        if (isMounted) setIsLoading(false);
      }
    }
    init();
    return () => {
      isMounted = false;
    };
  }, []);

  // ── Periodic Polling Fallback (Guaranteed updates even if WS is blocked) ─
  useEffect(() => {
    if (!isActive) return;

    const pollInterval = setInterval(async () => {
      try {
        const snapshot = await getLiveShortScreenerStocks();
        if (snapshot && snapshot.stocks) {
          setData(snapshot);
          setLastTickTime(new Date().toLocaleTimeString('en-IN', { hour12: false }));

          // Update tick-by-tick price flashes
          const newFlashes: Record<string, 'up' | 'down'> = {};
          snapshot.stocks.forEach((st) => {
            const oldPrice = prevPricesRef.current[st.symbol];
            if (oldPrice !== undefined && oldPrice !== st.ltp && st.ltp > 0) {
              newFlashes[st.symbol] = st.ltp > oldPrice ? 'up' : 'down';
            }
            prevPricesRef.current[st.symbol] = st.ltp;
          });

          if (Object.keys(newFlashes).length > 0) {
            setPriceFlashes((prev) => ({ ...prev, ...newFlashes }));
            setTimeout(() => {
              setPriceFlashes((prev) => {
                const cleaned = { ...prev };
                Object.keys(newFlashes).forEach((sym) => delete cleaned[sym]);
                return cleaned;
              });
            }, 800);
          }
        }
      } catch (e) {
        // Silent catch for background poll
      }
    }, 1500);

    return () => clearInterval(pollInterval);
  }, [isActive]);

  // ── WebSocket Live Telemetry ───────────────────────────────────────────
  useEffect(() => {
    if (!isActive) {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      setWsConnected(false);
      return;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const wsUrl = `${protocol}//${host}/ws/short-screener`;

    function connect() {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        setWsConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const payload: LiveShortScreenerResponse = JSON.parse(event.data);
          if (payload && payload.stocks) {
            setData(payload);
            setLastTickTime(new Date().toLocaleTimeString('en-IN', { hour12: false }));

            // Update tick-by-tick price flashes
            const newFlashes: Record<string, 'up' | 'down'> = {};
            payload.stocks.forEach((st) => {
              const oldPrice = prevPricesRef.current[st.symbol];
              if (oldPrice !== undefined && oldPrice !== st.ltp && st.ltp > 0) {
                newFlashes[st.symbol] = st.ltp > oldPrice ? 'up' : 'down';
              }
              prevPricesRef.current[st.symbol] = st.ltp;
            });

            if (Object.keys(newFlashes).length > 0) {
              setPriceFlashes((prev) => ({ ...prev, ...newFlashes }));
              setTimeout(() => {
                setPriceFlashes((prev) => {
                  const cleaned = { ...prev };
                  Object.keys(newFlashes).forEach((sym) => delete cleaned[sym]);
                  return cleaned;
                });
              }, 800);
            }
          }
        } catch (e) {
          console.error('Error parsing live screener message:', e);
        }
      };

      ws.onclose = () => {
        setWsConnected(false);
        if (isActive) {
          reconnectTimeoutRef.current = setTimeout(connect, 3000);
        }
      };

      ws.onerror = (err) => {
        console.warn('Live screener WebSocket encountered error:', err);
        ws.close();
      };
    }

    connect();

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      setWsConnected(false);
    };
  }, [isActive]);

  // ── Auto-Sorted & Filtered Stocks ──────────────────────────────────────
  const displayedStocks = useMemo(() => {
    if (!data || !data.stocks) return [];

    // Filter by search query if any
    let filtered = data.stocks;
    if (searchQuery.trim()) {
      const q = searchQuery.trim().toUpperCase();
      filtered = filtered.filter((s) => s.symbol.includes(q));
    }

    return [...filtered].sort((a, b) => {
      let comparison = 0;
      switch (sortField) {
        case 'symbol':
          comparison = a.symbol.localeCompare(b.symbol);
          break;
        case 'ltp':
          comparison = (a.ltp || 0) - (b.ltp || 0);
          break;
        case 'change_pct':
          comparison = (a.change_pct || 0) - (b.change_pct || 0);
          break;
        case 'rsi':
          comparison = (a.rsi || 0) - (b.rsi || 0);
          break;
        case 'rsi_velocity':
          comparison = (a.rsi_velocity || 0) - (b.rsi_velocity || 0);
          break;
        case 'news_catalyst_score':
          comparison = (a.news_catalyst_score || 0) - (b.news_catalyst_score || 0);
          break;
        case 'ifp_score':
          comparison = (a.ifp_score || 0) - (b.ifp_score || 0);
          break;
        case 'rank':
        default:
          comparison = (a.short_rank || 0) - (b.short_rank || 0);
          break;
      }

      if (comparison !== 0) {
        return sortDirection === 'desc' ? -comparison : comparison;
      }
      // Secondary fallback tie-breaker: RSI descending
      return (b.rsi || 0) - (a.rsi || 0);
    });
  }, [data, searchQuery, sortField, sortDirection]);

  const overboughtCount = useMemo(() => {
    if (!data?.stocks) return 0;
    return data.stocks.filter((s) => s.rsi >= 70.0).length;
  }, [data]);

  const rapidShiftCount = useMemo(() => {
    if (!data?.stocks) return 0;
    return data.stocks.filter((s) => s.is_rapid_rsi).length;
  }, [data]);

  // ── Formatters ──────────────────────────────────────────────────────────
  const formatCurrency = (val: number) => {
    if (!val || val <= 0) return '—';
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(val);
  };

  const formatVolume = (vol: number) => {
    if (!vol) return '—';
    if (vol >= 10000000) return `${(vol / 10000000).toFixed(2)} Cr`;
    if (vol >= 100000) return `${(vol / 100000).toFixed(2)} L`;
    if (vol >= 1000) return `${(vol / 1000).toFixed(1)}k`;
    return vol.toLocaleString();
  };

  return (
    <div className="flex flex-col h-full w-full text-slate-200 overflow-hidden bg-slate-950 font-sans select-none">
      {/* ── Sleek Compact Header Bar (h-11, zero waste, matching Chart Analysis) ── */}
      <header className="h-11 px-3 border-b border-slate-800/80 bg-slate-900/60 flex items-center justify-between shrink-0">
        {/* Left: Icon, Title, Desk Badge & Inline Search Box */}
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-rose-500/20 to-amber-500/20 border border-rose-500/30 flex items-center justify-center text-rose-400">
            <TrendingDown className="w-4 h-4 animate-pulse" />
          </div>
          <div className="flex items-center gap-2">
            <h1 className="text-sm font-black text-white tracking-tight">Live Stock Screener for Short</h1>
            <span className="text-[10px] uppercase font-bold tracking-wider px-1.5 py-0.5 rounded bg-rose-950 text-rose-300 border border-rose-800/50">
              SHORTS DESK (600+)
            </span>
          </div>

          {/* Compact Inline Search Box directly in top toolbar */}
          <div className="relative ml-2 w-48 sm:w-60 hidden sm:block">
            <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search symbol (e.g. RELIANCE)..."
              className="w-full pl-8 pr-2.5 py-1 bg-slate-800/80 border border-slate-700/60 rounded-lg text-xs text-white placeholder-slate-400 focus:outline-none focus:border-rose-500/60 focus:ring-1 focus:ring-rose-500/40 transition"
            />
          </div>
        </div>

        {/* Right: Metrics Badges & Master Live Streaming Power Switch */}
        <div className="flex items-center gap-2">
          {/* Small screen inline search */}
          <div className="relative w-28 sm:hidden">
            <Search className="w-3 h-3 text-slate-400 absolute left-2 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search..."
              className="w-full pl-6 pr-2 py-0.5 bg-slate-800/80 border border-slate-700/60 rounded-lg text-[11px] text-white placeholder-slate-400 focus:outline-none"
            />
          </div>

          {/* Quick Metrics Badges */}
          <div className="hidden lg:flex items-center gap-1.5 px-2 py-1 rounded-lg bg-slate-800/60 border border-slate-700/50 text-[11px]">
            <span className="text-slate-400">Total:</span>
            <span className="font-mono font-bold text-white">{data?.total_count || 0}</span>
          </div>

          <div className="hidden md:flex items-center gap-1.5 px-2 py-1 rounded-lg bg-rose-950/40 border border-rose-800/40 text-[11px]">
            <Flame className="w-3 h-3 text-rose-400" />
            <span className="text-rose-300 font-medium">Overbought:</span>
            <span className="font-mono font-bold text-rose-400">{overboughtCount}</span>
          </div>

          <div className="hidden sm:flex items-center gap-1.5 px-2 py-1 rounded-lg bg-amber-950/40 border border-amber-800/40 text-[11px]">
            <Zap className="w-3 h-3 text-amber-400" />
            <span className="text-amber-300 font-medium">Rapid:</span>
            <span className="font-mono font-bold text-amber-400">{rapidShiftCount}</span>
          </div>

          {/* Master Live Streaming Power Button */}
          <button
            onClick={handleToggleLive}
            disabled={isPowerToggling}
            className={`px-3 py-1.5 rounded-lg text-xs font-bold transition-all flex items-center gap-1.5 shadow-sm active:scale-95 ${
              isActive
                ? 'bg-emerald-500 hover:bg-emerald-400 text-slate-950 shadow-emerald-500/25 ring-2 ring-emerald-400/40'
                : 'bg-slate-800/90 hover:bg-slate-700/90 text-slate-300 border border-slate-700'
            }`}
          >
            <Power className={`w-3.5 h-3.5 ${isActive ? 'text-slate-950' : 'text-slate-400'}`} />
            <span>{isActive ? 'LIVE ACTIVE' : 'LIVE STANDBY'}</span>
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                isActive ? 'bg-slate-950 animate-ping' : 'bg-slate-500'
              }`}
            />
          </button>
        </div>
      </header>

      {/* ── Sub-Status & Sorting Bar (h-6, dense information) ───────────────── */}
      <div className="h-6 bg-slate-950/90 border-b border-slate-800/70 px-3 flex items-center justify-between text-[10px] text-slate-400 font-mono shrink-0 select-none">
        {/* Left: Stream Connection & Tick Time */}
        <div className="flex items-center gap-2">
          <span
            className={`w-1.5 h-1.5 rounded-full ${
              wsConnected ? 'bg-emerald-400 animate-pulse' : isActive ? 'bg-amber-400 animate-pulse' : 'bg-slate-500'
            }`}
          />
          <span className="font-semibold tracking-tight text-slate-300">
            {wsConnected ? 'LIVE STREAMING (WEBSOCKET)' : isActive ? 'CONNECTING...' : 'STANDBY (PAUSED)'}
          </span>
          {data?.market_status && (
            <span
              className={`px-1.5 py-0.2 rounded font-bold text-[9px] ${
                data.market_status.includes('OPEN')
                  ? 'bg-emerald-950/60 text-emerald-400 border border-emerald-800/40'
                  : 'bg-slate-800 text-slate-400 border border-slate-700/50'
              }`}
            >
              • {data.market_status}
            </span>
          )}
          {lastTickTime && (
            <span className="text-slate-500 hidden sm:inline">• Last tick: {lastTickTime} IST</span>
          )}
          <span className="text-slate-500 hidden md:inline">
            • Showing {displayedStocks.length} of {data?.total_count || 0} stocks
          </span>
        </div>

        {/* Right: Active Column Sort & Reset Action */}
        <div className="flex items-center gap-1.5">
          <span>Sorted by:</span>
          <span className="font-semibold text-rose-400">
            {sortField === 'rsi'
              ? 'Live RSI (14)'
              : sortField === 'ltp'
              ? 'LTP'
              : sortField === 'change_pct'
              ? 'Change %'
              : sortField === 'symbol'
              ? 'Stock Symbol'
              : sortField === 'rsi_velocity'
              ? 'RSI Shift'
              : sortField === 'news_catalyst_score'
              ? '24h News Catalyst'
              : sortField === 'ifp_score'
              ? 'IFP Flow'
              : 'Rank'}
          </span>
          <span className="text-slate-500 font-mono text-[10px]">
            ({sortDirection === 'desc' ? 'High → Low' : 'Low → High'})
          </span>
          {(sortField !== 'rsi' || sortDirection !== 'desc') && (
            <button
              onClick={() => {
                setSortField('rsi');
                setSortDirection('desc');
              }}
              className="ml-1.5 text-[10px] text-rose-400 hover:text-rose-300 underline underline-offset-2 cursor-pointer transition font-medium"
              title="Reset to default sorting (Wilder RSI Descending)"
            >
              Reset to Default (RSI High → Low)
            </button>
          )}
        </div>
      </div>

      {/* ── Standby Notification Strip (When OFF, table remains visible) ── */}
      {!isActive && (
        <div className="bg-amber-950/40 border-b border-amber-800/40 px-3 py-1 flex items-center justify-between text-xs text-amber-300 shrink-0 select-none">
          <div className="flex items-center gap-2">
            <Pause className="w-3.5 h-3.5 text-amber-400 shrink-0" />
            <span className="text-[11px]">
              Live tick streaming is paused. Displaying latest loaded market data across all {data?.total_count || 600}+ stocks.
            </span>
          </div>
          <button
            onClick={handleToggleLive}
            disabled={isPowerToggling}
            className="px-2.5 py-0.5 rounded bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-[10px] transition active:scale-95 inline-flex items-center gap-1 shadow-sm"
          >
            <Play className="w-3 h-3 fill-current" />
            <span>Resume Streaming</span>
          </button>
        </div>
      )}

      {/* ── Live Stock Table (Zero-Waste Edge-to-Edge Grid) ──────────────── */}
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-slate-950">
          <div className="flex-1 min-h-0 overflow-y-auto overflow-x-auto custom-scrollbar">
            <table className="w-full text-left border-collapse font-sans">
              <thead className="sticky top-0 z-20 bg-slate-900/95 backdrop-blur-md border-b border-slate-800 shadow-sm">
                <tr className="text-[10px] font-bold uppercase tracking-wider text-slate-400">
                  {/* # Rank */}
                  <th
                    className="py-2 px-2.5 w-10 text-center select-none cursor-pointer group hover:text-slate-200 transition-colors"
                    onClick={() => handleSort('rank')}
                    title="Sort by Rank (#)"
                  >
                    <div className="inline-flex items-center justify-center gap-1">
                      <span className={sortField === 'rank' ? 'text-rose-400 font-bold' : ''}>#</span>
                      {sortField === 'rank' ? (
                        sortDirection === 'desc' ? (
                          <ArrowDown className="w-2.5 h-2.5 text-rose-400" />
                        ) : (
                          <ArrowUp className="w-2.5 h-2.5 text-rose-400" />
                        )
                      ) : (
                        <ArrowUpDown className="w-2.5 h-2.5 opacity-0 group-hover:opacity-60 transition" />
                      )}
                    </div>
                  </th>

                  {/* Stock Symbol */}
                  <th
                    className="py-2 px-3 select-none cursor-pointer group hover:text-slate-200 transition-colors"
                    onClick={() => handleSort('symbol')}
                    title="Sort by Stock Symbol"
                  >
                    <div className="inline-flex items-center gap-1">
                      <span className={sortField === 'symbol' ? 'text-rose-400 font-bold' : ''}>
                        Stock Symbol
                      </span>
                      {sortField === 'symbol' ? (
                        sortDirection === 'desc' ? (
                          <ArrowDown className="w-3 h-3 text-rose-400" />
                        ) : (
                          <ArrowUp className="w-3 h-3 text-rose-400" />
                        )
                      ) : (
                        <ArrowUpDown className="w-3 h-3 opacity-0 group-hover:opacity-60 transition" />
                      )}
                    </div>
                  </th>

                  {/* LTP */}
                  <th
                    className="py-2 px-3 text-right select-none cursor-pointer group hover:text-slate-200 transition-colors"
                    onClick={() => handleSort('ltp')}
                    title="Sort by Last Traded Price (LTP)"
                  >
                    <div className="inline-flex items-center justify-end gap-1 w-full">
                      <span className={sortField === 'ltp' ? 'text-rose-400 font-bold' : ''}>
                        LTP (₹)
                      </span>
                      {sortField === 'ltp' ? (
                        sortDirection === 'desc' ? (
                          <ArrowDown className="w-3 h-3 text-rose-400" />
                        ) : (
                          <ArrowUp className="w-3 h-3 text-rose-400" />
                        )
                      ) : (
                        <ArrowUpDown className="w-3 h-3 opacity-0 group-hover:opacity-60 transition" />
                      )}
                    </div>
                  </th>

                  {/* Change % */}
                  <th
                    className="py-2 px-3 text-right select-none cursor-pointer group hover:text-slate-200 transition-colors"
                    onClick={() => handleSort('change_pct')}
                    title="Sort by Change %"
                  >
                    <div className="inline-flex items-center justify-end gap-1 w-full">
                      <span className={sortField === 'change_pct' ? 'text-rose-400 font-bold' : ''}>
                        Change %
                      </span>
                      {sortField === 'change_pct' ? (
                        sortDirection === 'desc' ? (
                          <ArrowDown className="w-3 h-3 text-rose-400" />
                        ) : (
                          <ArrowUp className="w-3 h-3 text-rose-400" />
                        )
                      ) : (
                        <ArrowUpDown className="w-3 h-3 opacity-0 group-hover:opacity-60 transition" />
                      )}
                    </div>
                  </th>

                  {/* Live RSI (14) */}
                  <th
                    className="py-2 px-3 text-right select-none cursor-pointer group hover:text-slate-200 transition-colors"
                    onClick={() => handleSort('rsi')}
                    title="Sort by Wilder 14 RSI"
                  >
                    <div className="inline-flex items-center justify-end gap-1 w-full">
                      <span className={sortField === 'rsi' ? 'text-rose-400 font-bold' : ''}>
                        Live RSI (14)
                      </span>
                      {sortField === 'rsi' ? (
                        sortDirection === 'desc' ? (
                          <ArrowDown className="w-3 h-3 text-rose-400" />
                        ) : (
                          <ArrowUp className="w-3 h-3 text-rose-400" />
                        )
                      ) : (
                        <ArrowUpDown className="w-3 h-3 opacity-0 group-hover:opacity-60 transition" />
                      )}
                    </div>
                  </th>

                  {/* RSI Shift / Velocity */}
                  <th
                    className="py-2 px-3 text-center select-none cursor-pointer group hover:text-slate-200 transition-colors"
                    onClick={() => handleSort('rsi_velocity')}
                    title="Sort by RSI Velocity (pts/min)"
                  >
                    <div className="inline-flex items-center justify-center gap-1 w-full">
                      <span className={sortField === 'rsi_velocity' ? 'text-rose-400 font-bold' : ''}>
                        RSI Shift
                      </span>
                      {sortField === 'rsi_velocity' ? (
                        sortDirection === 'desc' ? (
                          <ArrowDown className="w-3 h-3 text-rose-400" />
                        ) : (
                          <ArrowUp className="w-3 h-3 text-rose-400" />
                        )
                      ) : (
                        <ArrowUpDown className="w-3 h-3 opacity-0 group-hover:opacity-60 transition" />
                      )}
                    </div>
                  </th>

                  {/* 24h News Catalyst (FinBERT) */}
                  <th
                    className="py-2 px-3 text-center select-none cursor-pointer group hover:text-slate-200 transition-colors"
                    onClick={() => handleSort('news_catalyst_score')}
                    title="Sort by 24h News Catalyst Score (FinBERT)"
                  >
                    <div className="inline-flex items-center justify-center gap-1 w-full">
                      <span className={sortField === 'news_catalyst_score' ? 'text-rose-400 font-bold' : ''}>
                        24h Catalyst (FinBERT)
                      </span>
                      {sortField === 'news_catalyst_score' ? (
                        sortDirection === 'desc' ? (
                          <ArrowDown className="w-3 h-3 text-rose-400" />
                        ) : (
                          <ArrowUp className="w-3 h-3 text-rose-400" />
                        )
                      ) : (
                        <ArrowUpDown className="w-3 h-3 opacity-0 group-hover:opacity-60 transition" />
                      )}
                    </div>
                  </th>

                  {/* IFP Flow Score */}
                  <th
                    className="py-2 px-3 text-center select-none cursor-pointer group hover:text-slate-200 transition-colors"
                    onClick={() => handleSort('ifp_score')}
                    title="Sort by Institutional Flow Profile (IFP) Score"
                  >
                    <div className="inline-flex items-center justify-center gap-1 w-full">
                      <span className={sortField === 'ifp_score' ? 'text-rose-400 font-bold' : ''}>
                        IFP Flow
                      </span>
                      {sortField === 'ifp_score' ? (
                        sortDirection === 'desc' ? (
                          <ArrowDown className="w-3 h-3 text-rose-400" />
                        ) : (
                          <ArrowUp className="w-3 h-3 text-rose-400" />
                        )
                      ) : (
                        <ArrowUpDown className="w-3 h-3 opacity-0 group-hover:opacity-60 transition" />
                      )}
                    </div>
                  </th>

                  {/* Action */}
                  <th className="py-2 px-3 text-right text-slate-400">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/40 text-xs">
                {isLoading ? (
                  <tr>
                    <td colSpan={9} className="py-12 text-center text-slate-400">
                      <div className="inline-flex items-center gap-2">
                        <RefreshCw className="w-4 h-4 animate-spin text-rose-500" />
                        <span>Initializing live universe...</span>
                      </div>
                    </td>
                  </tr>
                ) : displayedStocks.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="py-12 text-center text-slate-400">
                      No stocks match your search criteria.
                    </td>
                  </tr>
                ) : (
                  displayedStocks.map((stock, index) => {
                    const flash = priceFlashes[stock.symbol];
                    const isOverbought = stock.rsi >= 70.0;
                    const isExtremeOverbought = stock.rsi >= 75.0;
                    const isRapid = stock.is_rapid_rsi;

                    return (
                      <tr
                        key={stock.symbol}
                        className={`h-9 transition-colors duration-150 hover:bg-slate-800/50 ${
                          isRapid
                            ? 'bg-amber-500/10 hover:bg-amber-500/15 ring-1 ring-inset ring-amber-400/40'
                            : isExtremeOverbought
                            ? 'bg-rose-950/25 hover:bg-rose-950/35'
                            : ''
                        }`}
                      >
                        {/* Rank */}
                        <td className="py-1 px-2.5 text-center font-mono text-[11px] text-slate-500">
                          {index + 1}
                        </td>

                        {/* Stock Symbol */}
                        <td className="py-1 px-3">
                          <button
                            onClick={() => handleSymbolClick(stock.symbol)}
                            className="group flex items-center gap-1.5 text-left focus:outline-none"
                            title="Click to open full chart in Charts Pro"
                          >
                            <span className="font-bold text-white group-hover:text-rose-400 transition tracking-wide text-xs">
                              {stock.symbol}
                            </span>
                            <span className="text-[9px] px-1 py-0.2 rounded font-mono bg-slate-800 text-slate-400 border border-slate-700/80 group-hover:border-rose-500/40">
                              EQ
                            </span>
                            <ExternalLink className="w-3 h-3 opacity-0 group-hover:opacity-100 text-rose-400 transition" />
                          </button>
                        </td>

                        {/* LTP with Tick Flash */}
                        <td
                          className={`py-1 px-3 text-right font-mono font-bold text-xs transition-colors duration-200 ${
                            flash === 'up'
                              ? 'text-emerald-400 bg-emerald-500/10'
                              : flash === 'down'
                              ? 'text-rose-400 bg-rose-500/10'
                              : 'text-white'
                          }`}
                        >
                          {formatCurrency(stock.ltp)}
                        </td>

                        {/* Change % */}
                        <td className="py-1 px-3 text-right font-mono">
                          <span
                            className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[11px] font-semibold ${
                              stock.change_pct > 0
                                ? 'bg-emerald-950/50 text-emerald-400 border border-emerald-800/40'
                                : stock.change_pct < 0
                                ? 'bg-rose-950/50 text-rose-400 border border-rose-800/40'
                                : 'bg-slate-800 text-slate-400'
                            }`}
                          >
                            {stock.change_pct > 0 ? (
                              <ArrowUpRight className="w-2.5 h-2.5" />
                            ) : stock.change_pct < 0 ? (
                              <ArrowDownRight className="w-2.5 h-2.5" />
                            ) : null}
                            {stock.change_pct > 0 ? `+${stock.change_pct}%` : `${stock.change_pct}%`}
                          </span>
                        </td>

                        {/* Live 5m Wilder RSI */}
                        <td className="py-1 px-3 text-right">
                          <div className="flex items-center justify-end gap-2">
                            <span
                              className={`font-mono text-xs font-black ${
                                isExtremeOverbought
                                  ? 'text-rose-400 drop-shadow-[0_0_6px_rgba(244,63,94,0.4)]'
                                  : isOverbought
                                  ? 'text-rose-300'
                                  : stock.rsi >= 60
                                  ? 'text-amber-300'
                                  : stock.rsi <= 40
                                  ? 'text-emerald-400'
                                  : 'text-slate-300'
                              }`}
                            >
                              {stock.rsi.toFixed(1)}
                            </span>
                            <div className="w-12 h-1 rounded-full bg-slate-800 overflow-hidden relative">
                              <div
                                className={`h-full rounded-full transition-all duration-300 ${
                                  isOverbought
                                    ? 'bg-rose-500'
                                    : stock.rsi >= 60
                                    ? 'bg-amber-500'
                                    : 'bg-emerald-500'
                                }`}
                                style={{ width: `${Math.min(100, Math.max(0, stock.rsi))}%` }}
                              />
                            </div>
                          </div>
                        </td>

                        {/* RSI Shift / Velocity Indicator */}
                        <td className="py-1 px-3 text-center">
                          {isRapid ? (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-400 text-slate-950 shadow-sm animate-pulse">
                              <Zap className="w-3 h-3 fill-current" />
                              <span>
                                RAPID ({stock.rsi_velocity > 0 ? `+` : ''}
                                {stock.rsi_velocity.toFixed(1)}/m)
                              </span>
                            </span>
                          ) : Math.abs(stock.rsi_velocity) > 0.5 ? (
                            <span className="font-mono text-[11px] text-slate-400">
                              {stock.rsi_velocity > 0 ? `+` : ''}
                              {stock.rsi_velocity.toFixed(1)}/m
                            </span>
                          ) : (
                            <span className="text-[10px] text-slate-600 font-mono">Stable</span>
                          )}
                        </td>

                        {/* 24h News Catalyst (FinBERT) */}
                        <td className="py-1 px-3 text-center">
                          {(() => {
                            const isEvaluated = Boolean(
                              stock.news_status === 'EVALUATED' ||
                              (stock.is_news_evaluated && (stock.news_article_count ?? 0) > 0)
                            );
                            const isNoNews = Boolean(
                              stock.news_status === 'NO_NEWS' ||
                              stock.news_catalyst_label === 'NO_NEWS' ||
                              (!isEvaluated && stock.news_article_count === 0 && stock.news_status !== 'UNEVALUATED' && stock.news_catalyst_label !== 'UNEVALUATED')
                            );
                            const score = stock.news_catalyst_score || 0;

                            if (isEvaluated) {
                              const badgeColor =
                                score > 0
                                  ? 'bg-emerald-950/70 text-emerald-300 border border-emerald-800/60 hover:border-emerald-400 hover:bg-emerald-900/60'
                                  : score < 0
                                  ? 'bg-rose-950/70 text-rose-300 border border-rose-800/60 hover:border-rose-400 hover:bg-rose-900/60'
                                  : 'bg-slate-800/70 text-slate-300 border border-slate-700/60 hover:border-slate-500 hover:bg-slate-800';

                              return (
                                <button
                                  onClick={() => handleOpenNewsSidebar(stock.symbol)}
                                  className={`group inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-mono font-medium transition-all duration-150 hover:scale-105 active:scale-95 cursor-pointer shadow-sm ${badgeColor}`}
                                  title={`Evaluated: FinBERT Catalyst score ${score > 0 ? '+' : ''}${score} (${stock.news_article_count ?? 0} articles). Click to inspect.`}
                                >
                                  <Newspaper className="w-3 h-3 opacity-80 group-hover:opacity-100 transition" />
                                  <span className="font-bold">
                                    EVALUATED ({score > 0 ? `+` : ''}{score})
                                  </span>
                                </button>
                              );
                            }

                            if (isNoNews) {
                              return (
                                <button
                                  onClick={() => handleOpenNewsSidebar(stock.symbol)}
                                  className="group inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-mono font-medium transition-all duration-150 hover:scale-105 active:scale-95 cursor-pointer shadow-sm bg-slate-800/50 text-slate-400 border border-slate-700/50 hover:border-slate-500 hover:bg-slate-800 hover:text-slate-200"
                                  title={`Search completed: No news articles found for ${stock.symbol} in past 24 hours. Click to check.`}
                                >
                                  <Newspaper className="w-3 h-3 opacity-50 group-hover:opacity-80 transition" />
                                  <span className="font-semibold tracking-wide">
                                    (NO NEWS)
                                  </span>
                                </button>
                              );
                            }

                            // Unevaluated (in process of evaluation / searching news)
                            return (
                              <button
                                onClick={() => handleOpenNewsSidebar(stock.symbol)}
                                className="group inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-mono font-medium transition-all duration-150 hover:scale-105 active:scale-95 cursor-pointer shadow-sm bg-slate-900/60 text-slate-500 border border-slate-800/80 hover:border-slate-600 hover:text-slate-400"
                                title={`Pending: In process of searching 24h news for ${stock.symbol}. Click to check.`}
                              >
                                <Newspaper className="w-3 h-3 opacity-40 group-hover:opacity-70 transition" />
                                <span className="font-medium tracking-wide">
                                  UNEVALUATED...
                                </span>
                              </button>
                            );
                          })()}
                        </td>

                        {/* IFP Score (Institutional Flow Profile) */}
                        <td className="py-1 px-3 text-center">
                          <span
                            className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-mono font-medium ${
                              stock.ifp_score <= -20
                                ? 'bg-rose-950/60 text-rose-300 border border-rose-800/50'
                                : stock.ifp_score >= 20
                                ? 'bg-emerald-950/60 text-emerald-300 border border-emerald-800/50'
                                : 'bg-slate-800/50 text-slate-400 border border-slate-700/40'
                            }`}
                            title={`IFP Score: ${stock.ifp_score} (${stock.ifp_label})`}
                          >
                            <span className="font-bold">
                              {stock.ifp_score > 0 ? `+` : ''}
                              {stock.ifp_score}
                            </span>
                            <span className="text-[9px] text-slate-400">
                              {stock.ifp_score <= -20 ? 'Dist' : stock.ifp_score >= 20 ? 'Acc' : 'Bal'}
                            </span>
                          </span>
                        </td>

                        {/* Action: Open Chart in Charts Pro */}
                        <td className="py-1 px-3 text-right">
                          <button
                            onClick={() => handleSymbolClick(stock.symbol)}
                            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg bg-slate-800/90 hover:bg-rose-600 text-slate-300 hover:text-white text-[10px] font-semibold transition shadow-sm"
                          >
                            <BarChart2 className="w-3 h-3" />
                            <span>Charts Pro</span>
                          </button>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>

      {/* ── 24h FinBERT News Breakdown Slide-Out Sidebar ────────────────── */}
      {selectedNewsSymbol && (
        <div className="fixed inset-0 z-50 flex justify-end">
          {/* Backdrop overlay */}
          <div
            className="fixed inset-0 bg-black/60 backdrop-blur-sm transition-opacity"
            onClick={handleCloseNewsSidebar}
          />

          {/* Slide-out Sidebar Panel */}
          <aside className="relative w-[480px] max-w-[95vw] h-full bg-slate-950/98 border-l border-slate-800/90 shadow-2xl flex flex-col z-50 overflow-hidden text-slate-100 animate-in slide-in-from-right duration-200">
            {/* Drawer Header (h-11 matching top bar) */}
            <div className="h-11 px-3 border-b border-slate-800/80 bg-slate-900/80 backdrop-blur-md flex items-center justify-between shrink-0">
              <div className="flex items-center gap-2">
                <div className="w-6 h-6 rounded-lg bg-purple-500/10 border border-purple-500/30 flex items-center justify-center text-purple-400">
                  <Newspaper className="w-3.5 h-3.5" />
                </div>
                <div className="flex items-center gap-1.5">
                  <h2 className="text-sm font-bold text-white tracking-wide">{selectedNewsSymbol}</h2>
                  <span className="text-[9px] px-1 py-0.2 rounded font-mono bg-slate-800 text-slate-400 border border-slate-700">
                    EQ
                  </span>
                  <span className="text-[10px] text-slate-400 hidden sm:inline">• 24h News Intelligence</span>
                </div>
              </div>

              <div className="flex items-center gap-1.5">
                <button
                  onClick={handleCloseNewsSidebar}
                  className="p-1 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-400 hover:text-white transition"
                  title="Close sidebar (Esc)"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>

            {/* Drawer Body (Scrollable) */}
            <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar p-3.5 space-y-3">
              {isNewsLoading ? (
                <div className="py-20 text-center space-y-3">
                  <RefreshCw className="w-6 h-6 animate-spin text-purple-400 mx-auto" />
                  <p className="text-xs text-slate-300 font-medium">Scanning 24h News & Running FinBERT Inference...</p>
                  <p className="text-[11px] text-slate-500 max-w-xs mx-auto">
                    Evaluating headlines, regulatory weights, and net catalyst score.
                  </p>
                </div>
              ) : newsError ? (
                <div className="p-4 rounded-xl bg-rose-950/30 border border-rose-800/50 text-center space-y-2">
                  <AlertCircle className="w-5 h-5 text-rose-400 mx-auto" />
                  <p className="text-xs font-semibold text-rose-200">{newsError}</p>
                  <button
                    onClick={() => handleOpenNewsSidebar(selectedNewsSymbol)}
                    className="px-3 py-1 rounded-lg bg-rose-600 hover:bg-rose-500 text-white text-[11px] font-semibold"
                  >
                    Retry Scan
                  </button>
                </div>
              ) : newsData ? (
                <>
                  {/* Executive Score Card */}
                  <div className="p-3.5 rounded-xl bg-slate-900/90 border border-slate-800 space-y-2.5 shadow-md">
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
                        Net 24h Catalyst Score
                      </span>
                      <span className="text-[10px] font-mono text-slate-500">
                        {newsData.is_evaluated && newsData.article_count > 0
                          ? `${newsData.article_count} Articles Evaluated`
                          : '0 Articles (Search Completed)'}
                      </span>
                    </div>

                    <div className="flex items-center justify-between gap-3">
                      <div className="flex items-baseline gap-1.5">
                        <span
                          className={`font-mono text-2xl font-black ${
                            newsData.status === 'NO_NEWS' || newsData.sentiment_label === 'NO_NEWS' || newsData.article_count === 0
                              ? 'text-slate-400'
                              : !newsData.is_evaluated
                              ? 'text-slate-500'
                              : newsData.sentiment_score < 0
                              ? 'text-rose-400'
                              : newsData.sentiment_score > 0
                              ? 'text-emerald-400'
                              : 'text-slate-300'
                          }`}
                        >
                          {newsData.is_evaluated && newsData.article_count > 0
                            ? `${newsData.sentiment_score > 0 ? '+' : ''}${newsData.sentiment_score}`
                            : '0'}
                        </span>
                        <span className="text-[10px] text-slate-500 font-mono">/ 100</span>
                      </div>

                      <span
                        className={`px-2.5 py-0.5 rounded-full text-[11px] font-bold uppercase tracking-wide border ${
                          newsData.status === 'NO_NEWS' || newsData.sentiment_label === 'NO_NEWS' || newsData.article_count === 0
                            ? 'bg-slate-800 text-slate-300 border-slate-700'
                            : !newsData.is_evaluated
                            ? 'bg-slate-900/80 text-slate-500 border-slate-800'
                            : newsData.sentiment_score < 0
                            ? 'bg-rose-950/60 text-rose-300 border-rose-700/60'
                            : newsData.sentiment_score > 0
                            ? 'bg-emerald-950/60 text-emerald-300 border-emerald-700/60'
                            : 'bg-slate-800 text-slate-400 border-slate-700'
                        }`}
                      >
                        {newsData.status === 'NO_NEWS' || newsData.sentiment_label === 'NO_NEWS' || newsData.article_count === 0
                          ? '(NO NEWS)'
                          : !newsData.is_evaluated
                          ? 'UNEVALUATED...'
                          : `EVALUATED (${newsData.sentiment_score > 0 ? '+' : ''}${newsData.sentiment_score})`}
                      </span>
                    </div>

                    {/* FinBERT Probability Breakdown Bar */}
                    <div className="space-y-1 pt-0.5">
                      <div className="flex justify-between text-[10px] font-mono text-slate-400">
                        <span className="text-rose-400 font-semibold">
                          Bearish: {Math.round((newsData.probabilities?.negative || 0) * 100)}%
                        </span>
                        <span className="text-slate-400">
                          Neutral: {Math.round((newsData.probabilities?.neutral || 0) * 100)}%
                        </span>
                        <span className="text-emerald-400 font-semibold">
                          Bullish: {Math.round((newsData.probabilities?.positive || 0) * 100)}%
                        </span>
                      </div>

                      <div className="h-1.5 w-full rounded-full bg-slate-800 overflow-hidden flex">
                        <div
                          className="bg-rose-500 transition-all duration-500"
                          style={{ width: `${Math.round((newsData.probabilities?.negative || 0) * 100)}%` }}
                        />
                        <div
                          className="bg-slate-600 transition-all duration-500"
                          style={{ width: `${Math.round((newsData.probabilities?.neutral || 0) * 100)}%` }}
                        />
                        <div
                          className="bg-emerald-500 transition-all duration-500"
                          style={{ width: `${Math.round((newsData.probabilities?.positive || 0) * 100)}%` }}
                        />
                      </div>
                    </div>

                    {/* Summary text */}
                    {newsData.summary && (
                      <p className="text-[11px] text-slate-300 leading-relaxed pt-1 border-t border-slate-800/80">
                        {newsData.summary}
                      </p>
                    )}
                  </div>

                  {/* Cross-Modal Narrative Alignment (with IFP) */}
                  {newsData.narrative_alignment && (
                    <div className="p-2.5 rounded-lg bg-slate-900/60 border border-slate-800 flex items-start gap-2">
                      <ShieldAlert className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5" />
                      <div className="space-y-0.5 text-xs">
                        <div className="flex items-center gap-1.5">
                          <span className="font-semibold text-white text-[11px]">Tape & Narrative Alignment:</span>
                          <span
                            className={`px-1.5 py-0.2 rounded text-[9px] font-bold uppercase ${
                              newsData.narrative_alignment.type === 'danger'
                                ? 'bg-rose-950 text-rose-300 border border-rose-800'
                                : newsData.narrative_alignment.type === 'success'
                                ? 'bg-emerald-950 text-emerald-300 border border-emerald-800'
                                : 'bg-slate-800 text-slate-300 border border-slate-700'
                            }`}
                          >
                            {newsData.narrative_alignment.badge}
                          </span>
                        </div>
                        <p className="text-slate-400 text-[10px] leading-relaxed">
                          {newsData.narrative_alignment.detail}
                        </p>
                      </div>
                    </div>
                  )}

                  {/* Scored News Articles List */}
                  <div className="space-y-2">
                    <div className="flex items-center justify-between pb-0.5">
                      <div className="flex items-center gap-1.5">
                        <FileText className="w-3.5 h-3.5 text-rose-400" />
                        <h3 className="text-[10px] font-bold text-white uppercase tracking-wider">
                          News Headlines Deciding Score ({newsData.headlines.length})
                        </h3>
                      </div>
                      <span className="text-[10px] font-mono text-slate-500">Lookback: Past 24h</span>
                    </div>

                    {newsData.headlines.length === 0 ? (
                      <div className="p-4 rounded-xl bg-slate-900/40 border border-slate-800 text-center space-y-1.5">
                        <CheckCircle2 className="w-4 h-4 text-slate-500 mx-auto" />
                        <p className="text-[11px] text-slate-400">
                          {newsData.status === 'UNEVALUATED'
                            ? `Searching 24h news for ${newsData.symbol}...`
                            : `Search completed: No strictly relevant news articles published for ${newsData.symbol} in the past 24 hours.`}
                        </p>
                      </div>
                    ) : (
                      newsData.headlines.map((item, idx) => (
                        <div
                          key={idx}
                          className="p-3 rounded-lg bg-slate-900/80 border border-slate-800/80 hover:border-slate-700 transition space-y-2 shadow-sm"
                        >
                          {/* Top Row: Time, Source, Sentiment Tag */}
                          <div className="flex flex-wrap items-center justify-between gap-1.5">
                            <div className="flex items-center gap-1.5 text-xs">
                              {/* Time with Clock icon */}
                              <span className="inline-flex items-center gap-1 font-mono text-[10px] font-semibold text-amber-400/90 bg-amber-500/10 px-1.5 py-0.5 rounded border border-amber-500/20">
                                <Clock className="w-2.5 h-2.5 text-amber-400" />
                                <span>{item.time_ago || `${item.hours_ago}h ago`}</span>
                              </span>

                              {/* Publisher Source */}
                              <span className="text-[10px] text-slate-400 font-medium px-1.5 py-0.5 rounded bg-slate-800/60 border border-slate-700/50">
                                {item.source || 'News'}
                              </span>

                              {item.is_regulatory && (
                                <span className="text-[9px] font-mono font-bold text-purple-300 bg-purple-950/60 px-1 py-0.2 rounded border border-purple-800/60">
                                  REGULATORY 1.5x
                                </span>
                              )}
                            </div>

                            {/* Item Sentiment Score Badge */}
                            <span
                              className={`px-1.5 py-0.5 rounded font-mono text-[10px] font-bold ${
                                item.score <= -15
                                  ? 'bg-rose-950/80 text-rose-300 border border-rose-800/60'
                                  : item.score >= 15
                                  ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-800/60'
                                  : 'bg-slate-800 text-slate-400 border border-slate-700'
                              }`}
                            >
                              {item.sentiment} ({item.score > 0 ? '+' : ''}{item.score})
                            </span>
                          </div>

                          {/* Headline Title */}
                          <h4 className="text-xs font-semibold text-white leading-snug">
                            {item.title}
                          </h4>

                          {/* Important Details / Impact Context */}
                          {item.detail && (
                            <div className="p-2 rounded bg-slate-950/60 border border-slate-800/60 text-[10px] text-slate-300 leading-relaxed font-sans">
                              <span className="text-slate-400 font-semibold block text-[9px] uppercase tracking-wider mb-0.5">
                                Key Impact Details:
                              </span>
                              {item.detail}
                            </div>
                          )}

                          {/* FinBERT Neural Probabilities sub-row */}
                          {(item.prob_negative !== undefined || item.prob_positive !== undefined) && (
                            <div className="flex items-center gap-2.5 text-[9px] font-mono text-slate-500 pt-0.5">
                              <span>Probabilities:</span>
                              <span className="text-rose-400/80">
                                Neg: {Math.round((item.prob_negative || 0) * 100)}%
                              </span>
                              <span className="text-slate-500">
                                Neu: {Math.round((item.prob_neutral || 0) * 100)}%
                              </span>
                              <span className="text-emerald-400/80">
                                Pos: {Math.round((item.prob_positive || 0) * 100)}%
                              </span>
                            </div>
                          )}
                        </div>
                      ))
                    )}
                  </div>
                </>
              ) : null}
            </div>

            {/* Drawer Footer (h-11) */}
            <div className="h-11 px-3 border-t border-slate-800/80 bg-slate-900/60 flex items-center justify-between gap-2 shrink-0">
              <button
                onClick={() => {
                  if (selectedNewsSymbol) {
                    handleSymbolClick(selectedNewsSymbol);
                    handleCloseNewsSidebar();
                  }
                }}
                className="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-rose-600 text-white font-medium text-xs transition shadow"
              >
                <BarChart2 className="w-3.5 h-3.5" />
                <span>Open in Charts Pro</span>
              </button>

              <button
                onClick={() => {
                  if (selectedNewsSymbol) {
                    handleOpenNewsSidebar(selectedNewsSymbol);
                  }
                }}
                disabled={isNewsLoading}
                className="inline-flex items-center justify-center gap-1 px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition"
                title="Rescan 24h News"
              >
                <RefreshCw className={`w-3 h-3 ${isNewsLoading ? 'animate-spin' : ''}`} />
                <span>Refresh</span>
              </button>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

