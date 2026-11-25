import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  createChart,
  ColorType,
  CrosshairMode,
  LineStyle,
  TickMarkType,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
  IChartApi,
  ISeriesApi,
  IPriceLine,
  UTCTimestamp,
} from 'lightweight-charts';
import { formatISTTime, formatISTDateTime, IST_TIMEZONE, IST_LOCALE } from '../utils/timeFormat';
import {
  Search,
  RefreshCw,
  TrendingUp,
  TrendingDown,
  Shield,
  Layers,
  Activity,
  AlertCircle,
  Eye,
  Sliders,
  CheckCircle2,
  Clock,
  Zap,
  Target,
  ArrowUpRight,
  ArrowDownRight,
  Radio,
} from 'lucide-react';
import {
  getChartData,
  searchChartSymbols,
  ChartPayload,
  ChartSearchMatch,
  WallItem,
} from '../api';

interface ChartsTerminalPageProps {
  token?: string;
  initialSymbol?: string;
}

const PRESET_SYMBOLS = ['SAIL', 'HEG', 'TATASTEEL', 'RELIANCE', 'INFY', 'HDFCBANK', 'ICICIBANK', 'ITC'];

export function ChartsTerminalPage({ token: _token, initialSymbol }: ChartsTerminalPageProps) {
  // State: Idle by default (null) so zero background processing or polling occurs until user selects a symbol
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(() => initialSymbol || null);
  const [interval, setIntervalVal] = useState<'1m' | '5m' | '15m'>('5m');
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [chartData, setChartData] = useState<ChartPayload | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(false);

  // Search state
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [searchMatches, setSearchMatches] = useState<ChartSearchMatch[]>([]);
  const [isSearching, setIsSearching] = useState<boolean>(false);
  const [showDropdown, setShowDropdown] = useState<boolean>(false);
  const searchContainerRef = useRef<HTMLDivElement>(null);

  // Indicator visibility toggles
  const [showVwap, setShowVwap] = useState<boolean>(true);
  const [showBands1, setShowBands1] = useState<boolean>(true);
  const [showBands2, setShowBands2] = useState<boolean>(true);
  const [showBands3, setShowBands3] = useState<boolean>(false);
  const [showWalls, setShowWalls] = useState<boolean>(true);
  const [showLevels, setShowLevels] = useState<boolean>(true);

  // Crosshair HUD state
  const [hoverData, setHoverData] = useState<{
    time?: string;
    open?: number;
    high?: number;
    low?: number;
    close?: number;
    volume?: number;
    vwap?: number;
    delta?: number;
  } | null>(null);

  // Chart refs
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartApiRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick', UTCTimestamp> | null>(null);
  const vwapSeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const upper1SeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const lower1SeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const upper2SeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const lower2SeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const upper3SeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const lower3SeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const deltaSeriesRef = useRef<ISeriesApi<'Histogram', UTCTimestamp> | null>(null);
  const cvdSeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const activePriceLinesRef = useRef<IPriceLine[]>([]);
  const lastLoadedKeyRef = useRef<string>('');

  // Sync initialSymbol if prop changes
  useEffect(() => {
    if (initialSymbol && initialSymbol !== selectedSymbol) {
      setSelectedSymbol(initialSymbol);
    }
  }, [initialSymbol]);

  const sidebarContainerRef = useRef<HTMLDivElement>(null);

  // Reset sidebar scroll when switching symbols
  useEffect(() => {
    lastLoadedKeyRef.current = '';
    if (sidebarContainerRef.current) {
      sidebarContainerRef.current.scrollTop = 0;
    }
  }, [selectedSymbol]);

  // Click outside to close search dropdown
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (searchContainerRef.current && !searchContainerRef.current.contains(e.target as Node)) {
        setShowDropdown(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Debounced search
  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchMatches([]);
      setShowDropdown(false);
      return;
    }
    const timer = setTimeout(async () => {
      setIsSearching(true);
      try {
        const matches = await searchChartSymbols(searchQuery.trim());
        setSearchMatches(matches);
        setShowDropdown(true);
      } catch (err) {
        console.error('Search error:', err);
      } finally {
        setIsSearching(false);
      }
    }, 250);

    return () => clearTimeout(timer);
  }, [searchQuery]);

  // Fetch Chart Data (on-demand only when a symbol is actively chosen)
  const fetchData = useCallback(
    async (isPolling: boolean = false) => {
      if (!selectedSymbol) {
        setLoading(false);
        return;
      }
      if (!isPolling) {
        setLoading(true);
      }
      try {
        const data = await getChartData(selectedSymbol, interval);
        setChartData(data);
        setLastUpdated(new Date());
        setError(null);
      } catch (err: any) {
        console.error('Failed to fetch chart payload:', err);
        if (!isPolling) {
          setError(err?.response?.data?.detail || err?.message || 'Failed to load chart data');
        }
      } finally {
        if (!isPolling) {
          setLoading(false);
        }
      }
    },
    [selectedSymbol, interval]
  );

  useEffect(() => {
    if (selectedSymbol) {
      fetchData(false);
    } else {
      setChartData(null);
      setLoading(false);
    }
  }, [selectedSymbol, interval, fetchData]);

  // Auto refresh interval (only when active symbol selected AND document is visible)
  useEffect(() => {
    if (!autoRefresh || !selectedSymbol) return;
    const intervalId = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
        fetchData(true);
      }
    }, 6000);
    return () => window.clearInterval(intervalId);
  }, [autoRefresh, selectedSymbol, fetchData]);

  // Initialize and update Lightweight Charts Canvas
  useEffect(() => {
    if (!chartContainerRef.current) return;

    // Clean up prior chart instance
    if (chartApiRef.current) {
      chartApiRef.current.remove();
      chartApiRef.current = null;
    }

    if (!selectedSymbol) return;

    const container = chartContainerRef.current;
    const chart = createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight || 560,
      layout: {
        background: { type: ColorType.Solid, color: '#090D16' },
        textColor: '#94A3B8',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: 'rgba(255, 255, 255, 0.04)' },
        horzLines: { color: 'rgba(255, 255, 255, 0.04)' },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: {
          color: 'rgba(56, 189, 248, 0.4)',
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: '#0284C7',
        },
        horzLine: {
          color: 'rgba(56, 189, 248, 0.4)',
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: '#0284C7',
        },
      },
      localization: {
        locale: IST_LOCALE,
        timeFormatter: (time: any) => {
          const ts = typeof time === 'number' ? time : Number((time as any)?.timestamp || 0);
          return formatISTDateTime(ts, false);
        },
      },
      timeScale: {
        borderColor: 'rgba(255, 255, 255, 0.08)',
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6,
        lockVisibleTimeRangeOnResize: true,
        tickMarkFormatter: (time: any, tickMarkType: TickMarkType) => {
          const ts = typeof time === 'number' ? time : Number((time as any)?.timestamp || 0);
          if (!ts) return null;
          const d = new Date(ts * 1000);
          if (tickMarkType === TickMarkType.Time || tickMarkType === TickMarkType.TimeWithSeconds) {
            return d.toLocaleTimeString(IST_LOCALE, {
              timeZone: IST_TIMEZONE,
              hour: '2-digit',
              minute: '2-digit',
              hour12: false,
            });
          }
          if (tickMarkType === TickMarkType.DayOfMonth) {
            return d.toLocaleDateString(IST_LOCALE, {
              timeZone: IST_TIMEZONE,
              day: 'numeric',
              month: 'short',
            });
          }
          if (tickMarkType === TickMarkType.Month) {
            return d.toLocaleDateString(IST_LOCALE, {
              timeZone: IST_TIMEZONE,
              month: 'short',
            });
          }
          return d.toLocaleDateString(IST_LOCALE, {
            timeZone: IST_TIMEZONE,
            year: 'numeric',
          });
        },
      },
      rightPriceScale: {
        borderColor: 'rgba(255, 255, 255, 0.08)',
        scaleMargins: {
          top: 0.1,
          bottom: 0.15,
        },
      },
    });

    chartApiRef.current = chart;

    // Pane 0: Candlesticks
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#10B981',
      downColor: '#EF4444',
      borderVisible: false,
      wickUpColor: '#10B981',
      wickDownColor: '#EF4444',
      priceFormat: {
        type: 'price',
        precision: 2,
        minMove: 0.05,
      },
    }, 0);
    candleSeriesRef.current = candleSeries;

    // Pane 0: VWAP line
    const vwapSeries = chart.addSeries(LineSeries, {
      color: '#F59E0B',
      lineWidth: 2,
      priceLineVisible: false,
      title: 'VWAP',
    }, 0);
    vwapSeriesRef.current = vwapSeries;

    // Pane 0: ±1σ Bands
    const upper1 = chart.addSeries(LineSeries, {
      color: '#60A5FA',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      title: '+1σ',
    }, 0);
    upper1SeriesRef.current = upper1;

    const lower1 = chart.addSeries(LineSeries, {
      color: '#60A5FA',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      title: '-1σ',
    }, 0);
    lower1SeriesRef.current = lower1;

    // Pane 0: ±2σ Bands
    const upper2 = chart.addSeries(LineSeries, {
      color: '#A78BFA',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      title: '+2σ',
    }, 0);
    upper2SeriesRef.current = upper2;

    const lower2 = chart.addSeries(LineSeries, {
      color: '#A78BFA',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      title: '-2σ',
    }, 0);
    lower2SeriesRef.current = lower2;

    // Pane 0: ±3σ Bands
    const upper3 = chart.addSeries(LineSeries, {
      color: '#F472B6',
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      priceLineVisible: false,
      title: '+3σ',
    }, 0);
    upper3SeriesRef.current = upper3;

    const lower3 = chart.addSeries(LineSeries, {
      color: '#F472B6',
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      priceLineVisible: false,
      title: '-3σ',
    }, 0);
    lower3SeriesRef.current = lower3;

    // Pane 1: Order Flow Volume Delta Histogram
    const deltaSeries = chart.addSeries(HistogramSeries, {
      priceFormat: {
        type: 'volume',
      },
      priceScaleId: 'delta-scale',
    }, 1);
    deltaSeriesRef.current = deltaSeries;

    // Pane 1: CVD Line Overlay
    const cvdSeries = chart.addSeries(LineSeries, {
      color: '#38BDF8',
      lineWidth: 2,
      priceLineVisible: false,
      priceScaleId: 'cvd-scale',
    }, 1);
    cvdSeriesRef.current = cvdSeries;

    // Auto-resize observer
    const resizeObserver = new ResizeObserver((entries) => {
      if (entries.length === 0 || !entries[0].contentRect) return;
      const { width, height } = entries[0].contentRect;
      if (chartApiRef.current && width > 0 && height > 0) {
        chartApiRef.current.resize(width, height);
      }
    });
    resizeObserver.observe(container);

    // Crosshair HUD listener
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.seriesData) {
        setHoverData(null);
        return;
      }
      const candleData = param.seriesData.get(candleSeries) as any;
      const vwapData = param.seriesData.get(vwapSeries) as any;
      const deltaData = param.seriesData.get(deltaSeries) as any;

      if (candleData) {
        setHoverData({
          time: formatISTDateTime(param.time as number, false),
          open: candleData.open,
          high: candleData.high,
          low: candleData.low,
          close: candleData.close,
          volume: candleData.volume,
          vwap: vwapData?.value,
          delta: deltaData?.value,
        });
      } else {
        setHoverData(null);
      }
    });

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartApiRef.current = null;
    };
  }, []);

  // Push updated candle and indicator data into series
  useEffect(() => {
    if (!chartData || !chartApiRef.current || !candleSeriesRef.current) return;

    // Helper to deduplicate and sort by ascending time
    const cleanSeries = <T extends { time: number }>(items: T[]): T[] => {
      const seen = new Set<number>();
      const result: T[] = [];
      for (const it of items) {
        if (!seen.has(it.time)) {
          seen.add(it.time);
          result.push(it);
        }
      }
      return result.sort((a, b) => a.time - b.time);
    };

    const formattedCandles = cleanSeries(chartData.candles).map((c) => ({
      time: c.time as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));

    if (formattedCandles.length > 0) {
      candleSeriesRef.current.setData(formattedCandles);
    }

    // Set VWAP
    if (vwapSeriesRef.current) {
      if (showVwap && chartData.indicators?.vwap) {
        vwapSeriesRef.current.setData(
          cleanSeries(chartData.indicators.vwap).map((p) => ({
            time: p.time as UTCTimestamp,
            value: p.value,
          }))
        );
      } else {
        vwapSeriesRef.current.setData([]);
      }
    }

    // Set Bands 1
    if (upper1SeriesRef.current && lower1SeriesRef.current) {
      if (showBands1 && chartData.indicators?.vwap_upper_1 && chartData.indicators?.vwap_lower_1) {
        upper1SeriesRef.current.setData(
          cleanSeries(chartData.indicators.vwap_upper_1).map((p) => ({
            time: p.time as UTCTimestamp,
            value: p.value,
          }))
        );
        lower1SeriesRef.current.setData(
          cleanSeries(chartData.indicators.vwap_lower_1).map((p) => ({
            time: p.time as UTCTimestamp,
            value: p.value,
          }))
        );
      } else {
        upper1SeriesRef.current.setData([]);
        lower1SeriesRef.current.setData([]);
      }
    }

    // Set Bands 2
    if (upper2SeriesRef.current && lower2SeriesRef.current) {
      if (showBands2 && chartData.indicators?.vwap_upper_2 && chartData.indicators?.vwap_lower_2) {
        upper2SeriesRef.current.setData(
          cleanSeries(chartData.indicators.vwap_upper_2).map((p) => ({
            time: p.time as UTCTimestamp,
            value: p.value,
          }))
        );
        lower2SeriesRef.current.setData(
          cleanSeries(chartData.indicators.vwap_lower_2).map((p) => ({
            time: p.time as UTCTimestamp,
            value: p.value,
          }))
        );
      } else {
        upper2SeriesRef.current.setData([]);
        lower2SeriesRef.current.setData([]);
      }
    }

    // Set Bands 3
    if (upper3SeriesRef.current && lower3SeriesRef.current) {
      if (showBands3 && chartData.indicators?.vwap_upper_3 && chartData.indicators?.vwap_lower_3) {
        upper3SeriesRef.current.setData(
          cleanSeries(chartData.indicators.vwap_upper_3).map((p) => ({
            time: p.time as UTCTimestamp,
            value: p.value,
          }))
        );
        lower3SeriesRef.current.setData(
          cleanSeries(chartData.indicators.vwap_lower_3).map((p) => ({
            time: p.time as UTCTimestamp,
            value: p.value,
          }))
        );
      } else {
        upper3SeriesRef.current.setData([]);
        lower3SeriesRef.current.setData([]);
      }
    }

    // Set Volume Delta
    if (deltaSeriesRef.current && chartData.indicators?.volume_delta) {
      deltaSeriesRef.current.setData(
        cleanSeries(chartData.indicators.volume_delta).map((p) => ({
          time: p.time as UTCTimestamp,
          value: p.value,
          color: p.value >= 0 ? '#10B981' : '#EF4444',
        }))
      );
    }

    // Set CVD Line
    if (cvdSeriesRef.current && chartData.indicators?.cvd) {
      cvdSeriesRef.current.setData(
        cleanSeries(chartData.indicators.cvd).map((p) => ({
          time: p.time as UTCTimestamp,
          value: p.value,
        }))
      );
    }

    // Clear prior price lines
    if (candleSeriesRef.current) {
      for (const pl of activePriceLinesRef.current) {
        try {
          candleSeriesRef.current.removePriceLine(pl);
        } catch {
          // ignore
        }
      }
      activePriceLinesRef.current = [];

      // Add Trade Levels
      if (showLevels && chartData.quant_metrics?.levels) {
        const lvl = chartData.quant_metrics.levels;
        if (lvl.entry > 0) {
          const entryLine = candleSeriesRef.current.createPriceLine({
            price: lvl.entry,
            color: '#3B82F6',
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: true,
            title: `ENTRY ₹${lvl.entry.toFixed(2)}`,
          });
          activePriceLinesRef.current.push(entryLine);
        }
        if (lvl.sl > 0) {
          const slLine = candleSeriesRef.current.createPriceLine({
            price: lvl.sl,
            color: '#EF4444',
            lineWidth: 1,
            lineStyle: LineStyle.Solid,
            axisLabelVisible: true,
            title: `SL ₹${lvl.sl.toFixed(2)}`,
          });
          activePriceLinesRef.current.push(slLine);
        }
        if (lvl.target1 > 0) {
          const tp1Line = candleSeriesRef.current.createPriceLine({
            price: lvl.target1,
            color: '#10B981',
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: true,
            title: `TP1 ₹${lvl.target1.toFixed(2)}`,
          });
          activePriceLinesRef.current.push(tp1Line);
        }
        if (lvl.target2 > 0) {
          const tp2Line = candleSeriesRef.current.createPriceLine({
            price: lvl.target2,
            color: '#059669',
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: true,
            title: `TP2 ₹${lvl.target2.toFixed(2)}`,
          });
          activePriceLinesRef.current.push(tp2Line);
        }
      }

      // Add Institutional Order Walls
      if (showWalls && chartData.order_flow?.institutional_walls) {
        chartData.order_flow.institutional_walls.slice(0, 4).forEach((wall) => {
          const isAsk = wall.side === 'ask';
          const line = candleSeriesRef.current!.createPriceLine({
            price: wall.price,
            color: isAsk ? 'rgba(239, 68, 68, 0.85)' : 'rgba(16, 185, 129, 0.85)',
            lineWidth: 1,
            lineStyle: LineStyle.Dotted,
            axisLabelVisible: true,
            title: `${isAsk ? 'ASK WALL' : 'BID WALL'} ${wall.strength.toFixed(1)}x`,
          });
          activePriceLinesRef.current.push(line);
        });
      }
    }

    // Only scroll to latest candle when a new symbol or interval is first loaded
    // Preserves the user's manual pan, drag, and zoom position during periodic live polling & indicator toggling
    const currentKey = `${selectedSymbol}_${interval}`;
    if (lastLoadedKeyRef.current !== currentKey && formattedCandles.length > 0) {
      chartApiRef.current.timeScale().scrollToRealTime();
      lastLoadedKeyRef.current = currentKey;
    }
  }, [
    chartData,
    showVwap,
    showBands1,
    showBands2,
    showBands3,
    showWalls,
    showLevels,
  ]);

  // Handle preset chip click
  const handleSelectSymbol = (sym: string) => {
    setSelectedSymbol(sym);
    setSearchQuery('');
    setShowDropdown(false);
  };

  // Compute stats & indicators
  const stats = useMemo(() => {
    if (!chartData) return null;
    const isUp = chartData.change_pct >= 0;
    const quant = chartData.quant_metrics;
    const orderFlow = chartData.order_flow;

    // Detect institutional walls presence and absorption
    const walls = orderFlow?.institutional_walls || [];
    const hasAbsorption = walls.some((w) => w.is_absorption);
    const heavyAskWalls = walls.filter((w) => w.side === 'ask');
    const heavyBidWalls = walls.filter((w) => w.side === 'bid');

    return {
      isUp,
      quant,
      orderFlow,
      walls,
      hasAbsorption,
      heavyAskWalls,
      heavyBidWalls,
    };
  }, [chartData]);



  return (
    <div className="flex flex-col h-[calc(100vh-2rem)] space-y-3 p-4 overflow-hidden text-slate-100">
      {/* ── Top Bar: Symbol Search, Presets, Timeframes, Toggles ───────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-2.5 glass-panel rounded-2xl border border-white/10 bg-slate-900/60 shadow-lg backdrop-blur-md">
        {/* Left: Universal Symbol Search */}
        <div className="flex items-center gap-3">
          <div className="relative" ref={searchContainerRef}>
            <div className="flex items-center bg-black/40 border border-white/15 rounded-xl px-3 py-1.5 focus-within:border-cyan-400/80 transition-colors w-64 md:w-72">
              <Search size={16} className="text-slate-400 mr-2 shrink-0" />
              <input
                type="text"
                placeholder="Search symbol (e.g. SAIL, HEG)..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onFocus={() => {
                  if (searchMatches.length > 0) setShowDropdown(true);
                }}
                className="bg-transparent border-none outline-none text-xs text-white placeholder-slate-400 w-full font-medium uppercase"
              />
              {isSearching && <RefreshCw size={14} className="animate-spin text-cyan-400 shrink-0" />}
            </div>

            {/* Dropdown Suggestions */}
            {showDropdown && searchMatches.length > 0 && (
              <div className="absolute left-0 top-full mt-2 w-80 bg-slate-900/95 border border-cyan-500/30 rounded-xl shadow-2xl backdrop-blur-xl z-50 overflow-hidden max-h-72 overflow-y-auto custom-scrollbar">
                <div className="p-2 border-b border-white/10 text-[10px] uppercase font-bold text-slate-400 tracking-wider">
                  Instruments ({searchMatches.length})
                </div>
                {searchMatches.map((m) => (
                  <button
                    key={`${m.segment}-${m.symbol}`}
                    onClick={() => handleSelectSymbol(m.symbol)}
                    className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-cyan-500/15 transition-colors border-b border-white/5 last:border-none"
                  >
                    <div className="flex flex-col">
                      <span className="font-bold text-xs text-white flex items-center gap-1.5">
                        {m.symbol}
                        <span className="text-[9px] px-1.5 py-0.5 rounded bg-white/10 text-cyan-200 font-mono">
                          {m.segment}
                        </span>
                      </span>
                      <span className="text-[10px] text-slate-400 truncate max-w-[190px]">
                        {m.name}
                      </span>
                    </div>
                    <span className="text-[10px] font-mono text-slate-500">ID: {m.security_id}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Quick Presets */}
          <div className="hidden lg:flex items-center gap-1.5">
            {PRESET_SYMBOLS.map((sym) => (
              <button
                key={sym}
                onClick={() => handleSelectSymbol(sym)}
                className={`px-2.5 py-1 text-xs font-semibold rounded-lg transition-all ${
                  selectedSymbol === sym
                    ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-400/40 shadow-sm shadow-cyan-500/20'
                    : 'text-slate-400 hover:text-white hover:bg-white/5 border border-transparent'
                }`}
              >
                {sym}
              </button>
            ))}
          </div>
        </div>

        {/* Center: Interval Switchers */}
        <div className="flex items-center gap-1 bg-black/40 border border-white/15 rounded-xl p-1">
          {(['1m', '5m', '15m'] as const).map((int) => (
            <button
              key={int}
              onClick={() => {
                lastLoadedKeyRef.current = '';
                setIntervalVal(int);
              }}
              className={`px-3 py-1 text-xs font-bold rounded-lg transition-all ${
                interval === int
                  ? 'bg-cyan-500 text-black shadow-md shadow-cyan-500/20'
                  : 'text-slate-400 hover:text-white hover:bg-white/10'
              }`}
            >
              {int}
            </button>
          ))}
        </div>

        {/* Right: Indicators & Controls */}
        <div className="flex items-center gap-2">
          {/* Indicator toggles */}
          <div className="hidden md:flex items-center gap-1 text-[11px]">
            <button
              onClick={() => setShowVwap(!showVwap)}
              className={`px-2 py-1 rounded border transition-colors ${
                showVwap
                  ? 'bg-amber-500/20 border-amber-400/50 text-amber-300 font-semibold'
                  : 'border-white/10 text-slate-400 line-through'
              }`}
            >
              VWAP
            </button>
            <button
              onClick={() => setShowBands1(!showBands1)}
              className={`px-2 py-1 rounded border transition-colors ${
                showBands1
                  ? 'bg-blue-500/20 border-blue-400/50 text-blue-300 font-semibold'
                  : 'border-white/10 text-slate-400 line-through'
              }`}
            >
              ±1σ
            </button>
            <button
              onClick={() => setShowBands2(!showBands2)}
              className={`px-2 py-1 rounded border transition-colors ${
                showBands2
                  ? 'bg-purple-500/20 border-purple-400/50 text-purple-300 font-semibold'
                  : 'border-white/10 text-slate-400 line-through'
              }`}
            >
              ±2σ
            </button>
            <button
              onClick={() => setShowBands3(!showBands3)}
              className={`px-2 py-1 rounded border transition-colors ${
                showBands3
                  ? 'bg-pink-500/20 border-pink-400/50 text-pink-300 font-semibold'
                  : 'border-white/10 text-slate-400 line-through'
              }`}
            >
              ±3σ
            </button>
            <button
              onClick={() => setShowWalls(!showWalls)}
              className={`px-2 py-1 rounded border transition-colors ${
                showWalls
                  ? 'bg-emerald-500/20 border-emerald-400/50 text-emerald-300 font-semibold'
                  : 'border-white/10 text-slate-400 line-through'
              }`}
            >
              Walls
            </button>
            <button
              onClick={() => setShowLevels(!showLevels)}
              className={`px-2 py-1 rounded border transition-colors ${
                showLevels
                  ? 'bg-cyan-500/20 border-cyan-400/50 text-cyan-300 font-semibold'
                  : 'border-white/10 text-slate-400 line-through'
              }`}
            >
              Levels
            </button>
          </div>

          {/* Auto refresh status & manual trigger */}
          <button
            onClick={() => setAutoRefresh(!autoRefresh)}
            className={`p-1.5 rounded-lg border text-xs flex items-center gap-1 transition-colors ${
              autoRefresh
                ? 'bg-emerald-500/15 border-emerald-400/40 text-emerald-300'
                : 'bg-white/5 border-white/10 text-slate-400'
            }`}
            title={autoRefresh ? 'Live Stream On (Every 6s)' : 'Live Stream Paused'}
          >
            <Radio size={14} className={autoRefresh ? 'text-emerald-400 animate-pulse' : 'text-slate-500'} />
            <span className="text-[10px] hidden sm:inline">{autoRefresh ? 'LIVE' : 'PAUSED'}</span>
          </button>

          <button
            onClick={() => fetchData(false)}
            disabled={loading}
            className="p-1.5 rounded-lg border border-white/15 bg-white/5 hover:bg-white/10 text-slate-300 hover:text-white transition-all disabled:opacity-50"
            title="Refresh Bar & Flow Now"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin text-cyan-400' : ''} />
          </button>
        </div>
      </div>

      {/* ── Main Layout: 75% Multi-pane Canvas + 25% Microstructure Rail ──────── */}
      <div className="flex-1 grid grid-cols-1 lg:grid-cols-4 gap-3 min-h-0">
        {/* Left 3 cols: Canvas Container with HUD overlay */}
        <div className="lg:col-span-3 flex flex-col glass-panel rounded-2xl border border-white/10 bg-slate-950/80 overflow-hidden relative shadow-2xl">
          {/* Header Bar within Chart: Active Symbol Price & Crosshair HUD */}
          <div className="flex flex-wrap items-center justify-between px-4 py-2 bg-black/40 border-b border-white/10 z-10 text-xs">
            {/* Symbol & Live Price Header */}
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-2">
                <span className="font-black text-base text-white tracking-wide">{selectedSymbol || 'NO SYMBOL SELECTED'}</span>
                <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-300 border border-cyan-400/30">
                  {interval.toUpperCase()}
                </span>
                <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-300 border border-emerald-400/30">
                  IST
                </span>
              </div>

              {chartData && (
                <div className="flex items-baseline gap-2">
                  <span className="text-lg font-black text-white">
                    ₹{chartData.last_price.toFixed(2)}
                  </span>
                  <span
                    className={`font-bold text-xs flex items-center ${
                      stats?.isUp ? 'text-emerald-400' : 'text-rose-400'
                    }`}
                  >
                    {stats?.isUp ? '+' : ''}
                    {chartData.change_pct.toFixed(2)}%
                  </span>
                </div>
              )}
            </div>

            {/* Crosshair Floating HUD */}
            {hoverData ? (
              <div className="flex items-center gap-3 text-[11px] font-mono bg-slate-900/90 border border-white/10 rounded-lg px-2.5 py-1 text-slate-300 shadow-md">
                <span className="text-cyan-300 font-bold">{hoverData.time}</span>
                <span>O: <strong className="text-white">{hoverData.open?.toFixed(2)}</strong></span>
                <span>H: <strong className="text-emerald-400">{hoverData.high?.toFixed(2)}</strong></span>
                <span>L: <strong className="text-rose-400">{hoverData.low?.toFixed(2)}</strong></span>
                <span>C: <strong className="text-cyan-300">{hoverData.close?.toFixed(2)}</strong></span>
                {hoverData.vwap !== undefined && (
                  <span>VWAP: <strong className="text-amber-400">{hoverData.vwap.toFixed(2)}</strong></span>
                )}
                {hoverData.delta !== undefined && (
                  <span>
                    Δ: <strong className={hoverData.delta >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                      {hoverData.delta >= 0 ? '+' : ''}{hoverData.delta.toLocaleString()}
                    </strong>
                  </span>
                )}
              </div>
            ) : (
              chartData && (
                <div className="hidden sm:flex items-center gap-3 text-[11px] font-mono text-slate-400">
                  <span>H: <strong className="text-white">₹{chartData.day_high.toFixed(2)}</strong></span>
                  <span>L: <strong className="text-white">₹{chartData.day_low.toFixed(2)}</strong></span>
                  <span>VWAP: <strong className="text-amber-300">₹{chartData.vwap.toFixed(2)}</strong></span>
                  {lastUpdated && (
                    <span className="text-slate-500 text-[10px]">
                      Synced {formatISTTime(lastUpdated, true)} IST
                    </span>
                  )}
                </div>
              )
            )}
          </div>

          {/* Institutional Wall & Absorption Alert Banner */}
          {stats?.hasAbsorption && (
            <div className="bg-rose-500/15 border-b border-rose-500/30 px-4 py-1.5 flex items-center justify-between text-xs text-rose-200 z-10">
              <div className="flex items-center gap-2 font-bold">
                <AlertCircle size={15} className="text-rose-400 animate-pulse" />
                <span>INSTITUTIONAL ABSORPTION DETECTED:</span>
                <span className="text-slate-300 font-normal">
                  Aggressive limit orders absorbing tape without price progression. Breakout imminent.
                </span>
              </div>
              <span className="text-[10px] uppercase font-mono px-2 py-0.5 rounded bg-rose-500/30 text-rose-100">
                High Gamma Risk
              </span>
            </div>
          )}

          {/* Canvas Chart Mount Node */}
          <div ref={chartContainerRef} className="flex-1 w-full h-full relative" />

          {/* Quick Snap to Real-Time Floating Button */}
          <button
            onClick={() => {
              if (chartApiRef.current) {
                chartApiRef.current.timeScale().scrollToRealTime();
              }
            }}
            className="absolute bottom-4 right-16 z-10 px-2.5 py-1 bg-slate-900/90 hover:bg-slate-800 text-cyan-300 border border-cyan-500/30 hover:border-cyan-400 rounded-lg text-[10px] font-mono font-bold shadow-lg backdrop-blur-md flex items-center gap-1.5 transition-all opacity-70 hover:opacity-100 active:scale-95"
            title="Snap view to real-time live bar"
          >
            <Clock size={11} className="text-cyan-400" />
            <span>Live Bar</span>
          </button>

          {/* Standby Overlay when no symbol is selected */}
          {!selectedSymbol && (
            <div className="absolute inset-0 bg-slate-950/95 flex flex-col items-center justify-center z-20 p-8 text-center backdrop-blur-sm">
              <div className="w-16 h-16 rounded-2xl bg-cyan-500/10 border border-cyan-500/30 flex items-center justify-center mb-4 text-cyan-400 shadow-neon-blue">
                <Search size={30} />
              </div>
              <h3 className="text-base font-bold text-white mb-2">Pro Charts &amp; Order Flow — Standby Mode</h3>
              <p className="text-xs text-slate-400 max-w-md mb-6 leading-relaxed">
                Zero background processing or polling is active. Search any stock above or click a quick-pick preset below to inspect live candlestick, orderflow, and VWAP bands.
              </p>
              <div className="flex flex-wrap items-center justify-center gap-2 max-w-lg">
                {PRESET_SYMBOLS.map((sym) => (
                  <button
                    key={sym}
                    onClick={() => handleSelectSymbol(sym)}
                    className="px-3 py-1.5 rounded-xl bg-slate-900 border border-white/10 hover:border-cyan-400/50 hover:bg-cyan-500/10 text-xs font-semibold text-slate-300 hover:text-cyan-300 transition-all shadow-sm"
                  >
                    {sym}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Loading Overlay */}
          {loading && !chartData && (
            <div className="absolute inset-0 bg-slate-950/75 flex flex-col items-center justify-center z-20 backdrop-blur-sm">
              <RefreshCw size={32} className="animate-spin text-cyan-400 mb-3" />
              <p className="text-xs font-semibold text-slate-300 tracking-wider">
                COMPUTING MULTI-PANE ORDERFLOW &amp; VWAP BANDS...
              </p>
            </div>
          )}

          {/* Error Banner */}
          {error && (
            <div className="absolute inset-0 bg-slate-950/90 flex flex-col items-center justify-center z-20 p-6">
              <AlertCircle size={36} className="text-rose-400 mb-3" />
              <p className="text-sm font-bold text-rose-300 mb-2">Error Loading Instrument Telemetry</p>
              <p className="text-xs text-slate-400 text-center max-w-md mb-4">{error}</p>
              <button
                onClick={() => fetchData(false)}
                className="px-4 py-2 bg-cyan-500/20 border border-cyan-400/40 text-cyan-200 text-xs rounded-xl hover:bg-cyan-500/30 font-medium"
              >
                Retry Request
              </button>
            </div>
          )}
        </div>

        {/* Right 1 col: Intelligence & Order Flow Rail */}
        <div ref={sidebarContainerRef} className="flex flex-col space-y-3 overflow-y-auto custom-scrollbar pr-0.5">

          {/* 1. Quant Setup & State Machine Card */}
          <div className="glass-panel rounded-2xl border border-white/10 bg-slate-900/70 p-3.5 shadow-lg">
            <div className="flex items-center justify-between mb-2">
              <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400 flex items-center gap-1.5">
                <Zap size={13} className="text-cyan-400" />
                Quant State Machine
              </span>
              <span
                className={`text-[10px] font-extrabold uppercase px-2 py-0.5 rounded-full ${
                  stats?.quant?.signal === 'BUY'
                    ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40'
                    : stats?.quant?.signal === 'SELL'
                    ? 'bg-rose-500/20 text-rose-300 border border-rose-500/40'
                    : 'bg-slate-700/50 text-slate-300 border border-slate-600/40'
                }`}
              >
                {stats?.quant?.signal || 'NEUTRAL'}
              </span>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs">
                <span className="text-slate-400">Setup State</span>
                <span className="font-semibold text-white">
                  {stats?.quant?.state || 'Scanning Market'}
                </span>
              </div>

              <div className="flex items-center justify-between text-xs">
                <span className="text-slate-400">Bayesian Confidence</span>
                <span className="font-bold text-cyan-300 font-mono">
                  {stats?.quant?.confidence ? `${stats.quant.confidence.toFixed(1)}%` : '—'}
                </span>
              </div>

              <div className="flex items-center justify-between text-xs">
                <span className="text-slate-400">Market Regime</span>
                <span className="font-medium text-slate-200">
                  {stats?.quant?.regime || 'Calm Neutral'}
                </span>
              </div>

              <div className="flex items-center justify-between text-xs">
                <span className="text-slate-400">C-Value Compression</span>
                <span className="font-mono text-xs text-amber-300">
                  {stats?.quant?.c_value !== undefined ? stats.quant.c_value.toFixed(3) : '—'}
                </span>
              </div>

              <div className="flex items-center justify-between text-xs">
                <span className="text-slate-400">VCP Volatility State</span>
                <span
                  className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                    stats?.quant?.vcp_confirmed
                      ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                      : 'bg-white/5 text-slate-400'
                  }`}
                >
                  {stats?.quant?.vcp_confirmed ? 'Confirmed Contracted' : 'Pending Contraction'}
                </span>
              </div>
            </div>
          </div>

          {/* 2. Order Flow & OBI Microstructure Meter */}
          <div className="glass-panel rounded-2xl border border-white/10 bg-slate-900/70 p-3.5 shadow-lg">
            <div className="flex items-center justify-between mb-2.5">
              <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400 flex items-center gap-1.5">
                <Activity size={13} className="text-amber-400" />
                Live Order Flow &amp; OBI
              </span>
              <span className="text-[10px] font-mono text-slate-400">
                CVD: {stats?.orderFlow?.running_cvd ? stats.orderFlow.running_cvd.toLocaleString() : '0'}
              </span>
            </div>

            {/* OBI Meter */}
            <div className="space-y-1.5 mb-3">
              <div className="flex justify-between text-[11px]">
                <span className="text-slate-400">Order Book Imbalance</span>
                <span
                  className={`font-mono font-bold ${
                    (stats?.orderFlow?.deep_obi || 0) > 0.1
                      ? 'text-emerald-400'
                      : (stats?.orderFlow?.deep_obi || 0) < -0.1
                      ? 'text-rose-400'
                      : 'text-slate-300'
                  }`}
                >
                  {stats?.orderFlow?.deep_obi !== undefined
                    ? `${(stats.orderFlow.deep_obi * 100).toFixed(1)}%`
                    : '0.0%'}
                </span>
              </div>

              {/* Visual gauge bar */}
              <div className="h-2 w-full bg-slate-800 rounded-full overflow-hidden flex">
                <div
                  className="h-full bg-rose-500 transition-all duration-300"
                  style={{
                    width: `${Math.max(0, 50 - ((stats?.orderFlow?.deep_obi || 0) * 50))}%`,
                  }}
                  title="Ask Depth"
                />
                <div
                  className="h-full bg-emerald-500 transition-all duration-300"
                  style={{
                    width: `${Math.max(0, 50 + ((stats?.orderFlow?.deep_obi || 0) * 50))}%`,
                  }}
                  title="Bid Depth"
                />
              </div>
              <div className="flex justify-between text-[9px] font-mono text-slate-500">
                <span>Ask Pressure</span>
                <span>Bid Dominance</span>
              </div>
            </div>

            {/* Institutional Wall Alerts */}
            <div className="space-y-1.5">
              <span className="text-[10px] uppercase font-bold text-slate-400 tracking-wider">
                Institutional Block Walls ({stats?.walls.length || 0})
              </span>

              {stats?.walls && stats.walls.length > 0 ? (
                <div className="space-y-1.5 max-h-36 overflow-y-auto custom-scrollbar">
                  {stats.walls.map((wall, idx) => (
                    <div
                      key={`${wall.side}-${wall.price}-${idx}`}
                      className={`p-2 rounded-xl border flex items-center justify-between text-xs transition-colors ${
                        wall.side === 'ask'
                          ? 'bg-rose-500/10 border-rose-500/30 text-rose-200'
                          : 'bg-emerald-500/10 border-emerald-500/30 text-emerald-200'
                      }`}
                    >
                      <div className="flex flex-col">
                        <span className="font-bold flex items-center gap-1.5">
                          {wall.side.toUpperCase()} WALL @ ₹{wall.price.toFixed(2)}
                          {wall.is_absorption && (
                            <span className="text-[9px] bg-rose-500 text-white font-black px-1 rounded">
                              ABSORPTION
                            </span>
                          )}
                        </span>
                        <span className="text-[10px] text-slate-400">
                          Qty: <strong>{wall.qty.toLocaleString()}</strong> ({wall.orders || 1} block order
                          {(wall.orders || 1) > 1 ? 's' : ''})
                        </span>
                      </div>
                      <div className="text-right">
                        <span className="font-mono text-xs font-black">
                          {wall.strength.toFixed(1)}x
                        </span>
                        <span className="block text-[9px] text-slate-400">vs Normal Depth</span>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-[11px] text-slate-500 italic p-2 bg-black/20 rounded-xl">
                  No anomalous institutional block walls detected in current visible depth.
                </p>
              )}
            </div>
          </div>

          {/* 3. Expected Moves & Quant ATR Rails */}
          <div className="glass-panel rounded-2xl border border-white/10 bg-slate-900/70 p-3.5 shadow-lg">
            <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400 flex items-center gap-1.5 mb-2.5">
              <Sliders size={13} className="text-purple-400" />
              Statistical Expected Moves
            </span>

            <div className="space-y-2 text-xs">
              <div className="flex items-center justify-between">
                <span className="text-slate-400">EWMA ATR (Volatility)</span>
                <span className="font-mono font-bold text-white">
                  ₹{stats?.quant?.ewma_atr_rs ? stats.quant.ewma_atr_rs.toFixed(2) : '—'}
                </span>
              </div>

              <div className="flex items-center justify-between">
                <span className="text-slate-400">±1 ATR Expected Move (68%)</span>
                <span className="font-mono text-cyan-300 font-semibold">
                  ±₹{stats?.quant?.expected_move_1atr ? stats.quant.expected_move_1atr.toFixed(2) : '—'}
                </span>
              </div>

              <div className="flex items-center justify-between">
                <span className="text-slate-400">±2 ATR Tail Move (95%)</span>
                <span className="font-mono text-purple-300 font-semibold">
                  ±₹{stats?.quant?.expected_move_2atr ? stats.quant.expected_move_2atr.toFixed(2) : '—'}
                </span>
              </div>

              <div className="p-2 rounded-xl bg-black/30 border border-white/5 space-y-1">
                <div className="flex justify-between text-[11px]">
                  <span className="text-slate-400">Projected High</span>
                  <span className="font-mono font-bold text-emerald-400">
                    ₹{stats?.quant?.expected_high ? stats.quant.expected_high.toFixed(2) : '—'}
                  </span>
                </div>
                <div className="flex justify-between text-[11px]">
                  <span className="text-slate-400">Projected Low</span>
                  <span className="font-mono font-bold text-rose-400">
                    ₹{stats?.quant?.expected_low ? stats.quant.expected_low.toFixed(2) : '—'}
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* 4. Execution Levels & Kelly Sizing */}
          <div className="glass-panel rounded-2xl border border-white/10 bg-slate-900/70 p-3.5 shadow-lg">
            <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400 flex items-center gap-1.5 mb-2.5">
              <Shield size={13} className="text-emerald-400" />
              Calculated Execution Levels
            </span>

            {stats?.quant?.levels ? (
              <div className="space-y-1.5 text-xs">
                <div className="flex items-center justify-between p-1.5 rounded-lg bg-blue-500/10 border border-blue-500/25">
                  <span className="text-blue-300 font-medium">Entry</span>
                  <span className="font-mono font-bold text-white">
                    ₹{stats.quant.levels.entry.toFixed(2)}
                  </span>
                </div>

                <div className="flex items-center justify-between p-1.5 rounded-lg bg-rose-500/10 border border-rose-500/25">
                  <span className="text-rose-300 font-medium">Stop Loss</span>
                  <span className="font-mono font-bold text-rose-300">
                    ₹{stats.quant.levels.sl.toFixed(2)}
                  </span>
                </div>

                <div className="flex items-center justify-between p-1.5 rounded-lg bg-emerald-500/10 border border-emerald-500/25">
                  <span className="text-emerald-300 font-medium">Target 1</span>
                  <span className="font-mono font-bold text-emerald-300">
                    ₹{stats.quant.levels.target1.toFixed(2)}
                  </span>
                </div>

                <div className="flex items-center justify-between p-1.5 rounded-lg bg-emerald-500/15 border border-emerald-500/35">
                  <span className="text-emerald-200 font-medium">Target 2</span>
                  <span className="font-mono font-bold text-emerald-200">
                    ₹{stats.quant.levels.target2.toFixed(2)}
                  </span>
                </div>

                <div className="pt-1 flex items-center justify-between text-[11px] font-mono text-slate-400">
                  <span>R:R: <strong className="text-white">{stats.quant.levels.rr_ratio.toFixed(2)}</strong></span>
                  <span>Kelly Qty: <strong className="text-cyan-300">{stats.quant.levels.kelly_qty}</strong></span>
                </div>
              </div>
            ) : (
              <p className="text-[11px] text-slate-500 italic p-2 bg-black/20 rounded-xl">
                Levels will populate when a valid setup pattern is identified.
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
