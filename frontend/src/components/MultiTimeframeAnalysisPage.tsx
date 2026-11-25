import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  createChart,
  createSeriesMarkers,
  ColorType,
  CrosshairMode,
  LineStyle,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
  IChartApi,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  IPriceLine,
  UTCTimestamp,
} from 'lightweight-charts';
import {
  Play,
  Square,
  Search,
  Activity,
  Layers,
  Sparkles,
  Zap,
  Target,
  Shield,
  Clock,
  Filter,
  CheckCircle2,
  TrendingUp,
  TrendingDown,
  Copy,
  Check,
  Newspaper,
  Building2,
  Maximize2,
  BarChart2,
  Radio,
} from 'lucide-react';
import {
  getAnalysisSymbols,
  getSymbolAnalysis,
  getLiveTicks,
  addManualShortlistTrade,
  promoteWatchdogCandidate,
  promoteWatchdogShortsCandidate,
  AnalysisSymbolItem,
  SymbolAnalysisPayload,
} from '../api';
import { saveAnalysisCache, loadAnalysisCache } from '../utils/analysisStorage';

interface MultiTimeframeAnalysisPageProps {
  token?: string;
}

type FilterType = 'all' | 'breakout' | 'bounce' | 'squeeze' | 'breakdown';
type UniverseOption = 'all' | 'nifty50' | 'niftynext50' | 'midcap' | 'smallcap';


export function MultiTimeframeAnalysisPage({ token: _token }: MultiTimeframeAnalysisPageProps) {
  // ── State ───────────────────────────────────────────────────────────────────
  const [universe, setUniverse] = useState<UniverseOption>('all');
  const [symbols, setSymbols] = useState<AnalysisSymbolItem[]>([]);
  const [analyzedStocks, setAnalyzedStocks] = useState<Record<string, SymbolAnalysisPayload>>({});
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [activeTimeframe, setActiveTimeframe] = useState<'1D' | '1W' | '1M'>('1D');

  // Scanning engine state
  const [isScanning, setIsScanning] = useState<boolean>(false);
  const [currentScanIdx, setCurrentScanIdx] = useState<number>(0);
  const [lastScannedAt, setLastScannedAt] = useState<string | null>(null);

  const [searchQuery, setSearchQuery] = useState<string>('');
  const [activeFilter, setActiveFilter] = useState<FilterType>('all');
  const [copied, setCopied] = useState<boolean>(false);
  const [singleCopied, setSingleCopied] = useState<boolean>(false);
  const [isTracking, setIsTracking] = useState<boolean>(false);
  const [trackSuccess, setTrackSuccess] = useState<boolean>(false);

  // ── Live Real-Time Analysis State ─────────────────────────────────────────
  const [isLiveMode, setIsLiveMode] = useState<boolean>(false);
  const [isLiveWarmingUp, setIsLiveWarmingUp] = useState<boolean>(false);
  const [liveTickTimestamp, setLiveTickTimestamp] = useState<string | null>(null);
  const [livePriceFlashes, setLivePriceFlashes] = useState<Record<string, 'up' | 'down'>>({});
  const [liveConnectionLost, setLiveConnectionLost] = useState<boolean>(false);

  const isLiveModeRef = useRef<boolean>(false);
  const liveIntervalRef = useRef<any>(null);
  const liveErrorCountRef = useRef<number>(0);
  const isTickingRef = useRef<boolean>(false);
  const liveEventSequenceRef = useRef<number>(0);

  // Retained for compatibility with the local fallback path; server state is authoritative.
  const triggerSustainsRef = useRef<Record<string, { firstSeen: number; tickCount: number; slipCount: number }>>({});
  const autoPromotedSymbolsRef = useRef<Set<string>>(new Set());

  const analyzedStocksRef = useRef<Record<string, SymbolAnalysisPayload>>(analyzedStocks);
  useEffect(() => {
    analyzedStocksRef.current = analyzedStocks;
  }, [analyzedStocks]);
  const selectedSymbolRef = useRef<string | null>(selectedSymbol);
  useEffect(() => {
    selectedSymbolRef.current = selectedSymbol;
  }, [selectedSymbol]);

  // Chart refs & Crosshair HUD
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartApiRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick', UTCTimestamp> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram', UTCTimestamp> | null>(null);
  const line1SeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const line2SeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const line3SeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const line4SeriesRef = useRef<ISeriesApi<'Line', UTCTimestamp> | null>(null);
  const markersPluginRef = useRef<ISeriesMarkersPluginApi<UTCTimestamp> | null>(null);
  const activePriceLinesRef = useRef<IPriceLine[]>([]);

  // Timeframe & Candle tracking refs for crosshair HUD
  const activeTimeframeRef = useRef<'1D' | '1W' | '1M'>(activeTimeframe);
  activeTimeframeRef.current = activeTimeframe;
  const latestCandleTimeRef = useRef<number | null>(null);

  // Toggles
  const [showVolume, setShowVolume] = useState<boolean>(true);
  const [showLine1, setShowLine1] = useState<boolean>(true);
  const [showLine2, setShowLine2] = useState<boolean>(true);
  const [showLine3, setShowLine3] = useState<boolean>(true);
  const [showLine4, setShowLine4] = useState<boolean>(true);
  const [showLevels, setShowLevels] = useState<boolean>(true);

  // Crosshair Hover
  const [hoverData, setHoverData] = useState<{
    time?: string;
    open?: number;
    high?: number;
    low?: number;
    close?: number;
    volume?: number;
    val1?: number;
    val2?: number;
    val3?: number;
    val4?: number;
    median?: number;
    p10?: number;
    p90?: number;
  } | null>(null);

  const formatVolume = useCallback((vol?: number) => {
    if (vol === undefined || vol === null || isNaN(vol)) return 'N/A';
    if (vol >= 10000000) return `${(vol / 10000000).toFixed(2)} Cr`;
    if (vol >= 100000) return `${(vol / 100000).toFixed(2)} L`;
    if (vol >= 1000) return `${(vol / 1000).toFixed(1)} K`;
    return vol.toLocaleString('en-IN');
  }, []);

  // Scan loop control ref
  const isScanningRef = useRef<boolean>(false);
  isScanningRef.current = isScanning;

  // ── 1. Hydrate from IndexedDB / LocalStorage on mount ────────────────────────
  useEffect(() => {
    let alive = true;
    const hydrate = async () => {
      try {
        const cached = await loadAnalysisCache();
        if (alive && cached && cached.stocks && Object.keys(cached.stocks).length > 0) {
          // Cleanse duplicate legacy ZOMATO entry (Zomato is rebranded to ETERNAL)
          if (cached.stocks['ZOMATO']) {
            if (!cached.stocks['ETERNAL']) {
              cached.stocks['ETERNAL'] = { ...cached.stocks['ZOMATO'], symbol: 'ETERNAL' };
            }
            delete cached.stocks['ZOMATO'];
          }
          if (cached.selectedSymbol === 'ZOMATO') {
            cached.selectedSymbol = 'ETERNAL';
          }

          // Cleanse duplicate legacy PEL entry (Piramal Enterprises is rebranded to PIRAMALFIN)
          if (cached.stocks['PEL']) {
            if (!cached.stocks['PIRAMALFIN']) {
              cached.stocks['PIRAMALFIN'] = { ...cached.stocks['PEL'], symbol: 'PIRAMALFIN' };
            }
            delete cached.stocks['PEL'];
          }
          if (cached.selectedSymbol === 'PEL') {
            cached.selectedSymbol = 'PIRAMALFIN';
          }

          setAnalyzedStocks(cached.stocks);
          setLastScannedAt(cached.timestamp || null);
          if (cached.activeTimeframe) {
            setActiveTimeframe(cached.activeTimeframe as '1D' | '1W' | '1M');
          }
          if (cached.universe) {
            setUniverse(cached.universe as UniverseOption);
          }
          if (cached.selectedSymbol && cached.stocks[cached.selectedSymbol]) {
            setSelectedSymbol(cached.selectedSymbol);
          } else {
            const firstSym = Object.keys(cached.stocks)[0];
            setSelectedSymbol(firstSym);
          }
        }
      } catch (err) {
        console.error('Failed to load cached analysis:', err);
      }
    };
    hydrate();
    return () => {
      alive = false;
    };
  }, []);

  // Save to Persistent Cache whenever analyzedStocks, selectedSymbol, activeTimeframe, or universe updates
  // Skip during live mode to avoid thrashing IndexedDB with multi-MB writes every 3s tick.
  useEffect(() => {
    if (Object.keys(analyzedStocks).length > 0 && !isLiveMode) {
      saveAnalysisCache({
        timestamp: lastScannedAt || new Date().toLocaleTimeString('en-IN', { hour12: false }),
        selectedSymbol,
        activeTimeframe,
        universe,
        stocks: analyzedStocks,
      });
    }
  }, [analyzedStocks, selectedSymbol, activeTimeframe, universe, lastScannedAt, isLiveMode]);

  // ── 2. Load Universe Symbols ───────────────────────────────────────────────
  useEffect(() => {
    let alive = true;
    const fetchUniverse = async () => {
      try {
        const res = await getAnalysisSymbols(universe);
        if (alive && res.symbols?.length) {
          setSymbols(res.symbols);
          // Only fallback to first symbol if NO symbol is selected
          setSelectedSymbol((curr) => {
            if (curr) return curr;
            return res.symbols[0]?.symbol || null;
          });
        }
      } catch (err) {
        console.error('Failed to load analysis symbols:', err);
      }
    };
    fetchUniverse();
    return () => {
      alive = false;
    };
  }, [universe]);

  // ── 3. Initialize Lightweight Chart with ResizeObserver ───────────────────
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      width: chartContainerRef.current.clientWidth || 800,
      height: chartContainerRef.current.clientHeight || 450,
      layout: {
        background: { type: ColorType.Solid, color: '#090d16' },
        textColor: '#94a3b8',
      },
      grid: {
        vertLines: { color: 'rgba(30, 41, 59, 0.45)', style: LineStyle.Dashed },
        horzLines: { color: 'rgba(30, 41, 59, 0.45)', style: LineStyle.Dashed },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: '#38bdf8', width: 1, style: LineStyle.Dashed },
        horzLine: { color: '#38bdf8', width: 1, style: LineStyle.Dashed },
      },
      rightPriceScale: {
        borderColor: 'rgba(51, 65, 85, 0.5)',
        scaleMargins: { top: 0.08, bottom: 0.22 },
        autoScale: true,
      },
      timeScale: {
        borderColor: 'rgba(51, 65, 85, 0.5)',
        timeVisible: false,
        secondsVisible: false,
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#10b981',
      downColor: '#f43f5e',
      borderVisible: false,
      wickUpColor: '#10b981',
      wickDownColor: '#f43f5e',
    });

    // Volume Histogram Series (Sits in bottom 20% above X-axis)
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: {
        type: 'volume',
      },
      priceScaleId: 'volume',
    });

    chart.priceScale('volume').applyOptions({
      scaleMargins: {
        top: 0.80,
        bottom: 0.0,
      },
    });

    // Dynamic 4 Indicator Lines:
    // 1D: 50 EMA, 50 SMA, 200 SMA, 1M AVWAP
    // 1W: 20W EMA, 50W EMA, 50W SMA, 1Y AVWAP
    // 1M: 10M EMA, 20M EMA, 20M SMA, Multi-Yr AVWAP
    const line1Series = chart.addSeries(LineSeries, {
      color: '#06b6d4', // Cyan
      lineWidth: 2,
      priceLineVisible: false,
      title: 'EMA Fast',
    });

    const line2Series = chart.addSeries(LineSeries, {
      color: '#f59e0b', // Amber
      lineWidth: 2,
      lineStyle: LineStyle.Solid,
      priceLineVisible: false,
      title: 'MA Mid',
    });

    const line3Series = chart.addSeries(LineSeries, {
      color: '#e2e8f0', // Slate/White
      lineWidth: 2,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      title: 'MA Trend',
    });

    const line4Series = chart.addSeries(LineSeries, {
      color: '#a855f7', // Violet
      lineWidth: 2,
      priceLineVisible: false,
      title: 'AVWAP',
    });

    chartApiRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    line1SeriesRef.current = line1Series;
    line2SeriesRef.current = line2Series;
    line3SeriesRef.current = line3Series;
    line4SeriesRef.current = line4Series;
    markersPluginRef.current = createSeriesMarkers(candleSeries, []);

    // Crosshair Subscriber
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.seriesData) {
        setHoverData(null);
        return;
      }

      const cData = param.seriesData.get(candleSeries) as any;
      const vData = param.seriesData.get(volumeSeries) as any;
      const l1 = param.seriesData.get(line1Series) as any;
      const l2 = param.seriesData.get(line2Series) as any;
      const l3 = param.seriesData.get(line3Series) as any;
      const l4 = param.seriesData.get(line4Series) as any;

      if (cData) {
        const d = new Date(Number(param.time) * 1000);
        let timeStr = d.toLocaleDateString('en-IN', {
          day: '2-digit',
          month: 'short',
          year: 'numeric',
        });

        // Clarify forming week/month for the latest period bar
        if (latestCandleTimeRef.current && Number(param.time) === latestCandleTimeRef.current) {
          if (activeTimeframeRef.current === '1W') {
            timeStr += ' (WTD • Forming)';
          } else if (activeTimeframeRef.current === '1M') {
            timeStr += ' (MTD • Forming)';
          }
        }

        setHoverData({
          time: timeStr,
          open: cData.open,
          high: cData.high,
          low: cData.low,
          close: cData.close,
          volume: vData?.value,
          val1: l1?.value,
          val2: l2?.value,
          val3: l3?.value,
          val4: l4?.value,
        });
      }
    });

    // ResizeObserver ensures chart dynamically fills exact container bounds
    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0) {
          chart.applyOptions({ width, height });
        }
      }
    });

    if (chartContainerRef.current) {
      resizeObserver.observe(chartContainerRef.current);
    }

    return () => {
      resizeObserver.disconnect();
      markersPluginRef.current = null;
      volumeSeriesRef.current = null;
      chart.remove();
    };
  }, []);

  // Indicator Names based on Active Timeframe
  const indicatorLabels = useMemo(() => {
    if (activeTimeframe === '1D') {
      return {
        l1: '50 EMA',
        l2: '50 SMA',
        l3: '200 SMA',
        l4: '1M AVWAP',
      };
    }
    if (activeTimeframe === '1W') {
      return {
        l1: '20W EMA',
        l2: '50W EMA',
        l3: '50W SMA',
        l4: '1Y AVWAP',
      };
    }
    return {
      l1: '10M EMA',
      l2: '20M EMA',
      l3: '20M SMA',
      l4: 'Multi-Yr AVWAP',
    };
  }, [activeTimeframe]);

  // ── 4. Render Selected Stock onto Chart Canvas ──────────────────────────────
  useEffect(() => {
    if (!selectedSymbol) return;

    let payload = analyzedStocks[selectedSymbol];

    // Check if cached payload is missing, incomplete, or stale (older than today's live session)
    const isPayloadStale = (() => {
      if (!payload || !payload.commentary?.latest_news || !payload.commentary?.institutional_flow_detail) return true;
      const dailyCandles = payload.timeframes?.['1D']?.candles;
      if (!dailyCandles || dailyCandles.length === 0) return true;

      const lastCandle = dailyCandles[dailyCandles.length - 1];
      const lastCandleDate = new Date(lastCandle.time * 1000);

      // Use Intl.DateTimeFormat for reliable IST date computation
      const toISTDateStr = (d: Date) =>
        new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(d);

      const now = new Date();
      const todayStr = toISTDateStr(now);
      const lastDateStr = toISTDateStr(lastCandleDate);

      if (lastDateStr >= todayStr) return false; // Already has today's data

      // Get IST weekday name and map to day number
      const istDayName = new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Kolkata', weekday: 'short' }).format(now);
      const istDayNum = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(istDayName);
      const isWeekday = istDayNum >= 1 && istDayNum <= 5;
      const istHour = parseInt(new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Kolkata', hour: 'numeric', hour12: false }).format(now));
      const istMinute = parseInt(new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Kolkata', minute: 'numeric' }).format(now));
      const marketStarted = istHour > 9 || (istHour === 9 && istMinute >= 15);
      void marketStarted; // available for future use; stale check below covers all weekday scenarios

      // Stale if: weekday AND (market has started OR last candle is > 1 day old)
      // Pre-market fix: even before 09:15, if cache is from a previous day, mark stale
      if (isWeekday && lastDateStr < todayStr) {
        return true;
      }
      return false;
    })();

    if (isPayloadStale && !isScanning) {
      let alive = true;
      getSymbolAnalysis(selectedSymbol, { force_refresh: true })
        .then((data) => {
          if (!alive) return;
          setAnalyzedStocks((prev) => ({ ...prev, [selectedSymbol]: data }));
        })
        .catch((err) => console.error(`Failed to refresh ${selectedSymbol}:`, err));
      if (!payload) return;
    }

    const candleSeries = candleSeriesRef.current;
    const l1Series = line1SeriesRef.current;
    const l2Series = line2SeriesRef.current;
    const l3Series = line3SeriesRef.current;
    const l4Series = line4SeriesRef.current;
    if (!candleSeries || !l1Series || !l2Series || !l3Series || !l4Series) return;

    // Clear previous PriceLines
    activePriceLinesRef.current.forEach((pl) => {
      try {
        candleSeries.removePriceLine(pl);
      } catch (_e) {
        // ignore
      }
    });
    activePriceLinesRef.current = [];

    const tfData = payload.timeframes?.[activeTimeframe];
    if (!tfData || !tfData.candles || tfData.candles.length === 0) {
      return;
    }

    // Map candles to Lightweight Charts format
    const chartCandles = tfData.candles.map((c: any) => ({
      time: c.time as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    candleSeries.setData(chartCandles);
    latestCandleTimeRef.current = chartCandles.length > 0 ? (chartCandles[chartCandles.length - 1].time as number) : null;

    // Bind Dynamic Indicators
    if (activeTimeframe === '1D') {
      // 50 EMA
      if (showLine1 && tfData.ema50?.length) {
        l1Series.setData(tfData.ema50.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l1Series.applyOptions({ visible: true, title: indicatorLabels.l1 });
      } else {
        l1Series.applyOptions({ visible: false });
      }

      // 50 SMA
      if (showLine2 && tfData.sma50?.length) {
        l2Series.setData(tfData.sma50.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l2Series.applyOptions({ visible: true, title: indicatorLabels.l2 });
      } else {
        l2Series.applyOptions({ visible: false });
      }

      // 200 SMA
      if (showLine3 && tfData.sma200?.length) {
        l3Series.setData(tfData.sma200.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l3Series.applyOptions({ visible: true, title: indicatorLabels.l3 });
      } else {
        l3Series.applyOptions({ visible: false });
      }

      // 1M AVWAP
      if (showLine4 && tfData.avwap_month?.length) {
        l4Series.setData(tfData.avwap_month.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l4Series.applyOptions({ visible: true, title: indicatorLabels.l4 });
      } else {
        l4Series.applyOptions({ visible: false });
      }
    } else if (activeTimeframe === '1W') {
      // 20W EMA
      if (showLine1 && tfData.ema20?.length) {
        l1Series.setData(tfData.ema20.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l1Series.applyOptions({ visible: true, title: indicatorLabels.l1 });
      } else {
        l1Series.applyOptions({ visible: false });
      }

      // 50W EMA
      if (showLine2 && tfData.ema50?.length) {
        l2Series.setData(tfData.ema50.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l2Series.applyOptions({ visible: true, title: indicatorLabels.l2 });
      } else {
        l2Series.applyOptions({ visible: false });
      }

      // 50W SMA
      if (showLine3 && tfData.sma50?.length) {
        l3Series.setData(tfData.sma50.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l3Series.applyOptions({ visible: true, title: indicatorLabels.l3 });
      } else {
        l3Series.applyOptions({ visible: false });
      }

      // 1Y AVWAP
      if (showLine4 && tfData.avwap?.length) {
        l4Series.setData(tfData.avwap.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l4Series.applyOptions({ visible: true, title: indicatorLabels.l4 });
      } else {
        l4Series.applyOptions({ visible: false });
      }
    } else {
      // 1M Monthly
      // 10M EMA
      if (showLine1 && tfData.ema10?.length) {
        l1Series.setData(tfData.ema10.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l1Series.applyOptions({ visible: true, title: indicatorLabels.l1 });
      } else {
        l1Series.applyOptions({ visible: false });
      }

      // 20M EMA
      if (showLine2 && tfData.ema20?.length) {
        l2Series.setData(tfData.ema20.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l2Series.applyOptions({ visible: true, title: indicatorLabels.l2 });
      } else {
        l2Series.applyOptions({ visible: false });
      }

      // 20M SMA
      if (showLine3 && tfData.sma20?.length) {
        l3Series.setData(tfData.sma20.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l3Series.applyOptions({ visible: true, title: indicatorLabels.l3 });
      } else {
        l3Series.applyOptions({ visible: false });
      }

      // Multi-Yr AVWAP
      if (showLine4 && tfData.avwap?.length) {
        l4Series.setData(tfData.avwap.map((p: any) => ({ time: p.time as UTCTimestamp, value: p.value })));
        l4Series.applyOptions({ visible: true, title: indicatorLabels.l4 });
      } else {
        l4Series.applyOptions({ visible: false });
      }
    }

    // Set Markers (Arrows & Callout labels on 1D)
    if (markersPluginRef.current) {
      if (activeTimeframe === '1D' && payload.markers?.length) {
        markersPluginRef.current.setMarkers(
          payload.markers.map((m) => ({
            time: m.time as UTCTimestamp,
            position: m.position as any,
            color: m.color,
            shape: m.shape as any,
            text: m.text,
          }))
        );
      } else {
        markersPluginRef.current.setMarkers([]);
      }
    }

    // Set Price Lines (Resistance, Support, Trigger, SL)
    if (showLevels && payload.price_lines?.length) {
      payload.price_lines.forEach((pl) => {
        const line = candleSeries.createPriceLine({
          price: pl.price,
          color: pl.color,
          lineWidth: pl.lineWidth as any,
          lineStyle: pl.lineStyle === 'dashed' ? LineStyle.Dashed : pl.lineStyle === 'dotted' ? LineStyle.Dotted : LineStyle.Solid,
          axisLabelVisible: true,
          title: pl.label,
        });
        activePriceLinesRef.current.push(line);
      });
    }

    // Set Volume Histogram Overlay
    const volumeSeries = volumeSeriesRef.current;
    if (volumeSeries) {
      if (showVolume && tfData.candles?.length) {
        const volumeData = tfData.candles.map((c: any) => ({
          time: c.time as UTCTimestamp,
          value: c.volume || 0,
          color: c.close >= c.open ? 'rgba(16, 185, 129, 0.45)' : 'rgba(244, 63, 94, 0.45)',
        }));
        volumeSeries.setData(volumeData);
        volumeSeries.applyOptions({ visible: true });
      } else {
        volumeSeries.applyOptions({ visible: false });
      }
    }

    // Always restore autoScale when switching symbols or timeframes to eliminate manual dragging
    chartApiRef.current?.priceScale('right').applyOptions({ autoScale: true });
    chartApiRef.current?.timeScale().fitContent();
  }, [selectedSymbol, analyzedStocks, activeTimeframe, showVolume, showLine1, showLine2, showLine3, showLine4, showLevels, indicatorLabels, isScanning]);

  // Double-click canvas to auto-fit both Price and Time scales
  useEffect(() => {
    const el = chartContainerRef.current;
    if (!el) return;
    const onDblClick = () => {
      chartApiRef.current?.priceScale('right').applyOptions({ autoScale: true });
      chartApiRef.current?.timeScale().fitContent();
    };
    el.addEventListener('dblclick', onDblClick);
    return () => el.removeEventListener('dblclick', onDblClick);
  }, []);

  // Explicit Auto-Fit / Reset View Handler
  const handleResetView = useCallback(() => {
    if (!chartApiRef.current) return;
    chartApiRef.current.priceScale('right').applyOptions({ autoScale: true });
    chartApiRef.current.timeScale().fitContent();
  }, []);

  // ── 5. Instant Fast Batch Scanner Loop ─────────────────────────────────────
  const startFullScan = useCallback(async () => {

    let targetSymbols = symbols;
    if (!targetSymbols.length) {
      try {
        const res = await getAnalysisSymbols(universe);
        targetSymbols = res.symbols || [];
        setSymbols(targetSymbols);
      } catch (err) {
        console.error('Failed to load symbols for scan:', err);
      }
    }
    if (!targetSymbols.length) return;

    isScanningRef.current = true;
    setIsScanning(true);

    // 1. Immediately refresh the currently selected symbol first so the active screen updates instantly!
    if (selectedSymbol) {
      try {
        const activeRes = await getSymbolAnalysis(selectedSymbol, { force_refresh: true });
        if (activeRes) {
          setAnalyzedStocks((prev) => ({ ...prev, [selectedSymbol]: activeRes }));
        }
      } catch (err) {
        console.debug('Failed to refresh selected symbol on scan start:', err);
      }
    }

    const BATCH_SIZE = 5;
    const BATCH_DELAY_MS = 100;
    const total = targetSymbols.length;

    for (let i = 0; i < total; i += BATCH_SIZE) {
      if (!isScanningRef.current) break;

      const batch = targetSymbols.slice(i, i + BATCH_SIZE);
      setCurrentScanIdx(Math.min(i + batch.length - 1, total - 1));

      try {
        const results = await Promise.allSettled(
          batch.map((s) => getSymbolAnalysis(s.symbol, { force_refresh: true }))
        );

        const newEntries: Record<string, SymbolAnalysisPayload> = {};
        results.forEach((res, idx) => {
          if (res.status === 'fulfilled' && res.value) {
            newEntries[batch[idx].symbol] = res.value;
          }
        });

        if (Object.keys(newEntries).length > 0) {
          setAnalyzedStocks((prev) => ({ ...prev, ...newEntries }));
          if (i === 0 && !selectedSymbol && batch[0]) {
            setSelectedSymbol(batch[0].symbol);
          }
        }
      } catch (batchErr) {
        console.error('Batch scan error:', batchErr);
      }

      // Throttle: prevent backend overload — small delay between batches
      if (i + BATCH_SIZE < total) {
        await new Promise((resolve) => setTimeout(resolve, BATCH_DELAY_MS));
      }
    }

    // Save to cache once scan completes (not on every batch to avoid IndexedDB write thrashing)
    setAnalyzedStocks((prev) => {
      saveAnalysisCache({
        timestamp: new Date().toLocaleTimeString('en-IN', { hour12: false }),
        selectedSymbol: selectedSymbol || null,
        activeTimeframe,
        universe,
        stocks: prev,
      }).catch((err) => console.debug('Cache save error:', err));
      return prev;
    });

    setIsScanning(false);
    isScanningRef.current = false;
    setLastScannedAt(new Date().toLocaleTimeString('en-IN', { hour12: false }));
  }, [symbols, universe, selectedSymbol, activeTimeframe]);

  const stopScan = useCallback(() => {
    isScanningRef.current = false;
    setIsScanning(false);
  }, []);

  // ── 5B. Real-Time Live Surveillance Ticker Loop ─────────────────────────────
  // Constants for institutional acceptance thresholds
  const TRIGGER_SUSTAIN_TICKS = 40;        // ~2 minutes at 3s intervals
  const SUSTAINED_BREAKOUT_TICKS = 100;    // ~5 minutes
  const RETEST_TOLERANCE_PCT = 0.15;       // Allow 0.15% micro-pullback without revoking
  const MAX_SLIP_COUNT = 2;                // Max micro-slips allowed during sustain window
  const RSI_OVERBOUGHT = 73;               // Block trigger bonus above this
  const RSI_CAUTION = 65;                  // Reduced bonus in this zone (65-73)

  const executeLiveTick = useCallback(async () => {
    if (isTickingRef.current) return;
    const currentStocks = analyzedStocksRef.current;
    const symList = Object.keys(currentStocks);
    if (!symList.length) return;

    isTickingRef.current = true;
    try {
      const triggerSpecs = symList.map((symbol) => {
        const commentary: any = currentStocks[symbol]?.commentary || {};
        const bias = String(commentary.bias || 'BULLISH').toUpperCase();
        return {
          symbol,
          trigger_price: Number(commentary.trigger_price || currentStocks[symbol]?.current_price || 0),
          stop_loss: Number(commentary.stop_loss || 0),
          target_1: Number(commentary.target_price_1 || 0),
          target_2: Number(commentary.target_price_2 || 0),
          direction: bias.includes('BEAR') || bias.includes('BREAKDOWN') ? 'BEAR' as const : 'BULL' as const,
        };
      }).filter((spec) => spec.trigger_price > 0);
      const res = await getLiveTicks(symList, triggerSpecs, liveEventSequenceRef.current);
      liveEventSequenceRef.current = Math.max(liveEventSequenceRef.current, res.last_sequence_id || 0);
      if (res && res.ticks) {
        // Success: reset error counter and clear connection-lost state
        if (liveErrorCountRef.current > 0) {
          liveErrorCountRef.current = 0;
          setLiveConnectionLost(false);
        }
        setLiveTickTimestamp(res.timestamp);

        const flashes: Record<string, 'up' | 'down'> = {};
        const updatedEntries: Record<string, SymbolAnalysisPayload> = {};
        let hasUpdates = false;

        for (const [sym, tick] of Object.entries(res.ticks)) {
          const existing = currentStocks[sym];
          if (!existing) continue;

          const oldLtp = existing.current_price ?? (existing as any).price ?? 0;
          const newLtp = tick.ltp;
          if (newLtp !== oldLtp && oldLtp > 0) {
            flashes[sym] = newLtp > oldLtp ? 'up' : 'down';
          }

          // Live Mathematical Evaluations
          const c = existing.commentary;
          const trig = c?.trigger_price || newLtp;
          const sl = c?.stop_loss || (trig * 0.985);
          const t1 = c?.target_price_1 || (trig * 1.03);
          const t2 = c?.target_price_2 || (trig * 1.06);
          const bias = (c?.bias || 'BULLISH').toUpperCase();
          const isBear = bias.includes('BEAR') || bias.includes('BREAKDOWN');

          // RSI and RVOL from backend
          const rsi5m = tick.rsi_5m ?? null;
          const rvol = tick.rvol ?? null;

          let rawTriggerStatus = 'WATCHING';
          let livePnlPct = 0;
          const distToTrigger = Number((((newLtp - trig) / trig) * 100).toFixed(2));
          const distToT1 = Number((((t1 - newLtp) / newLtp) * 100).toFixed(2));
          const distToSL = Number((((newLtp - sl) / sl) * 100).toFixed(2));

          // Determine raw price-based trigger status
          if (isBear) {
            livePnlPct = Number((((trig - newLtp) / trig) * 100).toFixed(2));
            if (newLtp >= sl) {
              rawTriggerStatus = 'SL_HIT';
            } else if (t2 && newLtp <= t2) {
              rawTriggerStatus = 'TP2_HIT';
            } else if (t1 && newLtp <= t1) {
              rawTriggerStatus = 'TP1_HIT';
            } else if (newLtp <= trig) {
              rawTriggerStatus = 'TRIGGERED';
            } else {
              rawTriggerStatus = 'WATCHING';
            }
          } else {
            livePnlPct = Number((((newLtp - trig) / trig) * 100).toFixed(2));
            if (newLtp <= sl) {
              rawTriggerStatus = 'SL_HIT';
            } else if (t2 && newLtp >= t2) {
              rawTriggerStatus = 'TP2_HIT';
            } else if (t1 && newLtp >= t1) {
              rawTriggerStatus = 'TP1_HIT';
            } else if (newLtp >= trig) {
              rawTriggerStatus = 'TRIGGERED';
            } else {
              rawTriggerStatus = 'WATCHING';
            }
          }

          // ── Temporal Persistence Engine (server-owned; survives refresh/restart) ─
          // Track how long price sustains above trigger with retest tolerance
          let sustainData = triggerSustainsRef.current[sym];
          let liveStatus = rawTriggerStatus;
          let sustainTickCount = 0;
          const serverTriggerState = res.trigger_states?.[sym];

          const isAtOrPastTrigger = rawTriggerStatus === 'TRIGGERED' || rawTriggerStatus === 'TP1_HIT' || rawTriggerStatus === 'TP2_HIT';

          if (isAtOrPastTrigger) {
            // Price is at or past trigger — check sustain with retest tolerance
            const retestSlipped = isBear
              ? (distToTrigger > RETEST_TOLERANCE_PCT)   // Bear: price slightly above trigger = micro-retest
              : (distToTrigger < -RETEST_TOLERANCE_PCT); // Bull: price slightly below trigger = micro-retest

            if (!sustainData) {
              // First time crossing trigger
              sustainData = { firstSeen: Date.now(), tickCount: 1, slipCount: 0 };
              triggerSustainsRef.current[sym] = sustainData;
            } else {
              sustainData.tickCount += 1;
              if (retestSlipped) {
                sustainData.slipCount += 1;
              }

              if (serverTriggerState) {
                liveStatus = serverTriggerState.status;
                sustainTickCount = Math.round((serverTriggerState.sustain_seconds || 0) / 3);
              }
            }
            sustainTickCount = sustainData.tickCount;

            // Determine refined status based on temporal persistence
            if (sustainData.slipCount > MAX_SLIP_COUNT) {
              // Too many micro-slips — reject, treat as wick/fakeout
              liveStatus = 'WATCHING';
              delete triggerSustainsRef.current[sym];
            } else if (sustainData.tickCount >= SUSTAINED_BREAKOUT_TICKS) {
              liveStatus = 'SUSTAINED_BREAKOUT';
            } else if (sustainData.tickCount >= TRIGGER_SUSTAIN_TICKS) {
              liveStatus = 'CONFIRMED_TRIGGER';
            } else {
              liveStatus = 'TRIGGER_PENDING';
            }

            // Override for target hits — they bypass sustain requirement
            if (rawTriggerStatus === 'TP1_HIT') liveStatus = 'TP1_HIT';
            if (rawTriggerStatus === 'TP2_HIT') liveStatus = 'TP2_HIT';
          } else if (rawTriggerStatus === 'WATCHING') {
            // Price slipped back below trigger
            if (sustainData) {
              // Was previously tracking — check if it's a temporary micro-pullback
              const pullbackPct = Math.abs(distToTrigger);
              if (pullbackPct <= RETEST_TOLERANCE_PCT * 2) {
                // Still within retest tolerance zone — increment slip counter but don't reset
                sustainData.slipCount += 1;
                sustainData.tickCount += 1; // Continue counting
                if (sustainData.slipCount > MAX_SLIP_COUNT) {
                  // Failed retest — full reset
                  liveStatus = 'WATCHING';
                  delete triggerSustainsRef.current[sym];
                } else {
                  liveStatus = 'TRIGGER_PENDING';
                  sustainTickCount = sustainData.tickCount;
                }
              } else {
                // Price fell away from trigger — hard reset
                delete triggerSustainsRef.current[sym];
                liveStatus = 'WATCHING';
              }
            }
          } else if (rawTriggerStatus === 'SL_HIT') {
            // Stop loss hit — clear sustain tracking
            delete triggerSustainsRef.current[sym];
            liveStatus = 'SL_HIT';
          }

          // ── Institutional Acceptance Scoring Engine ─────────────────────
          let liveScore = c?.potential_score || 0;

          if (liveStatus === 'CONFIRMED_TRIGGER' || liveStatus === 'SUSTAINED_BREAKOUT') {
            // ── Confirmed Trigger: RSI-gated bonus (directional) ──
            if (!isBear) {
              // Longs: Clean momentum runway below 65, caution 65-73, overbought >= 73
              if (rsi5m !== null && rsi5m >= RSI_OVERBOUGHT) {
                // RSI ≥ 73: NO bonus — overbought exhaustion
              } else if (rsi5m !== null && rsi5m >= RSI_CAUTION) {
                // RSI 65-73: Reduced +3 bonus
                liveScore = Math.min(100, liveScore + 3);
              } else if (rsi5m !== null) {
                // RSI < 65: Full +5 bonus (healthy momentum runway)
                liveScore = Math.min(100, liveScore + 5);
              }
            } else {
              // Shorts: Clean downside runway above 35, caution 27-35, oversold exhaustion <= 27
              if (rsi5m !== null && rsi5m <= 27) {
                // RSI ≤ 27: NO bonus — oversold exhaustion, high snapback risk
              } else if (rsi5m !== null && rsi5m <= 35) {
                // RSI 27-35: Reduced +3 bonus
                liveScore = Math.min(100, liveScore + 3);
              } else if (rsi5m !== null) {
                // RSI > 35: Full +5 bonus (healthy downside runway)
                liveScore = Math.min(100, liveScore + 5);
              }
            }

            // ── RVOL Expansion Bonus (only for confirmed triggers) ──
            if (rvol !== null) {
              if (rvol >= 2.0) {
                liveScore = Math.min(100, liveScore + 3); // High volume breakout/breakdown
              } else if (rvol >= 1.5) {
                liveScore = Math.min(100, liveScore + 2); // Above-average volume
              } else if (rvol < 1.0) {
                liveScore = Math.max(10, liveScore - 2);  // Low volume — suspect move
              }
            }

            // Sustained breakout extra bonus
            if (liveStatus === 'SUSTAINED_BREAKOUT') {
              liveScore = Math.min(100, liveScore + 3);
            }

            // ── Auto-Promotion Protocol ──
            // If setup achieves confirmed trigger with Score >= 85, verified clean RSI (Long <73, Short >27), and RVOL >= 1.0:
            // Auto-promote directly to appropriate desk & dispatch Telegram HITL alert!
            const isRsiClean = isBear
              ? (rsi5m !== null && rsi5m > 27)
              : (rsi5m !== null && rsi5m < RSI_OVERBOUGHT);

            if (
              liveScore >= 85 &&
              isRsiClean &&
              (rvol !== null && rvol >= 1.0) &&
              !autoPromotedSymbolsRef.current.has(sym)
            ) {
              autoPromotedSymbolsRef.current.add(sym);
              const promoPayload = {
                symbol: sym,
                score: liveScore,
                bias: c?.bias || (isBear ? 'BEARISH_BREAKDOWN' : 'BULLISH_BREAKOUT'),
                live_status: liveStatus,
                trigger_price: trig,
                current_price: newLtp,
                rsi_5m: rsi5m,
                rvol: rvol,
                quote_source: tick.source,
                quote_age_ms: tick.quote_age_ms,
                rsi_is_real: tick.rsi_is_real === true,
                trigger_correlation_id: serverTriggerState?.correlation_id || null,
                thesis: c?.actionable_thesis || '',
                stop_loss: sl,
              };

              if (isBear) {
                promoteWatchdogShortsCandidate(promoPayload).catch((err) => console.debug('Short auto-promotion dispatch error:', err));
              } else {
                promoteWatchdogCandidate(promoPayload).catch((err) => console.debug('Long auto-promotion dispatch error:', err));
              }
            }
          } else if (liveStatus === 'TP1_HIT') {
            // Target 1 hit — always reward regardless of RSI
            liveScore = Math.min(100, liveScore + 5);
          } else if (liveStatus === 'TP2_HIT') {
            liveScore = Math.min(100, liveScore + 8);
          } else if (liveStatus === 'TRIGGER_PENDING') {
            // Pending temporal confirmation: ZERO bonus awarded!
            // Server-owned lifecycle state proves institutional acceptance by elapsed time.
            // This prevents momentary spikes/wicks from jumping rank.
          } else if (liveStatus === 'WATCHING') {
            // Not triggered — proximity-based modifiers
            if (!isBear) {
              if (distToTrigger >= -0.4 && distToTrigger < 0) {
                liveScore = Math.min(100, liveScore + 3); // Approaching trigger
              } else if (distToTrigger < -0.4 && distToTrigger >= -1.0) {
                liveScore = Math.max(10, liveScore - 2);  // Pullback / rejection
              }
            } else {
              if (distToTrigger > 0 && distToTrigger <= 0.4) {
                liveScore = Math.min(100, liveScore + 3);
              } else if (distToTrigger > 0.4 && distToTrigger <= 1.0) {
                liveScore = Math.max(10, liveScore - 2);
              }
            }
          } else if (liveStatus === 'SL_HIT') {
            liveScore = Math.max(10, liveScore - 5);
          }

          // Update candle on active chart if this is selectedSymbol and 1D timeframe
          if (sym === selectedSymbolRef.current && candleSeriesRef.current && activeTimeframeRef.current === '1D') {
            const dailyCandles = existing.timeframes?.['1D']?.candles;
            if (dailyCandles && dailyCandles.length > 0) {
              const lastBar = dailyCandles[dailyCandles.length - 1];
              candleSeriesRef.current.update({
                time: lastBar.time,
                open: lastBar.open,
                high: Math.max(lastBar.high, tick.high || newLtp),
                low: Math.min(lastBar.low, tick.low || newLtp),
                close: newLtp,
              });
            }
          }

          // Clone and enrich payload
          updatedEntries[sym] = {
            ...existing,
            current_price: newLtp,
            change_pct: tick.change_pct,
            commentary: {
              ...existing.commentary,
              live_status: liveStatus,
              live_pnl_pct: livePnlPct,
              distance_to_trigger: distToTrigger,
              distance_to_t1: distToT1,
              distance_to_sl: distToSL,
              live_potential_score: liveScore,
              live_rsi_5m: rsi5m,
              live_rvol: rvol,
              live_sustain_ticks: sustainTickCount,
              live_sustain_seconds: serverTriggerState?.sustain_seconds || 0,
            } as any,
          };
          hasUpdates = true;
        }

        if (hasUpdates) {
          setAnalyzedStocks((prev) => ({ ...prev, ...updatedEntries }));
          if (Object.keys(flashes).length > 0) {
            setLivePriceFlashes(flashes);
            setTimeout(() => setLivePriceFlashes({}), 800);
          }
        }
      }
    } catch (err) {
      console.debug('Live tick poll error:', err);
      // Circuit breaker: pause after 8 consecutive failures
      liveErrorCountRef.current += 1;
      if (liveErrorCountRef.current >= 8) {
        setLiveConnectionLost(true);
        if (liveIntervalRef.current) {
          clearInterval(liveIntervalRef.current);
          liveIntervalRef.current = null;
        }
        // Auto-retry after 10 seconds
        setTimeout(() => {
          if (isLiveModeRef.current) {
            liveErrorCountRef.current = 0;
            setLiveConnectionLost(false);
            liveIntervalRef.current = setInterval(() => {
              executeLiveTick();
            }, 3000);
            executeLiveTick();
          }
        }, 10000);
      }
    } finally {
      isTickingRef.current = false;
    }
  }, []);

  // ── 5C. Toggle Live Analysis with Mandatory Fresh Warmup ───────────────────
  const handleToggleLiveAnalysis = useCallback(async () => {
    // Reset circuit breaker state on fresh toggle
    liveErrorCountRef.current = 0;
    setLiveConnectionLost(false);
    if (isLiveModeRef.current) {
      // Toggle OFF
      isLiveModeRef.current = false;
      setIsLiveMode(false);
      setIsLiveWarmingUp(false);
      triggerSustainsRef.current = {};
      autoPromotedSymbolsRef.current.clear();
      if (liveIntervalRef.current) {
        clearInterval(liveIntervalRef.current);
        liveIntervalRef.current = null;
      }
      // Persist final live state to cache on toggle-off
      const finalStocks = analyzedStocksRef.current;
      if (Object.keys(finalStocks).length > 0) {
        saveAnalysisCache({
          timestamp: new Date().toLocaleTimeString('en-IN', { hour12: false }),
          selectedSymbol: selectedSymbolRef.current,
          activeTimeframe,
          universe,
          stocks: finalStocks,
        }).catch((err) => console.debug('Cache save on live-off error:', err));
      }
      return;
    }

    // Toggle ON: Fresh Foundation Warmup (Zero Stale Cache)
    isLiveModeRef.current = true;
    setIsLiveMode(true);
    setIsLiveWarmingUp(true);

    // In live mode, only stocks with potential_score >= 85 (Tier A+ elite setups) are included
    analyzedStocksRef.current = {};
    triggerSustainsRef.current = {};
    autoPromotedSymbolsRef.current.clear();
    setAnalyzedStocks({});

    let targetSymbols = symbols;
    if (!targetSymbols.length) {
      try {
        const res = await getAnalysisSymbols(universe);
        targetSymbols = res.symbols || [];
        setSymbols(targetSymbols);
      } catch (err) {
        console.error('Failed to load symbols for live warmup:', err);
      }
    }

    // 1. Immediately refresh selected stock with force_refresh: true (only if score >= 85)
    const currentSelectedSym = selectedSymbolRef.current;
    if (currentSelectedSym) {
      try {
        const activeRes = await getSymbolAnalysis(currentSelectedSym, { force_refresh: true });
        if (activeRes) {
          const score = activeRes.commentary?.potential_score ?? 0;
          if (score >= 85) {
            analyzedStocksRef.current = { [currentSelectedSym]: activeRes };
            setAnalyzedStocks({ [currentSelectedSym]: activeRes });
          }
        }
      } catch (err) {
        console.debug('Live warmup active stock refresh error:', err);
      }
    }

    // 2. Fresh batch scan across universe — ONLY include stocks with score >= 85
    const BATCH_SIZE = 5;
    const BATCH_DELAY_MS = 100;
    const total = targetSymbols.length;

    for (let i = 0; i < total; i += BATCH_SIZE) {
      if (!isLiveModeRef.current) break;

      const batch = targetSymbols.slice(i, i + BATCH_SIZE);
      setCurrentScanIdx(Math.min(i + batch.length - 1, total - 1));

      try {
        const results = await Promise.allSettled(
          batch.map((s) => getSymbolAnalysis(s.symbol, { force_refresh: true }))
        );

        const newEntries: Record<string, SymbolAnalysisPayload> = {};
        results.forEach((res, idx) => {
          if (res.status === 'fulfilled' && res.value) {
            const score = res.value.commentary?.potential_score ?? 0;
            // FILTER: In live mode, do not include stocks whose score is below 85 during warmup
            if (score >= 85) {
              newEntries[batch[idx].symbol] = res.value;
            }
          }
        });

        if (Object.keys(newEntries).length > 0) {
          analyzedStocksRef.current = { ...analyzedStocksRef.current, ...newEntries };
          setAnalyzedStocks((prev) => ({ ...prev, ...newEntries }));
          const currentSelected = selectedSymbolRef.current;
          if (!currentSelected || !analyzedStocksRef.current[currentSelected]) {
            const firstQualified = Object.keys(newEntries)[0];
            if (firstQualified) {
              setSelectedSymbol(firstQualified);
            }
          }
        }
      } catch (batchErr) {
        console.error('Live warmup batch error:', batchErr);
      }

      // Throttle: prevent backend overload during warmup
      if (i + BATCH_SIZE < total) {
        await new Promise((resolve) => setTimeout(resolve, BATCH_DELAY_MS));
      }
    }

    // Save qualified stocks to cache once warmup completes (not on every batch)
    const finalStocks = analyzedStocksRef.current;
    if (Object.keys(finalStocks).length > 0) {
      saveAnalysisCache({
        timestamp: new Date().toLocaleTimeString('en-IN', { hour12: false }),
        selectedSymbol: selectedSymbolRef.current || null,
        activeTimeframe,
        universe,
        stocks: finalStocks,
      }).catch((err) => console.debug('Live warmup cache save error:', err));
    }

    setIsLiveWarmingUp(false);
    setLastScannedAt(new Date().toLocaleTimeString('en-IN', { hour12: false }));
    const qualifiedKeys = Object.keys(analyzedStocksRef.current);
    if (qualifiedKeys.length > 0) {
      if (!selectedSymbolRef.current || !analyzedStocksRef.current[selectedSymbolRef.current]) {
        setSelectedSymbol(qualifiedKeys[0]);
      }
    }
  }, [symbols, universe, selectedSymbol, activeTimeframe]);

  // Surveillance heartbeat interval — only starts AFTER warmup completes
  useEffect(() => {
    if (isLiveMode && !isLiveWarmingUp) {
      executeLiveTick();
      liveIntervalRef.current = setInterval(() => {
        executeLiveTick();
      }, 3000);
    } else {
      if (liveIntervalRef.current) {
        clearInterval(liveIntervalRef.current);
        liveIntervalRef.current = null;
      }
    }
    return () => {
      if (liveIntervalRef.current) {
        clearInterval(liveIntervalRef.current);
        liveIntervalRef.current = null;
      }
    };
  }, [isLiveMode, isLiveWarmingUp, executeLiveTick]);

  // ── 6. Filter & Rank Completed Stocks ──────────────────────────────────────
  const rankedStocks = useMemo(() => {
    const list = Object.values(analyzedStocks);
    // Multi-factor ranking: Score -> Actionable Bias Priority -> Squeeze Compression
    list.sort((a, b) => {
      const scoreA = (a.commentary as any)?.live_potential_score ?? a.commentary?.potential_score ?? 0;
      const scoreB = (b.commentary as any)?.live_potential_score ?? b.commentary?.potential_score ?? 0;
      if (scoreB !== scoreA) return scoreB - scoreA;

      // Tie-break 1: Actionable bias before neutral
      const biasPriority: Record<string, number> = {
        'BULLISH_BREAKOUT': 6,
        'SQUEEZE_COIL': 5,
        '50EMA_BOUNCE': 4,
        'MONTHLY_VWAP_TEST': 3,
        'BEARISH_BREAKDOWN': 2,
        'CONFLICTED_SIGNAL': 1,
        'DISTRIBUTION_TRAP': 1,
        'NEUTRAL_CHOP': 0,
      };
      const biasA = biasPriority[a.commentary?.bias || ''] ?? 0;
      const biasB = biasPriority[b.commentary?.bias || ''] ?? 0;
      if (biasB !== biasA) return biasB - biasA;

      // Tie-break 2: Squeeze compression (lower squeeze percentile means tighter compression)
      const sqA = a.metrics?.squeeze_percentile ?? 999;
      const sqB = b.metrics?.squeeze_percentile ?? 999;
      if (sqA !== sqB) return sqA - sqB;

      // Tie-break 3: Deterministic alphabetical order (never jump randomly)
      return a.symbol.localeCompare(b.symbol);
    });

    return list.filter((item) => {
      if (searchQuery.trim()) {
        const rawQ = searchQuery.toUpperCase().trim();
        let q = rawQ === 'ZOMATO' ? 'ETERNAL' : rawQ;
        if (q === 'PEL') q = 'PIRAMALFIN';
        const matchSym = item.symbol.toUpperCase().includes(q) ||
          (item.symbol === 'ETERNAL' && rawQ.includes('ZOMA')) ||
          (item.symbol === 'PIRAMALFIN' && rawQ === 'PEL');
        const matchBias = item.commentary.bias.toUpperCase().includes(rawQ);
        if (!matchSym && !matchBias) return false;
      }
      if (activeFilter === 'breakout') {
        return item.commentary.bias === 'BULLISH_BREAKOUT';
      }
      if (activeFilter === 'bounce') {
        return item.commentary.bias === '50EMA_BOUNCE' || item.commentary.bias === 'MONTHLY_VWAP_TEST';
      }
      if (activeFilter === 'squeeze') {
        return item.commentary.bias === 'SQUEEZE_COIL';
      }
      if (activeFilter === 'breakdown') {
        return item.commentary.bias === 'BEARISH_BREAKDOWN';
      }
      return true;
    });
  }, [analyzedStocks, searchQuery, activeFilter]);

  // Matching universe stocks not yet in analyzedStocks
  const unanalyzedMatches = useMemo(() => {
    if (!searchQuery.trim()) return [];
    const rawQ = searchQuery.toUpperCase().trim();
    let q = rawQ === 'ZOMATO' ? 'ETERNAL' : rawQ;
    if (q === 'PEL') q = 'PIRAMALFIN';
    return symbols
      .filter((s) => (s.symbol.toUpperCase().includes(q) || (s.symbol === 'ETERNAL' && rawQ.includes('ZOMA')) || (s.symbol === 'PIRAMALFIN' && rawQ === 'PEL')) && !analyzedStocks[s.symbol])
      .slice(0, 15);
  }, [symbols, searchQuery, analyzedStocks]);

  const activeAnalysis = selectedSymbol ? analyzedStocks[selectedSymbol] : null;

  // Active filter label for tooltips and copy headers
  const activeFilterLabel = useMemo(() => {
    switch (activeFilter) {
      case 'breakout': return '⚡ Breakout';
      case 'bounce': return '🎯 Bounce';
      case 'squeeze': return '⏳ Squeeze';
      case 'breakdown': return '🛑 Breakdown';
      default: return 'All Stocks';
    }
  }, [activeFilter]);



  // Copy filtered stocks technical commentary to clipboard
  const handleCopyFilteredAnalysis = useCallback(async () => {
    if (rankedStocks.length === 0) return;

    const filterHeading = activeFilter === 'all'
      ? 'ALL STOCKS'
      : activeFilter === 'breakout'
      ? 'BREAKOUT SETUPS'
      : activeFilter === 'bounce'
      ? 'BOUNCE SETUPS (50 EMA / MONTHLY VWAP)'
      : activeFilter === 'squeeze'
      ? 'VOLATILITY SQUEEZE / COIL SETUPS'
      : 'BREAKDOWN SETUPS';

    const header = [
      '══════════════════════════════════════════════════════════════════════════════',
      'VIA MULTI-TIMEFRAME CHART ANALYSIS REPORT',
      `Filter Applied: ${filterHeading}${searchQuery.trim() ? ` (Search: "${searchQuery.trim()}")` : ''}`,
      `Universe: ${universe.toUpperCase()} | Matching Stocks: ${rankedStocks.length}`,
      `Exported: ${new Date().toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })} IST`,
      '══════════════════════════════════════════════════════════════════════════════',
    ].join('\n');

    const stockEntries = rankedStocks.map((item, idx) => {
      const c = item.commentary;
      const priceVal = Number(item.current_price ?? (item as any).price ?? item.timeframes?.['1D']?.candles?.slice(-1)[0]?.close ?? c?.trigger_price ?? 0);
      const priceStr = `₹${priceVal.toFixed(2)}`;
      const changeVal = Number(item.change_pct ?? 0);
      const changeStr = `${changeVal >= 0 ? '+' : ''}${changeVal.toFixed(2)}%`;
      const scoreStr = `${c?.potential_score ?? 0}/100 [Tier ${c?.quality_tier ?? '-'}]`;
      const biasStr = c?.bias ? c.bias.replace(/_/g, ' ') : 'NEUTRAL';

      const levels = [
        c?.trigger_price ? `Trigger: ₹${c.trigger_price.toFixed(2)}` : null,
        c?.stop_loss ? `SL: ₹${c.stop_loss.toFixed(2)}` : null,
        c?.target_price_1 ? `T1: ₹${c.target_price_1.toFixed(2)}` : null,
        c?.target_price_2 ? `T2: ₹${c.target_price_2.toFixed(2)}` : null,
      ].filter(Boolean).join(' | ');

      const liveRsi = (c as any)?.live_rsi_5m ?? (c as any)?.rsi_5m;
      const liveRvol = (c as any)?.live_rvol ?? (c as any)?.rvol;
      const liveStatus = (c as any)?.live_status;
      const liveMetrics = [
        liveRsi != null ? `5m RSI: ${liveRsi}` : null,
        liveRvol != null ? `RVOL: ${liveRvol}x` : null,
        liveStatus ? `Status: ${String(liveStatus).replace(/_/g, ' ')}` : null,
      ].filter(Boolean).join(' | ');

      return [
        '──────────────────────────────────────────────────────────────────────────────',
        `#${idx + 1}. ${item.symbol}  |  Price: ${priceStr} (${changeStr})`,
        `Score: ${scoreStr}  |  Bias: ${biasStr}`,
        levels ? `Key Levels: ${levels}` : null,
        liveMetrics ? `Institutional Acceptance: ${liveMetrics}` : null,
        `Actionable Thesis: ${c?.actionable_thesis || 'N/A'}`,
        '',
        '[1] BASELINE STRUCTURE:',
        `${c?.structure_base || 'N/A'}`,
        '',
        '[2] MULTI-TIMEFRAME TREND:',
        `${c?.multi_tf_trend || 'N/A'}`,
        '',
        '[3] INDICATOR INTERACTION:',
        `${c?.indicator_interaction || 'N/A'}`,
        '',
        '[4] VOLATILITY STATE:',
        `${c?.volatility_state || 'N/A'}`,
        '',
        '[5] FINBERT SENTIMENT & NARRATIVE INTELLIGENCE (24H):',
        c?.sentiment_detail
          ? [
              `Score: ${c.sentiment_detail.sentiment_score > 0 ? `+${c.sentiment_detail.sentiment_score}` : c.sentiment_detail.sentiment_score}/100 [${c.sentiment_detail.sentiment_label.replace(/_/g, ' ')}]`,
              `Distribution: Bull: ${Math.round(c.sentiment_detail.probabilities.positive * 100)}% | Neu: ${Math.round(c.sentiment_detail.probabilities.neutral * 100)}% | Bear: ${Math.round(c.sentiment_detail.probabilities.negative * 100)}%`,
              c.sentiment_detail.narrative_alignment ? `Narrative Alignment: ${c.sentiment_detail.narrative_alignment.badge} — ${c.sentiment_detail.narrative_alignment.detail}` : null,
              `Catalysts (${c.sentiment_detail.article_count}): ${c.sentiment_detail.headlines.map(h => `${h.is_regulatory ? '[BSE Filing] ' : ''}[${h.source}] ${h.title} (${h.sentiment})`).join(' | ') || 'No major headlines'}`,
            ].filter(Boolean).join('\n')
          : `${c?.latest_news || 'No news in past 24h'}`,
        '',
        '[6] INSTITUTIONAL FLOW ENGINE (IFP):',
        c?.institutional_flow_detail
          ? [
              `Score: ${c.institutional_flow_detail.score > 0 ? `+${c.institutional_flow_detail.score}` : c.institutional_flow_detail.score}/100 [${c.institutional_flow_detail.classification.replace(/_/g, ' ')}]`,
              `Confidence: ${c.institutional_flow_detail.confidence}% | Coverage: ${c.institutional_flow_detail.coverage}%`,
              `Trend: 5D: ${c.institutional_flow_detail.trend.trend_5d > 0 ? `+${c.institutional_flow_detail.trend.trend_5d}` : c.institutional_flow_detail.trend.trend_5d} | 20D: ${c.institutional_flow_detail.trend.trend_20d > 0 ? `+${c.institutional_flow_detail.trend.trend_20d}` : c.institutional_flow_detail.trend.trend_20d} | 10D Persistence: ${Math.round(c.institutional_flow_detail.trend.persistence_10d * 100)}%`,
              `Components: Vol: ${c.institutional_flow_detail.components.volume ?? 'N/A'}, CVD: ${c.institutional_flow_detail.components.cvd ?? 'N/A'}, Large: ${c.institutional_flow_detail.components.large_trades ?? 'N/A'}, Depth: ${c.institutional_flow_detail.components.depth ?? 'N/A'}, AVWAP: ${c.institutional_flow_detail.components.price_acceptance ?? 'N/A'}, F&O: ${c.institutional_flow_detail.components.fno_oi ?? 'N/A (Non-F&O)'}`,
              c.institutional_flow_detail.stealth_phase ? `Regime: ${c.institutional_flow_detail.stealth_phase}` : null,
              c.institutional_flow_detail.conflicts?.length ? `Conflicts: ${c.institutional_flow_detail.conflicts.join('; ')}` : null,
              `Narrative: ${c.institutional_flow_detail.summary}`,
            ].filter(Boolean).join('\n')
          : (c?.institutional_flow || 'Neutral / Balanced Flow'),
      ].filter((line) => line !== null).join('\n');
    }).join('\n\n');

    const fullExport = `${header}\n\n${stockEntries}\n\n══════════════════════════════════════════════════════════════════════════════\n`;

    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(fullExport);
      } else {
        const textarea = document.createElement('textarea');
        textarea.value = fullExport;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch (err) {
      console.error('Failed to copy to clipboard:', err);
    }
  }, [rankedStocks, activeFilter, searchQuery, universe]);



  // Add current active stock directly to Live Tracking
  const handleTrackCurrentStock = useCallback(async () => {
    if (!activeAnalysis) return;
    setIsTracking(true);
    try {
      const c = activeAnalysis.commentary;
      const curPrice = activeAnalysis.current_price ?? (activeAnalysis as any).price;
      const isBearish = c?.bias ? c.bias.toUpperCase().includes('BEAR') : false;
      const tradeAction: 'BUY' | 'SELL' = isBearish ? 'SELL' : 'BUY';
      await addManualShortlistTrade({
        symbol: activeAnalysis.symbol,
        action: tradeAction,
        current_price: curPrice,
        entry_price: c?.trigger_price || curPrice,
        stop_loss: c?.stop_loss,
        target_1: c?.target_price_1,
        target_2: c?.target_price_2,
        setup_type: c?.bias ? c.bias.replace(/_/g, ' ') : undefined,
        thesis: c?.actionable_thesis,
        catalyst_summary: c?.latest_news,
        tape_evidence: c?.institutional_flow_detail?.summary || c?.institutional_flow,
        risk_reward_ratio: c?.risk_reward_ratio,
      });
      setTrackSuccess(true);
      setTimeout(() => setTrackSuccess(false), 2000);
    } catch (err) {
      console.error('Failed to add stock to live tracking:', err);
    } finally {
      setIsTracking(false);
    }
  }, [activeAnalysis]);

  // Copy single stock technical commentary
  const handleCopySingleStock = useCallback(async (item: SymbolAnalysisPayload) => {
    const c = item.commentary;
    const priceVal = Number(item.current_price ?? (item as any).price ?? item.timeframes?.['1D']?.candles?.slice(-1)[0]?.close ?? c?.trigger_price ?? 0);
    const priceStr = `₹${priceVal.toFixed(2)}`;
    const changeVal = Number(item.change_pct ?? 0);
    const changeStr = `${changeVal >= 0 ? '+' : ''}${changeVal.toFixed(2)}%`;
    const scoreStr = `${c?.potential_score ?? 0}/100 [Tier ${c?.quality_tier ?? '-'}]`;
    const biasStr = c?.bias ? c.bias.replace(/_/g, ' ') : 'NEUTRAL';

    const levels = [
      c?.trigger_price ? `Trigger: ₹${c.trigger_price.toFixed(2)}` : null,
      c?.stop_loss ? `SL: ₹${c.stop_loss.toFixed(2)}` : null,
      c?.target_price_1 ? `T1: ₹${c.target_price_1.toFixed(2)}` : null,
      c?.target_price_2 ? `T2: ₹${c.target_price_2.toFixed(2)}` : null,
    ].filter(Boolean).join(' | ');

    const liveRsi = (c as any)?.live_rsi_5m ?? (c as any)?.rsi_5m;
    const liveRvol = (c as any)?.live_rvol ?? (c as any)?.rvol;
    const liveStatus = (c as any)?.live_status;
    const liveMetrics = [
      liveRsi != null ? `5m RSI: ${liveRsi}` : null,
      liveRvol != null ? `RVOL: ${liveRvol}x` : null,
      liveStatus ? `Status: ${String(liveStatus).replace(/_/g, ' ')}` : null,
    ].filter(Boolean).join(' | ');

    const text = [
      `📌 ${item.symbol} Technical Analysis  |  ${priceStr} (${changeStr})`,
      `Score: ${scoreStr}  |  Bias: ${biasStr}`,
      levels ? `Key Levels: ${levels}` : null,
      liveMetrics ? `Institutional Acceptance: ${liveMetrics}` : null,
      `Actionable Thesis: ${c?.actionable_thesis || 'N/A'}`,
      '',
      '[BASELINE STRUCTURE]',
      `${c?.structure_base || 'N/A'}`,
      '',
      '[MULTI-TIMEFRAME TREND]',
      `${c?.multi_tf_trend || 'N/A'}`,
      '',
      '[INDICATOR INTERACTION]',
      `${c?.indicator_interaction || 'N/A'}`,
      '',
      '[VOLATILITY STATE]',
      `${c?.volatility_state || 'N/A'}`,
      '',
      '[FINBERT SENTIMENT & NARRATIVE INTELLIGENCE (24H)]',
      c?.sentiment_detail
        ? [
            `Score: ${c.sentiment_detail.sentiment_score > 0 ? `+${c.sentiment_detail.sentiment_score}` : c.sentiment_detail.sentiment_score}/100 [${c.sentiment_detail.sentiment_label.replace(/_/g, ' ')}]`,
            `Distribution: Bull: ${Math.round(c.sentiment_detail.probabilities.positive * 100)}% | Neu: ${Math.round(c.sentiment_detail.probabilities.neutral * 100)}% | Bear: ${Math.round(c.sentiment_detail.probabilities.negative * 100)}%`,
            c.sentiment_detail.narrative_alignment ? `Narrative Alignment: ${c.sentiment_detail.narrative_alignment.badge} — ${c.sentiment_detail.narrative_alignment.detail}` : null,
            `Catalysts (${c.sentiment_detail.article_count}): ${c.sentiment_detail.headlines.map(h => `${h.is_regulatory ? '[BSE Filing] ' : ''}[${h.source}] ${h.title} (${h.sentiment})`).join(' | ') || 'No major headlines'}`,
          ].filter(Boolean).join('\n')
        : `${c?.latest_news || 'No news in past 24h'}`,
      '',
      '[INSTITUTIONAL FLOW ENGINE (IFP)]',
      c?.institutional_flow_detail
        ? [
            `Score: ${c.institutional_flow_detail.score > 0 ? `+${c.institutional_flow_detail.score}` : c.institutional_flow_detail.score}/100 [${c.institutional_flow_detail.classification.replace(/_/g, ' ')}]`,
            `Confidence: ${c.institutional_flow_detail.confidence}% | Coverage: ${c.institutional_flow_detail.coverage}%`,
            `Trend: 5D: ${c.institutional_flow_detail.trend.trend_5d > 0 ? `+${c.institutional_flow_detail.trend.trend_5d}` : c.institutional_flow_detail.trend.trend_5d} | 20D: ${c.institutional_flow_detail.trend.trend_20d > 0 ? `+${c.institutional_flow_detail.trend.trend_20d}` : c.institutional_flow_detail.trend.trend_20d} | 10D Persistence: ${Math.round(c.institutional_flow_detail.trend.persistence_10d * 100)}%`,
            `Components: Vol: ${c.institutional_flow_detail.components.volume ?? 'N/A'}, CVD: ${c.institutional_flow_detail.components.cvd ?? 'N/A'}, Large: ${c.institutional_flow_detail.components.large_trades ?? 'N/A'}, Depth: ${c.institutional_flow_detail.components.depth ?? 'N/A'}, AVWAP: ${c.institutional_flow_detail.components.price_acceptance ?? 'N/A'}, F&O: ${c.institutional_flow_detail.components.fno_oi ?? 'N/A (Non-F&O)'}`,
            c.institutional_flow_detail.stealth_phase ? `Regime: ${c.institutional_flow_detail.stealth_phase}` : null,
            c.institutional_flow_detail.conflicts?.length ? `Conflicts: ${c.institutional_flow_detail.conflicts.join('; ')}` : null,
            `Narrative: ${c.institutional_flow_detail.summary}`,
          ].filter(Boolean).join('\n')
        : (c?.institutional_flow || 'Neutral / Balanced Flow'),
    ].filter((line) => line !== null).join('\n');

    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
      }
      setSingleCopied(true);
      setTimeout(() => setSingleCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy single stock:', err);
    }
  }, []);

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col h-full w-full text-slate-200 overflow-hidden bg-slate-950 font-sans select-none">
      {/* ── Compact Top Bar: Universe & Scanner Controls ─────────────────────── */}
      <header className="h-11 px-3 border-b border-slate-800/80 bg-slate-900/60 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-cyan-500/20 to-emerald-500/20 border border-cyan-500/30 flex items-center justify-center text-cyan-400">
            <Activity className="w-4 h-4" />
          </div>
          <div className="flex items-center gap-2">
            <h1 className="text-sm font-black text-white tracking-tight">Chart Analysis</h1>
            <span className="text-[10px] uppercase font-bold tracking-wider px-1.5 py-0.5 rounded bg-cyan-950 text-cyan-300 border border-cyan-800/50">
              1D • 1W • 1M
            </span>
          </div>

          {/* Universe Selector Dropdown */}
          <div className="flex items-center ml-2">
            <select
              value={universe}
              onChange={(e) => setUniverse(e.target.value as UniverseOption)}
              className="bg-slate-800/90 border border-slate-700 text-xs text-white rounded-lg px-2.5 py-1 focus:outline-none focus:border-cyan-500"
            >
              <option value="all">All Chart Universe ({symbols.length || '640+'})</option>
              <option value="nifty50">NIFTY 50</option>
              <option value="niftynext50">NIFTY Next 50</option>
              <option value="midcap">NIFTY Midcap (200+)</option>
              <option value="smallcap">NIFTY Smallcap (350+)</option>
            </select>
          </div>
        </div>

        {/* Action Controls */}
        <div className="flex items-center gap-2">
          {lastScannedAt && (
            <span className="text-[10px] text-slate-400 mr-1 hidden md:inline">
              Updated: {lastScannedAt} ({Object.keys(analyzedStocks).length} stocks)
            </span>
          )}



          {!isScanning ? (
            <button
              onClick={startFullScan}
              className="px-3.5 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs shadow-md shadow-emerald-950/40 active:scale-95 transition-all flex items-center gap-1.5"
            >
              <Play className="w-3.5 h-3.5 fill-slate-950" />
              Start Analyzing
            </button>
          ) : (
            <button
              onClick={stopScan}
              className="px-3 py-1.5 rounded-lg bg-rose-500/20 text-rose-300 border border-rose-500/40 font-semibold text-xs hover:bg-rose-500/30 transition-all flex items-center gap-1.5"
            >
              <Square className="w-3.5 h-3.5 fill-rose-300" />
              Stop ({currentScanIdx + 1}/{symbols.length})
            </button>
          )}

          {/* Live Analysis Toggle Button */}
          <button
            onClick={handleToggleLiveAnalysis}
            disabled={isScanning}
            title={
              isLiveMode
                ? "Pause real-time surveillance and freeze levels"
                : "Engage real-time live analysis with continuous 3s price surveillance"
            }
            className={`px-3 py-1.5 rounded-lg text-xs font-bold transition-all flex items-center gap-1.5 shadow-sm active:scale-95 ${
              isLiveMode
                ? 'bg-cyan-500 hover:bg-cyan-400 text-slate-950 shadow-cyan-500/25 ring-2 ring-cyan-400/40'
                : 'bg-slate-800/90 hover:bg-slate-700/90 text-cyan-400 border border-cyan-500/40 hover:border-cyan-400'
            }`}
          >
            <Radio className={`w-3.5 h-3.5 ${isLiveMode ? 'animate-pulse text-slate-950' : 'text-cyan-400'}`} />
            {isLiveMode ? (isLiveWarmingUp ? 'Warming Up Fresh...' : 'Live Active (3s)') : 'Live Analysis'}
          </button>
        </div>
      </header>

      {/* ── Scan or Live Warmup Progress Bar (when active) ─────────────────── */}
      {(isScanning || isLiveWarmingUp) && (
        <div className="h-6 bg-slate-900 border-b border-cyan-500/30 px-3 flex items-center justify-between text-[10px] shrink-0">
          <div className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-cyan-400 animate-ping" />
            <span className="font-semibold text-cyan-300">
              {isLiveWarmingUp
                ? `Warming Up Fresh Multi-Timeframe Foundation (${currentScanIdx + 1} / ${symbols.length})... Filtering Tier A+ (Score ≥ 85)`
                : `Analyzing Screener Universe: ${currentScanIdx + 1} / ${symbols.length}`}
            </span>
          </div>
          <div className="w-48 h-1.5 bg-slate-800 rounded-full overflow-hidden border border-slate-700">
            <div
              className="h-full bg-cyan-400 transition-all duration-200"
              style={{ width: `${Math.round(((currentScanIdx + 1) / (symbols.length || 1)) * 100)}%` }}
            />
          </div>
        </div>
      )}

      {/* ── Live Surveillance Heartbeat Bar (when live and warmup complete) ── */}
      {isLiveMode && !isLiveWarmingUp && (
        liveConnectionLost ? (
          <div className="h-5 bg-rose-950/40 border-b border-rose-800/40 px-3 flex items-center justify-between text-[10px] shrink-0 text-rose-300">
            <div className="flex items-center gap-2 font-mono">
              <span className="w-1.5 h-1.5 rounded-full bg-rose-400 animate-ping" />
              <span className="font-semibold tracking-wide">⚠️ LIVE CONNECTION LOST • RETRYING IN 10s...</span>
            </div>
            <span className="text-[10px] text-slate-400 font-mono">
              Last Tick: {liveTickTimestamp || 'N/A'} • Data may be stale
            </span>
          </div>
        ) : (
          <div className="h-5 bg-cyan-950/40 border-b border-cyan-800/40 px-3 flex items-center justify-between text-[10px] shrink-0 text-cyan-300">
            <div className="flex items-center gap-2 font-mono">
              <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 animate-pulse" />
              <span className="font-semibold tracking-wide">LIVE REAL-TIME SURVEILLANCE • 3s TICKS • TIER A+ (SCORE ≥ 85)</span>
            </div>
            <span className="text-[10px] text-slate-400 font-mono">
              Last Tick: {liveTickTimestamp || 'streaming'} • {Object.keys(analyzedStocks).length} Stocks Active
            </span>
          </div>
        )
      )}

      {/* ── Main Workspace: 3-Column Zero-Waste Layout ────────────────────── */}
      <div className="flex-1 flex min-h-0 overflow-hidden">
        
        {/* ── LEFT COLUMN: Searchable & Ranked Results List ────────────────── */}
        <div className="w-[230px] min-w-[230px] max-w-[230px] border-r border-slate-800/80 bg-slate-900/30 flex flex-col shrink-0 min-h-0">
          {/* Search Box & Copy Filtered Analysis Button */}
          <div className="p-2 border-b border-slate-800/80">
            <div className="flex items-center gap-1.5">
              <div className="relative flex-1 min-w-0">
                <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-1/2 -translate-y-1/2" />
                <input
                  type="text"
                  placeholder="Filter symbol or bias..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="w-full pl-8 pr-2 py-1 bg-slate-800/70 border border-slate-700/60 rounded-lg text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                />
              </div>



              {/* Single Unified Copy Analysis Button */}
              <button
                onClick={handleCopyFilteredAnalysis}
                disabled={rankedStocks.length === 0}
                title={
                  rankedStocks.length > 0
                    ? `Copy ${rankedStocks.length} technical analyses (${activeFilterLabel})`
                    : 'No analyzed stocks to copy'
                }
                className={`px-2.5 py-1 rounded-lg border text-xs font-semibold transition-all shrink-0 flex items-center justify-center gap-1.5 ${
                  copied
                    ? 'bg-emerald-500/25 text-emerald-300 border-emerald-500/50 shadow-sm shadow-emerald-950/50'
                    : 'bg-slate-800/90 hover:bg-slate-700 text-slate-300 hover:text-white border-slate-700/70 active:scale-95 disabled:opacity-40 disabled:cursor-not-allowed'
                }`}
              >
                {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5 text-slate-300" />}
                <span className="text-[11px] font-medium">{copied ? 'Copied' : 'Copy'}</span>
              </button>
            </div>


            {/* Filter Pills */}
            <div className="flex items-center gap-1 mt-2 overflow-x-auto custom-scrollbar pb-0.5 text-[10px]">
              {(['all', 'breakout', 'bounce', 'squeeze', 'breakdown'] as FilterType[]).map((f) => (
                <button
                  key={f}
                  onClick={() => setActiveFilter(f)}
                  className={`px-2 py-0.5 rounded font-medium whitespace-nowrap transition-all ${
                    activeFilter === f
                      ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/40'
                      : 'bg-slate-800/40 text-slate-400 hover:text-slate-200 border border-transparent'
                  }`}
                >
                  {f === 'all' ? 'All' : f === 'breakout' ? '⚡ Breakout' : f === 'bounce' ? '🎯 Bounce' : f === 'squeeze' ? '⏳ Squeeze' : '🛑 Breakdown'}
                </button>
              ))}
            </div>
          </div>

          {/* List Counter */}
          <div className="px-3 py-1 bg-slate-950/40 border-b border-slate-800/60 flex items-center justify-between text-[10px] text-slate-400 font-mono">
            <span>{isLiveMode ? 'Tier A+ Setups (Score ≥ 85)' : 'Ranked by Potential Score'}</span>
            <span className={copied ? 'text-emerald-400 font-bold transition-colors' : ''}>
              {copied ? `✓ Copied ${rankedStocks.length} Stocks!` : `${rankedStocks.length} Analyzed`}
            </span>
          </div>

          {/* Stock Cards List */}
          <div className="flex-1 overflow-y-auto custom-scrollbar p-1.5 space-y-1">
            {rankedStocks.length === 0 && unanalyzedMatches.length === 0 ? (
              <div className="p-6 text-center text-slate-500 text-xs">
                <Sparkles className="w-6 h-6 text-slate-600 mx-auto mb-1.5 opacity-50" />
                {isLiveMode && isLiveWarmingUp ? (
                  <span>Warming up universe... Searching for <span className="text-emerald-400 font-semibold">Tier A+ (Score ≥ 85)</span> setups.</span>
                ) : isLiveMode ? (
                  <span>No stocks qualified with <span className="text-emerald-400 font-semibold">Score ≥ 85</span> during warmup. Click Start Analysis to view all stocks.</span>
                ) : (
                  <>Click <span className="text-emerald-400 font-semibold">Start Analyzing</span> to populate analysis.</>
                )}
              </div>
            ) : (
              <>
                {rankedStocks.map((item, idx) => {
                  const isSelected = selectedSymbol === item.symbol;
                  const score = isLiveMode
                    ? ((item.commentary as any)?.live_potential_score ?? item.commentary?.potential_score ?? 0)
                    : (item.commentary?.potential_score || 0);
                  const scoreColor =
                    score >= 85 ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30' :
                    score >= 70 ? 'text-cyan-400 bg-cyan-500/10 border-cyan-500/30' :
                    'text-amber-400 bg-amber-500/10 border-amber-500/30';

                  return (
                    <button
                      key={item.symbol}
                      onClick={() => setSelectedSymbol(item.symbol)}
                      className={`w-full text-left p-2 rounded-lg border transition-all ${
                        isSelected
                          ? 'bg-slate-800 border-cyan-500/60 shadow-sm'
                          : 'bg-slate-900/30 border-slate-800/50 hover:bg-slate-800/40'
                      }`}
                    >
                      <div className="flex items-center justify-between mb-0.5">
                        <div className="flex items-center gap-1.5">
                          <span className="text-[9px] font-mono text-slate-500">#{idx + 1}</span>
                          <span className="font-bold text-white text-xs">{item.symbol}</span>
                        </div>
                        <div className="flex items-center gap-1">
                          <span className={`px-1.5 py-0.2 rounded text-[10px] font-mono font-bold border ${scoreColor}`}>
                            {score} pts
                          </span>
                        </div>
                      </div>

                      <div className="flex items-center justify-between text-[11px] text-slate-400">
                        <span className={`transition-colors duration-300 rounded px-1 -mx-1 ${
                          livePriceFlashes[item.symbol] === 'up'
                            ? 'bg-emerald-500/30 text-emerald-300 font-bold'
                            : livePriceFlashes[item.symbol] === 'down'
                            ? 'bg-rose-500/30 text-rose-300 font-bold'
                            : ''
                        }`}>
                          ₹{item.current_price.toFixed(2)}
                        </span>
                        <span className={item.change_pct >= 0 ? 'text-emerald-400 font-semibold' : 'text-rose-400 font-semibold'}>
                          {item.change_pct >= 0 ? '+' : ''}{item.change_pct.toFixed(2)}%
                        </span>
                      </div>

                      {/* Live surveillance trigger status badge */}
                      {isLiveMode && (item.commentary as any)?.live_status && (
                        <div className="flex items-center justify-between pt-0.5 mt-0.5 text-[9px] font-mono">
                          {(item.commentary as any).live_status === 'CONFIRMED_TRIGGER' ? (
                            <span className="text-emerald-400 font-bold flex items-center gap-0.5">
                              <span>🎯 CONFIRMED</span>
                              <span>({(item.commentary as any).live_pnl_pct >= 0 ? '+' : ''}{(item.commentary as any).live_pnl_pct}%)</span>
                            </span>
                          ) : (item.commentary as any).live_status === 'SUSTAINED_BREAKOUT' ? (
                            <span className="text-cyan-300 font-bold flex items-center gap-0.5">
                              <span>🚀 SUSTAINED</span>
                              <span>(+{(item.commentary as any).live_pnl_pct}%)</span>
                            </span>
                          ) : (item.commentary as any).live_status === 'TRIGGER_PENDING' ? (
                            <span className="text-amber-300 font-bold flex items-center gap-0.5 animate-pulse">
                              <span>⏳ PENDING</span>
                              <span>({Math.round((item.commentary as any).live_sustain_seconds || 0)}s / 120s)</span>
                            </span>
                          ) : (item.commentary as any).live_status === 'TRIGGERED' ? (
                            <span className="text-emerald-400 font-bold flex items-center gap-0.5">
                              <span>🎯 ACTIVE</span>
                              <span>({(item.commentary as any).live_pnl_pct >= 0 ? '+' : ''}{(item.commentary as any).live_pnl_pct}%)</span>
                            </span>
                          ) : (item.commentary as any).live_status === 'TP1_HIT' ? (
                            <span className="text-cyan-300 font-bold flex items-center gap-0.5">
                              <span>🟢 T1 HIT</span>
                              <span>(+{(item.commentary as any).live_pnl_pct}%)</span>
                            </span>
                          ) : (item.commentary as any).live_status === 'TP2_HIT' ? (
                            <span className="text-purple-300 font-bold flex items-center gap-0.5">
                              <span>🏆 T2 HIT</span>
                              <span>(+{(item.commentary as any).live_pnl_pct}%)</span>
                            </span>
                          ) : (item.commentary as any).live_status === 'SL_HIT' ? (
                            <span className="text-rose-400 font-bold flex items-center gap-0.5">
                              <span>🛡️ SL HIT</span>
                              <span>({(item.commentary as any).live_pnl_pct}%)</span>
                            </span>
                          ) : (
                            <span className="text-slate-400">
                              ⏱️ {Math.abs((item.commentary as any).distance_to_trigger ?? 0)}% to Trig
                            </span>
                          )}
                          <span className="text-[9px] text-slate-500">
                            Trig: ₹{(item.commentary.trigger_price || item.current_price).toFixed(1)}
                          </span>
                        </div>
                      )}

                      {/* 5m RSI & RVOL metric tags on stock card */}
                      {(() => {
                        const rsi = (item.commentary as any)?.live_rsi_5m ?? (item.commentary as any)?.rsi_5m;
                        const rvol = (item.commentary as any)?.live_rvol ?? (item.commentary as any)?.rvol;
                        if (rsi == null && rvol == null) return null;
                        const isOB = rsi != null && rsi >= 73;
                        const isCaution = rsi != null && rsi >= 65 && rsi < 73;
                        return (
                          <div className="flex items-center gap-1.5 pt-0.5 text-[9px] font-mono">
                            {rsi != null && (
                              <span
                                title={isOB ? '5m RSI ≥ 73 (Overbought). Trigger bonus gated.' : isCaution ? '5m RSI 65-73 (Caution zone)' : '5m RSI healthy (<65)'}
                                className={`px-1 py-0.2 rounded border ${
                                  isOB
                                    ? 'bg-rose-500/20 text-rose-300 border-rose-500/40 font-bold'
                                    : isCaution
                                    ? 'bg-amber-500/20 text-amber-300 border-amber-500/40'
                                    : 'bg-slate-800 text-slate-300 border-slate-700'
                                }`}
                              >
                                RSI: {rsi}{isOB ? ' ⚠️' : ''}
                              </span>
                            )}
                            {rvol != null && (
                              <span
                                title={`Relative Volume: ${rvol}x`}
                                className={`px-1 py-0.2 rounded border ${
                                  rvol >= 2.0
                                    ? 'bg-purple-500/20 text-purple-300 border-purple-500/40 font-bold'
                                    : rvol >= 1.5
                                    ? 'bg-cyan-500/20 text-cyan-300 border-cyan-500/40'
                                    : 'bg-slate-800 text-slate-400 border-slate-700'
                                }`}
                              >
                                RVOL: {rvol}x
                              </span>
                            )}
                          </div>
                        );
                      })()}

                      <div className="flex items-center justify-between pt-1 mt-1 border-t border-slate-800/40 text-[9px]">
                        <span className="text-slate-300 font-medium">
                          {item.commentary.bias.replace(/_/g, ' ')}
                        </span>
                        {item.consolidation.is_consolidating && (
                          <span className="text-cyan-400 font-mono">
                            Coil: {item.consolidation.range_span_pct}% ({item.consolidation.duration_bars}d)
                          </span>
                        )}
                      </div>
                    </button>
                  );
                })}

                {unanalyzedMatches.length > 0 && (
                  <div className="pt-2 border-t border-slate-800/80 mt-2">
                    <div className="px-1 mb-1.5 text-[9px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1">
                      <Search className="w-3 h-3 text-cyan-400" />
                      <span>Universe Stocks ({unanalyzedMatches.length})</span>
                    </div>
                    <div className="space-y-1">
                      {unanalyzedMatches.map((s) => (
                        <button
                          key={s.symbol}
                          onClick={() => {
                            setSelectedSymbol(s.symbol);
                            getSymbolAnalysis(s.symbol, { force_refresh: true })
                              .then((data) => setAnalyzedStocks((prev) => ({ ...prev, [s.symbol]: data })))
                              .catch((err) => console.error(`Failed to analyze ${s.symbol}:`, err));
                          }}
                          className="w-full text-left p-2 rounded-lg border border-dashed border-cyan-500/40 bg-cyan-950/20 hover:bg-cyan-900/40 text-cyan-300 transition-all flex items-center justify-between group"
                        >
                          <div className="flex items-center gap-1.5">
                            <Sparkles className="w-3 h-3 text-cyan-400 group-hover:scale-110 transition-transform" />
                            <span className="font-bold text-xs text-white">{s.symbol}</span>
                          </div>
                          <span className="text-[10px] text-cyan-400 font-semibold px-1.5 py-0.5 rounded bg-cyan-900/40 border border-cyan-700/50">
                            Analyze Now
                          </span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>

        {/* ── CENTER COLUMN: Interactive Multi-Timeframe Chart Canvas ──────── */}
        <div className="flex-1 flex flex-col min-w-0 min-h-0 bg-slate-950 relative">
          {/* Chart Header Bar */}
          <div className="h-8 px-2.5 border-b border-slate-800/80 bg-slate-900/50 flex items-center justify-between shrink-0 text-xs gap-2">
            {/* Symbol & Timeframe Switcher */}
            <div className="flex items-center gap-2 shrink-0">
              <span className="text-xs font-black text-white tracking-wide truncate max-w-[110px]">{selectedSymbol || 'Select a Stock'}</span>
              {activeAnalysis && (
                <span className="text-[11px] font-mono text-cyan-300 font-bold shrink-0">
                  ₹{activeAnalysis.current_price.toFixed(2)}
                </span>
              )}

              {/* 5m RSI Badge - Always Visible across ALL timeframes (1D, 1W, 1M) as requested */}
              {activeAnalysis && (() => {
                const rsi = (activeAnalysis.commentary as any)?.live_rsi_5m ?? (activeAnalysis.commentary as any)?.rsi_5m;
                if (rsi == null) return null;
                const isOB = rsi >= 73;
                const isCaution = rsi >= 65 && rsi < 73;
                return (
                  <div
                    title={
                      isOB
                        ? `5m RSI: ${rsi} — Overbought (≥73). Trigger bonus gated.`
                        : isCaution
                        ? `5m RSI: ${rsi} — Caution zone (65-73). Reduced momentum room.`
                        : `5m RSI: ${rsi} — Healthy momentum room (<65).`
                    }
                    className={`flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono font-bold border transition-colors ${
                      isOB
                        ? 'bg-rose-500/20 text-rose-300 border-rose-500/50'
                        : isCaution
                        ? 'bg-amber-500/20 text-amber-300 border-amber-500/40'
                        : 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40'
                    }`}
                  >
                    <Activity className="w-2.5 h-2.5" />
                    <span>5m RSI: {rsi}</span>
                    {isOB && <span className="text-[8px] bg-rose-950 px-1 py-0.2 rounded text-rose-300 font-bold">OB</span>}
                  </div>
                );
              })()}

              {/* Timeframe Buttons */}
              <div className="flex items-center bg-slate-800/80 p-0.5 rounded-lg border border-slate-700/50 text-[10px]">
                {(['1D', '1W', '1M'] as const).map((tf) => (
                  <button
                    key={tf}
                    onClick={() => setActiveTimeframe(tf)}
                    title={
                      tf === '1D'
                        ? 'Daily (1D) Candles: Full session-by-session history'
                        : tf === '1W'
                        ? 'Weekly (1W) Candles: Mon-anchored, aggregates week-to-date sessions'
                        : 'Monthly (1M) Candles: 1st-anchored, aggregates month-to-date sessions'
                    }
                    className={`px-2 py-0.5 rounded font-semibold transition-all ${
                      activeTimeframe === tf
                        ? 'bg-cyan-500 text-slate-950 shadow-sm'
                        : 'text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {tf === '1D' ? '1D' : tf === '1W' ? '1W' : '1M'}
                  </button>
                ))}
              </div>
            </div>

            {/* Dynamic Indicator Toggles */}
            <div className="flex items-center gap-1 text-[10px] shrink-0">
              <button
                onClick={() => setShowLine1((v) => !v)}
                className={`px-1.5 py-0.5 rounded border font-medium transition-all ${
                  showLine1 ? 'bg-cyan-500/20 text-cyan-300 border-cyan-500/40' : 'bg-slate-900 text-slate-500 border-slate-800'
                }`}
              >
                {indicatorLabels.l1}
              </button>
              <button
                onClick={() => setShowLine2((v) => !v)}
                className={`px-1.5 py-0.5 rounded border font-medium transition-all ${
                  showLine2 ? 'bg-amber-500/20 text-amber-300 border-amber-500/40' : 'bg-slate-900 text-slate-500 border-slate-800'
                }`}
              >
                {indicatorLabels.l2}
              </button>
              <button
                onClick={() => setShowLine3((v) => !v)}
                className={`px-1.5 py-0.5 rounded border font-medium transition-all ${
                  showLine3 ? 'bg-slate-200/20 text-slate-200 border-slate-400/40' : 'bg-slate-900 text-slate-500 border-slate-800'
                }`}
              >
                {indicatorLabels.l3}
              </button>
              <button
                onClick={() => setShowLine4((v) => !v)}
                className={`px-1.5 py-0.5 rounded border font-medium transition-all ${
                  showLine4 ? 'bg-purple-500/20 text-purple-300 border-purple-500/40' : 'bg-slate-900 text-slate-500 border-slate-800'
                }`}
              >
                {indicatorLabels.l4}
              </button>
              <button
                onClick={() => setShowLevels((v) => !v)}
                className={`px-1.5 py-0.5 rounded border font-medium transition-all ${
                  showLevels ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' : 'bg-slate-900 text-slate-500 border-slate-800'
                }`}
              >
                Levels
              </button>
              <button
                onClick={() => setShowVolume((v) => !v)}
                className={`px-1.5 py-0.5 rounded border font-medium transition-all flex items-center gap-1 ${
                  showVolume ? 'bg-sky-500/20 text-sky-300 border-sky-500/40' : 'bg-slate-900 text-slate-500 border-slate-800'
                }`}
                title="Toggle Volume Histogram Overlay"
              >
                <BarChart2 className="w-2.5 h-2.5" />
                Vol
              </button>

              <button
                onClick={handleResetView}
                className="px-1.5 py-0.5 rounded border font-medium transition-all bg-slate-800 text-slate-300 hover:text-cyan-300 hover:bg-slate-700 border-slate-700 flex items-center gap-1 shadow-sm"
                title="Auto-Fit Price & Time Scales (Double click chart to reset)"
              >
                <Maximize2 className="w-2.5 h-2.5" />
                Auto Fit
              </button>
            </div>
          </div>

          {/* Crosshair HUD Bar */}
          <div className="h-5 px-2.5 bg-slate-900/60 border-b border-slate-800/40 flex items-center justify-between text-[10px] font-mono shrink-0 overflow-hidden">
            <div className="flex items-center gap-2 overflow-hidden text-ellipsis whitespace-nowrap">
              {hoverData ? (
                <>
                  <span className="text-slate-400">Date: <strong className="text-white">{hoverData.time}</strong></span>
                  <span className="text-slate-400">O: <strong className="text-white">{hoverData.open?.toFixed(2)}</strong></span>
                  <span className="text-slate-400">H: <strong className="text-emerald-400">{hoverData.high?.toFixed(2)}</strong></span>
                  <span className="text-slate-400">L: <strong className="text-rose-400">{hoverData.low?.toFixed(2)}</strong></span>
                  <span className="text-slate-400">C: <strong className="text-white">{hoverData.close?.toFixed(2)}</strong></span>
                  {hoverData.volume !== undefined && (
                    <span className="text-sky-400">Vol: <strong className="text-white">{formatVolume(hoverData.volume)}</strong></span>
                  )}
                  {hoverData.val1 !== undefined && (
                    <span className="text-cyan-400">{indicatorLabels.l1}: <strong>{hoverData.val1.toFixed(2)}</strong></span>
                  )}
                  {hoverData.val2 !== undefined && (
                    <span className="text-amber-400">{indicatorLabels.l2}: <strong>{hoverData.val2.toFixed(2)}</strong></span>
                  )}
                  {hoverData.val3 !== undefined && (
                    <span className="text-slate-300">{indicatorLabels.l3}: <strong>{hoverData.val3.toFixed(2)}</strong></span>
                  )}
                  {hoverData.val4 !== undefined && (
                    <span className="text-purple-400">{indicatorLabels.l4}: <strong>{hoverData.val4.toFixed(2)}</strong></span>
                  )}
                </>
              ) : (
                <span className="text-slate-500 text-[10px]">Hover chart to inspect candle and indicator levels</span>
              )}
            </div>

            {/* Permanent 5m RSI & RVOL in HUD right corner */}
            {activeAnalysis && (() => {
              const rsi = (activeAnalysis.commentary as any)?.live_rsi_5m ?? (activeAnalysis.commentary as any)?.rsi_5m;
              const rvol = (activeAnalysis.commentary as any)?.live_rvol ?? (activeAnalysis.commentary as any)?.rvol;
              if (rsi == null && rvol == null) return null;
              const isOB = rsi != null && rsi >= 73;
              return (
                <div className="flex items-center gap-2 shrink-0 pl-2">
                  {rsi != null && (
                    <span className={`font-bold ${isOB ? 'text-rose-400' : 'text-cyan-400'}`}>
                      5m RSI: {rsi}{isOB ? ' (OB)' : ''}
                    </span>
                  )}
                  {rvol != null && (
                    <span className="text-purple-300 font-bold">
                      RVOL: {rvol}x
                    </span>
                  )}
                </div>
              );
            })()}
          </div>

          {/* Chart Viewport: Dynamically measured with zero overflow */}
          <div ref={chartContainerRef} className="flex-1 w-full min-h-0 relative" />
        </div>

        {/* ── RIGHT COLUMN: Technical Commentary & Actionable Thesis Card ──── */}
        <div className="w-[310px] min-w-[310px] max-w-[310px] border-l border-slate-800/80 bg-slate-900/40 flex flex-col shrink-0 min-h-0 overflow-y-auto custom-scrollbar p-2.5 space-y-2">
          {activeAnalysis ? (
            <>


              {/* Potential Score Banner */}
              <div className="p-3 rounded-xl bg-slate-900 border border-slate-800 shadow-md">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Analysis Score</span>
                  <div className="flex items-center gap-1.5">
                    {/* Add to Live Tracking */}
                    <button
                      onClick={handleTrackCurrentStock}
                      disabled={isTracking}
                      title={`Add ${activeAnalysis.symbol} setup to Live Tracking surveillance`}
                      className={`px-2 py-0.5 rounded text-[10px] font-semibold flex items-center gap-1 transition-all border ${
                        trackSuccess
                          ? 'bg-emerald-500/25 text-emerald-300 border-emerald-500/50'
                          : 'bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-300 border-cyan-500/40 active:scale-95'
                      }`}
                    >
                      {trackSuccess ? <Check className="w-3 h-3 text-emerald-400" /> : <Activity className="w-3 h-3 text-cyan-400" />}
                      <span>{trackSuccess ? 'Tracking ✓' : '+ Track'}</span>
                    </button>

                    <button
                      onClick={() => handleCopySingleStock(activeAnalysis)}
                      title={`Copy ${activeAnalysis.symbol} analysis to clipboard`}
                      className="px-1.5 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white border border-slate-700/60 text-[10px] flex items-center gap-1 transition-all"
                    >
                      {singleCopied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
                      <span>{singleCopied ? 'Copied' : 'Copy'}</span>
                    </button>
                    <span className="px-2 py-0.2 rounded-full text-[10px] font-black bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">
                      Tier {activeAnalysis.commentary.quality_tier}
                    </span>
                  </div>
                </div>

                <div className="flex items-baseline gap-2 mb-1.5">
                  <span className="text-2xl font-black text-white tracking-tight">
                    {isLiveMode
                      ? ((activeAnalysis.commentary as any)?.live_potential_score ?? activeAnalysis.commentary.potential_score)
                      : activeAnalysis.commentary.potential_score}
                  </span>
                  <span className="text-xs font-semibold text-slate-400">/ 100 Potential</span>
                  {isLiveMode && (activeAnalysis.commentary as any)?.live_potential_score != null && (
                    (() => {
                      const diff = (activeAnalysis.commentary as any).live_potential_score - activeAnalysis.commentary.potential_score;
                      if (diff === 0) return null;
                      return (
                        <span className={`text-xs font-mono font-bold px-1.5 py-0.2 rounded border ${
                          diff > 0 ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' : 'bg-rose-500/20 text-rose-300 border-rose-500/40'
                        }`}>
                          {diff > 0 ? `+${diff}` : `${diff}`} Live
                        </span>
                      );
                    })()
                  )}
                </div>

                <div className="w-full h-1.5 bg-slate-800 rounded-full overflow-hidden mb-2">
                  <div
                    className="h-full bg-gradient-to-r from-cyan-400 via-emerald-400 to-teal-400"
                    style={{ width: `${isLiveMode ? ((activeAnalysis.commentary as any)?.live_potential_score ?? activeAnalysis.commentary.potential_score) : activeAnalysis.commentary.potential_score}%` }}
                  />
                </div>

                <div className="flex items-center justify-between text-[11px] text-slate-300 pt-1.5 border-t border-slate-800/80">
                  <span>Setup Bias:</span>
                  <span className="font-bold text-cyan-300">
                    {activeAnalysis.commentary.bias.replace(/_/g, ' ')}
                  </span>
                </div>
              </div>

              {/* Actionable Trade Thesis Card */}
              <div className="p-3 rounded-xl bg-slate-900/90 border border-slate-800 space-y-2">
                <div className="flex items-center gap-1.5 text-[11px] font-bold text-amber-400 uppercase tracking-wider">
                  <Zap className="w-3.5 h-3.5" />
                  Actionable Thesis & Levels
                </div>
                <p className="text-[11px] text-slate-200 leading-relaxed">
                  {activeAnalysis.commentary.actionable_thesis}
                </p>

                {/* Level Grid */}
                <div className="grid grid-cols-2 gap-1.5 pt-1.5 border-t border-slate-800/80 font-mono text-[11px]">
                  {activeAnalysis.commentary.trigger_price && (
                    <div className="p-1.5 rounded-lg bg-emerald-950/40 border border-emerald-800/50">
                      <div className="text-[9px] text-emerald-400 font-sans">Trigger Entry</div>
                      <div className="text-xs font-bold text-white">₹{activeAnalysis.commentary.trigger_price.toFixed(2)}</div>
                    </div>
                  )}
                  {activeAnalysis.commentary.stop_loss && (
                    <div className="p-1.5 rounded-lg bg-rose-950/40 border border-rose-800/50">
                      <div className="text-[9px] text-rose-400 font-sans">Stop Loss</div>
                      <div className="text-xs font-bold text-white">₹{activeAnalysis.commentary.stop_loss.toFixed(2)}</div>
                    </div>
                  )}
                  {activeAnalysis.commentary.target_price_1 && (
                    <div className="p-1.5 rounded-lg bg-blue-950/40 border border-blue-800/50">
                      <div className="text-[9px] text-blue-400 font-sans">Target 1</div>
                      <div className="text-xs font-bold text-white">₹{activeAnalysis.commentary.target_price_1.toFixed(2)}</div>
                    </div>
                  )}
                  {activeAnalysis.commentary.target_price_2 && (
                    <div className="p-1.5 rounded-lg bg-indigo-950/40 border border-indigo-800/50">
                      <div className="text-[9px] text-indigo-400 font-sans">Target 2</div>
                      <div className="text-xs font-bold text-white">₹{activeAnalysis.commentary.target_price_2.toFixed(2)}</div>
                    </div>
                  )}
                </div>
              </div>

              {/* Institutional Acceptance Matrix (Live Surveillance Durability & RSI Gate) */}
              <div className="p-3 rounded-xl bg-slate-900/90 border border-slate-800 space-y-2.5 shadow-md">
                <div className="flex items-center justify-between text-[11px] font-bold uppercase tracking-wider text-slate-400">
                  <span className="flex items-center gap-1.5 text-cyan-400">
                    <Target className="w-3.5 h-3.5" />
                    Institutional Acceptance
                  </span>
                  {isLiveMode ? (
                    <span className="text-[9px] font-mono text-cyan-300 bg-cyan-950/80 px-1.5 py-0.5 rounded border border-cyan-800/60 font-semibold animate-pulse">
                      Live (3s)
                    </span>
                  ) : (
                    <span className="text-[9px] font-mono text-slate-500 bg-slate-800/60 px-1.5 py-0.5 rounded border border-slate-700/60">
                      Standard
                    </span>
                  )}
                </div>

                {/* 1. Temporal Persistence */}
                <div className="space-y-1 text-[11px] font-mono">
                  <div className="flex items-center justify-between text-slate-300">
                    <span className="text-[10px] text-slate-400 font-sans">Temporal Duration:</span>
                    {(() => {
                      const status = (activeAnalysis.commentary as any)?.live_status || 'WATCHING';
                      const sustainSeconds = (activeAnalysis.commentary as any)?.live_sustain_seconds || 0;
                      if (status === 'SUSTAINED_BREAKOUT') {
                        return <span className="text-cyan-300 font-bold">🚀 5m+ Shelf Sustained</span>;
                      }
                      if (status === 'CONFIRMED_TRIGGER') {
                        return <span className="text-emerald-400 font-bold">🎯 Confirmed (120s+)</span>;
                      }
                      if (status === 'TRIGGER_PENDING') {
                        return <span className="text-amber-300 font-bold animate-pulse">⏳ {Math.round(sustainSeconds)}s / 120s</span>;
                      }
                      if (status === 'TP1_HIT' || status === 'TP2_HIT') {
                        return <span className="text-purple-300 font-bold">Target Hit</span>;
                      }
                      if (status === 'SL_HIT') {
                        return <span className="text-rose-400 font-bold">Stop Hit</span>;
                      }
                      return <span className="text-slate-500">Awaiting Trigger</span>;
                    })()}
                  </div>
                  {/* Progress bar for sustain duration */}
                  {isLiveMode && (activeAnalysis.commentary as any)?.live_status === 'TRIGGER_PENDING' && (
                    <div className="w-full h-1.5 bg-slate-800 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-amber-400 transition-all duration-300"
                        style={{ width: `${Math.min(100, Math.round((sustainSeconds / 120) * 100))}%` }}
                      />
                    </div>
                  )}
                </div>

                {/* 2. 5-Minute RSI Gate */}
                <div className="flex items-center justify-between text-[11px] font-mono pt-1.5 border-t border-slate-800/80">
                  <span className="text-[10px] text-slate-400 font-sans">5m RSI Gate:</span>
                  {(() => {
                    const rsi = (activeAnalysis.commentary as any)?.live_rsi_5m ?? (activeAnalysis.commentary as any)?.rsi_5m;
                    if (rsi == null) return <span className="text-slate-500">N/A</span>;
                    if (rsi >= 73) {
                      return <span className="text-rose-400 font-bold">{rsi} (Overbought • Bonus Gated)</span>;
                    }
                    if (rsi >= 65) {
                      return <span className="text-amber-300 font-bold">{rsi} (Caution Zone • +3 Max)</span>;
                    }
                    return <span className="text-emerald-400 font-bold">{rsi} (Clean • +5 Eligible)</span>;
                  })()}
                </div>

                {/* 3. Relative Volume (RVOL) */}
                <div className="flex items-center justify-between text-[11px] font-mono pt-1.5 border-t border-slate-800/80">
                  <span className="text-[10px] text-slate-400 font-sans">RVOL (20d avg):</span>
                  {(() => {
                    const rvol = (activeAnalysis.commentary as any)?.live_rvol ?? (activeAnalysis.commentary as any)?.rvol;
                    if (rvol == null) return <span className="text-slate-500">N/A</span>;
                    if (rvol >= 2.0) {
                      return <span className="text-purple-300 font-bold">{rvol}x (High Surge +3)</span>;
                    }
                    if (rvol >= 1.5) {
                      return <span className="text-cyan-300 font-bold">{rvol}x (Expansion +2)</span>;
                    }
                    if (rvol < 1.0) {
                      return <span className="text-slate-400">{rvol}x (Sub-par -2)</span>;
                    }
                    return <span className="text-slate-300">{rvol}x (Normal)</span>;
                  })()}
                </div>
              </div>

              {/* Technical Analysis Commentary Breakdown */}
              <div className="space-y-2">
                <div className="p-2.5 rounded-xl bg-slate-900/60 border border-slate-800/80 space-y-1">
                  <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Layers className="w-3 h-3 text-cyan-400" />
                    Base Structure
                  </div>
                  <p className="text-[11px] text-slate-300 leading-relaxed">
                    {activeAnalysis.commentary.structure_base}
                  </p>
                </div>

                <div className="p-2.5 rounded-xl bg-slate-900/60 border border-slate-800/80 space-y-1">
                  <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <TrendingUp className="w-3 h-3 text-emerald-400" />
                    Multi-Timeframe Trend
                  </div>
                  <p className="text-[11px] text-slate-300 leading-relaxed">
                    {activeAnalysis.commentary.multi_tf_trend}
                  </p>
                </div>

                <div className="p-2.5 rounded-xl bg-slate-900/60 border border-slate-800/80 space-y-1">
                  <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Activity className="w-3 h-3 text-purple-400" />
                    Indicator Interaction
                  </div>
                  <p className="text-[11px] text-slate-300 leading-relaxed">
                    {activeAnalysis.commentary.indicator_interaction}
                  </p>
                </div>

                <div className="p-2.5 rounded-xl bg-slate-900/60 border border-slate-800/80 space-y-1">
                  <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Clock className="w-3 h-3 text-amber-400" />
                    Volatility State
                  </div>
                  <p className="text-[11px] text-slate-300 leading-relaxed">
                    {activeAnalysis.commentary.volatility_state}
                  </p>
                </div>

                {/* [5] FinBERT Sentiment & Narrative Intelligence Card */}
                {(() => {
                  const sent = activeAnalysis.commentary.sentiment_detail;
                  const score = sent?.sentiment_score ?? 0;
                  const isPos = score > 0;
                  const scoreSign = isPos ? `+${score}` : `${score}`;
                  const label = sent?.sentiment_label || 'NEUTRAL';
                  const labelFormatted = label.replace(/_/g, ' ');
                  const alignment = sent?.narrative_alignment || activeAnalysis.commentary.narrative_alignment;

                  const scoreColor =
                    score >= 40 ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30' :
                    score >= 15 ? 'text-teal-400 bg-teal-500/10 border-teal-500/30' :
                    score <= -40 ? 'text-rose-400 bg-rose-500/10 border-rose-500/30' :
                    score <= -15 ? 'text-amber-400 bg-amber-500/10 border-amber-500/30' :
                    'text-slate-300 bg-slate-800/80 border-slate-700';

                  const posPct = Math.round((sent?.probabilities?.positive ?? 0.33) * 100);
                  const neuPct = Math.round((sent?.probabilities?.neutral ?? 0.34) * 100);
                  const negPct = Math.round((sent?.probabilities?.negative ?? 0.33) * 100);

                  return (
                    <div className="p-3 rounded-xl bg-slate-900/80 border border-slate-800/90 space-y-2.5 shadow-md">
                      {/* Header with Title & FinBERT Score Pill */}
                      <div className="flex items-center justify-between border-b border-slate-800/80 pb-2">
                        <div className="flex items-center gap-1.5">
                          <Sparkles className="w-3.5 h-3.5 text-sky-400" />
                          <span className="text-[11px] font-bold text-slate-200 tracking-wide">
                            FinBERT Sentiment & Catalysts (24h)
                          </span>
                        </div>
                        <div className="flex items-center gap-1.5">
                          {sent && sent.article_count > 0 ? (
                            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[9px] font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                              GPU Live
                            </span>
                          ) : (
                            <span className="px-1.5 py-0.5 rounded text-[9px] font-medium bg-slate-800 text-slate-400">
                              Quiet (24h)
                            </span>
                          )}
                          <span className={`px-2 py-0.5 rounded-md text-[10px] font-mono font-bold border ${scoreColor}`}>
                            {scoreSign}
                          </span>
                        </div>
                      </div>

                      {/* Sentiment Label & 3-Way Distribution Bar */}
                      <div className="space-y-1.5">
                        <div className="flex items-center justify-between text-[10px]">
                          <span className="text-slate-400">Conviction: <span className="font-semibold text-slate-200">{labelFormatted}</span></span>
                          <span className="text-slate-400 font-mono text-[9px]">
                            Bull: <span className="text-emerald-400">{posPct}%</span> | Neu: <span className="text-slate-300">{neuPct}%</span> | Bear: <span className="text-rose-400">{negPct}%</span>
                          </span>
                        </div>
                        {/* Segmented Distribution Bar */}
                        <div className="h-1.5 w-full bg-slate-800 rounded-full overflow-hidden flex">
                          <div style={{ width: `${posPct}%` }} className="bg-emerald-500 h-full transition-all duration-300" title={`Bullish: ${posPct}%`} />
                          <div style={{ width: `${neuPct}%` }} className="bg-slate-500 h-full transition-all duration-300" title={`Neutral: ${neuPct}%`} />
                          <div style={{ width: `${negPct}%` }} className="bg-rose-500 h-full transition-all duration-300" title={`Bearish: ${negPct}%`} />
                        </div>
                      </div>

                      {/* Narrative Alignment Banner */}
                      {alignment && alignment.state !== 'UNALIGNED' && (
                        <div className={`p-2 rounded-lg border text-[10px] space-y-0.5 ${
                          alignment.type === 'success' ? 'bg-emerald-950/30 border-emerald-800/40 text-emerald-300' :
                          alignment.type === 'danger' ? 'bg-rose-950/30 border-rose-800/40 text-rose-300' :
                          alignment.type === 'warning' ? 'bg-amber-950/30 border-amber-800/40 text-amber-300' :
                          alignment.type === 'info' ? 'bg-sky-950/30 border-sky-800/40 text-sky-300' :
                          'bg-slate-800/40 border-slate-700/50 text-slate-300'
                        }`}>
                          <div className="flex items-center gap-1 font-bold text-[9px] uppercase tracking-wider">
                            <Zap className="w-3 h-3 flex-shrink-0" />
                            {alignment.badge}
                          </div>
                          <p className="text-[10px] leading-relaxed opacity-90">{alignment.detail}</p>
                        </div>
                      )}

                      {/* Decomposed Headline Cards */}
                      {sent?.headlines && sent.headlines.length > 0 ? (
                        <div className="space-y-1.5 max-h-48 overflow-y-auto pr-1">
                          {sent.headlines.map((hl, hIdx) => {
                            const hlScore = hl.score;
                            const hlColor =
                              hl.sentiment === 'BULLISH' ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20' :
                              hl.sentiment === 'BEARISH' ? 'text-rose-400 bg-rose-500/10 border-rose-500/20' :
                              'text-slate-400 bg-slate-800/60 border-slate-700/40';

                            return (
                              <div key={hIdx} className="p-2 rounded-lg bg-slate-950/60 border border-slate-800/60 space-y-1">
                                <div className="flex items-center justify-between gap-1">
                                  <div className="flex items-center gap-1">
                                    {hl.is_regulatory && (
                                      <span className="px-1 py-0.2 rounded text-[8px] font-bold bg-purple-500/20 text-purple-300 border border-purple-500/30">
                                        BSE Filing
                                      </span>
                                    )}
                                    <span className="text-[9px] font-medium text-sky-400/90 font-mono">[{hl.source}]</span>
                                    <span className="text-[9px] text-slate-400 font-mono">• {hl.hours_ago}h ago</span>
                                  </div>
                                  <span className={`px-1.5 py-0.2 rounded text-[8px] font-mono font-bold border ${hlColor}`}>
                                    {hl.sentiment} ({hlScore > 0 ? `+${hlScore}` : hlScore})
                                  </span>
                                </div>
                                <p className="text-[10px] text-slate-300 leading-snug font-sans">{hl.title}</p>
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <p className="text-[11px] text-slate-400 leading-relaxed font-sans italic">
                          {activeAnalysis.commentary.latest_news || 'Quiet news cycle in the past 24h. No significant corporate catalysts detected.'}
                        </p>
                      )}

                      {/* Data Freshness & Ingestion Provenance */}
                      <div className="flex items-center justify-between pt-1 border-t border-slate-800/60 text-[9px] text-slate-400 font-mono">
                        <span className="flex items-center gap-1">
                          <Clock className="w-2.5 h-2.5 text-sky-400" />
                          Lookback: <span className="text-slate-300">24h Rolling</span>
                        </span>
                        <span>
                          {sent?.scanned_at ? (
                            <>Scanned: <span className="text-slate-300">{new Date(sent.scanned_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span></>
                          ) : (
                            <span className="text-slate-400">Live On-Demand</span>
                          )}
                        </span>
                      </div>
                    </div>
                  );
                })()}

                {/* [6] Universal Institutional Flow Engine (IFP) Card */}
                {(() => {
                  const detail = activeAnalysis.commentary.institutional_flow_detail;
                  const score = detail?.score ?? 0;
                  const isPos = score > 0;
                  const scoreSign = isPos ? `+${score}` : `${score}`;
                  const classification = detail?.classification || 'NEUTRAL';
                  const classLabel = classification.replace(/_/g, ' ');

                  const scoreColor =
                    score >= 50 ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30' :
                    score >= 20 ? 'text-teal-400 bg-teal-500/10 border-teal-500/30' :
                    score <= -50 ? 'text-rose-400 bg-rose-500/10 border-rose-500/30' :
                    score <= -20 ? 'text-amber-400 bg-amber-500/10 border-amber-500/30' :
                    'text-slate-300 bg-slate-800/80 border-slate-700';

                  return (
                    <div className="p-3 rounded-xl bg-slate-900/80 border border-slate-800/90 space-y-2.5 shadow-md">
                      {/* Top Header with IFP Badge & Confidence */}
                      <div className="flex items-center justify-between border-b border-slate-800/80 pb-2">
                        <div className="flex items-center gap-1.5">
                          <Building2 className="w-3.5 h-3.5 text-indigo-400" />
                          <span className="text-[11px] font-bold text-slate-200 uppercase tracking-wider">
                            Institutional Flow (IFP)
                          </span>
                        </div>
                        <div className="flex items-center gap-1.5">
                          {detail && (
                            <span className="text-[9px] font-mono text-slate-400 px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700">
                              {detail.confidence}% Conf • {detail.coverage}% Cov
                            </span>
                          )}
                          <span className={`px-2 py-0.5 rounded-md text-[10px] font-bold font-mono border ${scoreColor}`}>
                            {scoreSign} {classLabel}
                          </span>
                        </div>
                      </div>

                      {/* Stealth Regime Alert */}
                      {detail?.stealth_phase && (
                        <div className="px-2 py-1 rounded-lg bg-purple-500/15 border border-purple-500/30 flex items-center gap-1.5 text-[10px] text-purple-300 font-medium animate-pulse">
                          <Sparkles className="w-3 h-3 text-purple-400 flex-shrink-0" />
                          <span>
                            {detail.stealth_phase === 'STEALTH_ACCUMULATION'
                              ? '⚡ Stealth Accumulation: High institutional absorption without price breakout.'
                              : '⚠️ Stealth Distribution: Institutional supply offloading while price is contained.'}
                          </span>
                        </div>
                      )}

                      {/* Conflicts Alert */}
                      {detail?.conflicts && detail.conflicts.length > 0 && (
                        <div className="px-2 py-1 rounded-lg bg-amber-500/15 border border-amber-500/30 flex items-center gap-1.5 text-[10px] text-amber-300 font-medium">
                          <Shield className="w-3 h-3 text-amber-400 flex-shrink-0" />
                          <span>{detail.conflicts[0]}</span>
                        </div>
                      )}

                      {/* 7 Factor Components Grid */}
                      {detail?.components && (
                        <div className="grid grid-cols-4 gap-1 pt-0.5 font-mono text-[9px]">
                          {/* Vol */}
                          <div className="p-1 rounded bg-slate-950/60 border border-slate-800/80 text-center">
                            <div className="text-[8px] text-slate-500 font-sans">Volume</div>
                            <div className={`font-bold ${detail.components.volume !== null ? (detail.components.volume >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-slate-600'}`}>
                              {detail.components.volume !== null ? (detail.components.volume > 0 ? `+${detail.components.volume}` : detail.components.volume) : 'N/A'}
                            </div>
                          </div>
                          {/* CVD */}
                          <div className="p-1 rounded bg-slate-950/60 border border-slate-800/80 text-center">
                            <div className="text-[8px] text-slate-500 font-sans">CVD Delta</div>
                            <div className={`font-bold ${detail.components.cvd !== null ? (detail.components.cvd >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-slate-600'}`}>
                              {detail.components.cvd !== null ? (detail.components.cvd > 0 ? `+${detail.components.cvd}` : detail.components.cvd) : 'N/A'}
                            </div>
                          </div>
                          {/* Large Trades */}
                          <div className="p-1 rounded bg-slate-950/60 border border-slate-800/80 text-center">
                            <div className="text-[8px] text-slate-500 font-sans">Large Trades</div>
                            <div className={`font-bold ${detail.components.large_trades !== null ? (detail.components.large_trades >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-slate-600'}`}>
                              {detail.components.large_trades !== null ? (detail.components.large_trades > 0 ? `+${detail.components.large_trades}` : detail.components.large_trades) : 'N/A'}
                            </div>
                          </div>
                          {/* Depth */}
                          <div className="p-1 rounded bg-slate-950/60 border border-slate-800/80 text-center">
                            <div className="text-[8px] text-slate-500 font-sans">Depth/Absorp</div>
                            <div className={`font-bold ${detail.components.depth !== null ? (detail.components.depth >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-slate-600'}`}>
                              {detail.components.depth !== null ? (detail.components.depth > 0 ? `+${detail.components.depth}` : detail.components.depth) : 'N/A'}
                            </div>
                          </div>
                          {/* Price/AVWAP */}
                          <div className="p-1 rounded bg-slate-950/60 border border-slate-800/80 text-center">
                            <div className="text-[8px] text-slate-500 font-sans">AVWAP Shelf</div>
                            <div className={`font-bold ${detail.components.price_acceptance !== null ? (detail.components.price_acceptance >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-slate-600'}`}>
                              {detail.components.price_acceptance !== null ? (detail.components.price_acceptance > 0 ? `+${detail.components.price_acceptance}` : detail.components.price_acceptance) : 'N/A'}
                            </div>
                          </div>
                          {/* F&O OI */}
                          <div className="p-1 rounded bg-slate-950/60 border border-slate-800/80 text-center">
                            <div className="text-[8px] text-slate-500 font-sans">F&O OI</div>
                            <div className={`font-bold ${detail.components.fno_oi !== null ? (detail.components.fno_oi >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-slate-500 italic'}`}>
                              {detail.components.fno_oi !== null ? (detail.components.fno_oi > 0 ? `+${detail.components.fno_oi}` : detail.components.fno_oi) : 'Non-F&O'}
                            </div>
                          </div>
                          {/* 5D / 20D Trend */}
                          <div className="p-1 rounded bg-slate-950/60 border border-slate-800/80 text-center col-span-2">
                            <div className="text-[8px] text-slate-500 font-sans">5D / 20D Trend</div>
                            <div className="font-bold text-cyan-300">
                              {detail.trend ? `${detail.trend.trend_5d > 0 ? `+${detail.trend.trend_5d}` : detail.trend.trend_5d} (5D) • ${Math.round(detail.trend.persistence_10d * 100)}% Persist` : 'Normal'}
                            </div>
                          </div>
                        </div>
                      )}

                      {/* Executive Narrative */}
                      <p className="text-[11px] text-slate-300 leading-relaxed font-sans pt-0.5">
                        {activeAnalysis.commentary.institutional_flow || 'Neutral / Balanced Flow'}
                      </p>
                    </div>
                  );
                })()}
              </div>
            </>
          ) : (
            <div className="p-8 text-center text-slate-500 text-xs">
              Select a stock from the ranked list to view detailed technical breakdown.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
