import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Shield,
  Target,
  Clock,
  TrendingDown,
  Activity,
  Trash2,
  Play,
  Pause,
  RefreshCw,
  ExternalLink,
  Flame,
  AlertTriangle,
  Plus,
  Search,
  ChevronDown,
  ChevronUp,
  Check,
  X,
  Zap,
  Wallet,
  Settings,
  PowerOff,
  Sliders,
  ArrowDownCircle,
} from 'lucide-react';
import {
  getShortlistShorts,
  pollShortlistShortsSurveillance,
  getShortlistShortsTimeline,
  deleteShortlistShortsTrade,
  clearShortlistShorts,
  getAnalysisSymbols,
  searchNseSymbols,
  getSymbolAnalysis,
  addManualShortlistShortsTrade,
  getCapitalSlotsSummary,
  getWatchdogPositions,
  approveWatchdogShortsTrade,
  declineWatchdogShortsTrade,
  emergencyKillAllShortsPositions,
  getWatchdogSettings,
  updateWatchdogSettings,
  toggleExecutionSwitch,
  WatchdogSettings,
  DepthIntelligenceTelemetry,
  AddManualTradePayload,
  AnalysisSymbolItem,
  ShortlistedTrade,
  ShortlistTimelineEvent,
  CapitalSlotsSummary,
  WatchdogPositionItem,
} from '../api';
import { loadAnalysisCache } from '../utils/analysisStorage';
import { formatISTTime } from '../utils/timeFormat';

interface ShortlistedShortsPageProps {
  token?: string;
  onOpenChart?: (symbol: string) => void;
}

export function ShortlistedShortsPage({ token: _token, onOpenChart }: ShortlistedShortsPageProps) {
  // ── 1. Data & Real-Time Surveillance State for Shorts ──────────────────────
  const [trades, setTrades] = useState<ShortlistedTrade[]>([]);
  const [events, setEvents] = useState<ShortlistTimelineEvent[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [isSurveillanceActive, setIsSurveillanceActive] = useState<boolean>(true);
  const [lastEvaluatedAt, setLastEvaluatedAt] = useState<string | null>(null);
  const [timelineFilter, setTimelineFilter] = useState<'all' | 'triggers' | 'targets' | 'risk'>('all');

  // Watchdog & Shared Capital Slot State (₹1.80L total pool shared with Longs)
  const [slotsSummary, setSlotsSummary] = useState<CapitalSlotsSummary | null>(null);
  const [watchdogPositions, setWatchdogPositions] = useState<WatchdogPositionItem[]>([]);
  const [actionLoading, setActionLoading] = useState<Record<string, boolean>>({});

  // ── 1B. Watchdog Settings & Master Execution Switch State (Shared) ──────────
  const [settings, setSettings] = useState<WatchdogSettings | null>(null);
  const [depthIntelligence, setDepthIntelligence] = useState<DepthIntelligenceTelemetry | null>(null);
  const [isSettingsOpen, setIsSettingsOpen] = useState<boolean>(false);
  const [isTogglingExecution, setIsTogglingExecution] = useState<boolean>(false);
  const [isSavingSettings, setIsSavingSettings] = useState<boolean>(false);

  // Form states for Settings Modal
  const [formTotalCapital, setFormTotalCapital] = useState<string>('180000');
  const [formPerTrade, setFormPerTrade] = useState<string>('60000');
  const [formMaxSlots, setFormMaxSlots] = useState<string>('3');
  const [formMaxSLPercent, setFormMaxSLPercent] = useState<string>('1.0');
  const [formBreakevenPercent, setFormBreakevenPercent] = useState<string>('0.8');
  const [formTrailPercent, setFormTrailPercent] = useState<string>('1.5');
  const [formPurgePercent, setFormPurgePercent] = useState<string>('0.20');

  // Manual Add Stock state
  const [universeSymbols, setUniverseSymbols] = useState<AnalysisSymbolItem[]>([]);
  const [manualSymbol, setManualSymbol] = useState<string>('');
  const [isAddingManual, setIsAddingManual] = useState<boolean>(false);
  const [showCustomLevels, setShowCustomLevels] = useState<boolean>(false);
  const [customEntry, setCustomEntry] = useState<string>('');
  const [customSL, setCustomSL] = useState<string>('');
  const [customQty, setCustomQty] = useState<string>('25');
  const [customT1, setCustomT1] = useState<string>('');
  const [customT2, setCustomT2] = useState<string>('');
  const [isManualModalOpen, setIsManualModalOpen] = useState<boolean>(false);
  const [symbolSuggestionsOpen, setSymbolSuggestionsOpen] = useState<boolean>(false);
  const [modalSuggestionsOpen, setModalSuggestionsOpen] = useState<boolean>(false);
  const [nseSearchResults, setNseSearchResults] = useState<AnalysisSymbolItem[]>([]);
  const [isNseSearching, setIsNseSearching] = useState<boolean>(false);
  const nseSearchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const searchContainerRef = useRef<HTMLDivElement>(null);

  // ── 2. Initial Data Fetch ──────────────────────────────────────────────────
  const loadInitialData = useCallback(async () => {
    setIsLoading(true);
    setErrorMsg(null);
    try {
      const [tradeRes, eventRes, slotRes, posRes, settingRes] = await Promise.all([
        getShortlistShorts().catch(() => null),
        getShortlistShortsTimeline(undefined, 80).catch(() => null),
        getCapitalSlotsSummary().catch(() => null),
        getWatchdogPositions().catch(() => null),
        getWatchdogSettings().catch(() => null),
      ]);
      if (tradeRes && tradeRes.trades) {
        setTrades(tradeRes.trades);
      }
      if (eventRes && eventRes.events) {
        setEvents(eventRes.events);
      }
      if (slotRes) {
        setSlotsSummary(slotRes);
      }
      if (posRes && posRes.positions) {
        // Filter to display only active short positions on the shorts desk
        setWatchdogPositions(posRes.positions.filter((p: any) => (p.action || '').toUpperCase() === 'SELL'));
      }
      if (settingRes && settingRes.settings) {
        setSettings(settingRes.settings);
        if (settingRes.depth_intelligence) {
          setDepthIntelligence(settingRes.depth_intelligence);
        }
        setFormTotalCapital(String(settingRes.settings.total_capital));
        setFormPerTrade(String(settingRes.settings.per_trade_allocation));
        setFormMaxSlots(String(settingRes.settings.max_active_trades));
        setFormMaxSLPercent(String(settingRes.settings.max_hard_stop_loss_percent));
        setFormBreakevenPercent(String(settingRes.settings.breakeven_trigger_percent));
        setFormTrailPercent(String(settingRes.settings.structural_trail_percent));
        setFormPurgePercent(String(settingRes.settings.purge_slip_tolerance_percent));
      }
    } catch (err) {
      console.error('Failed to load initial shorts tracking data:', err);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadInitialData();
  }, [loadInitialData]);

  // Load universe symbols for manual search & autocomplete
  useEffect(() => {
    getAnalysisSymbols('all')
      .then((res) => {
        if (res && res.symbols) {
          setUniverseSymbols(res.symbols);
        }
      })
      .catch((err) => console.debug('Failed to load universe symbols:', err));
  }, []);

  // Close suggestions dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (searchContainerRef.current && !searchContainerRef.current.contains(e.target as Node)) {
        setSymbolSuggestionsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Combined local universe + full NSE search suggestions
  const localSuggestions = useMemo(() => {
    const q = manualSymbol.trim().toUpperCase();
    if (!q) return [];
    return universeSymbols
      .filter((s) => s.symbol.toUpperCase().includes(q) || (s.name && s.name.toUpperCase().includes(q)))
      .slice(0, 8);
  }, [universeSymbols, manualSymbol]);

  // Debounced NSE-wide search when the user types — searches ALL NSE stocks
  useEffect(() => {
    const q = manualSymbol.trim();
    if (q.length < 2) {
      setNseSearchResults([]);
      return;
    }
    if (nseSearchTimerRef.current) clearTimeout(nseSearchTimerRef.current);
    nseSearchTimerRef.current = setTimeout(async () => {
      setIsNseSearching(true);
      try {
        const res = await searchNseSymbols(q, 15);
        if (res?.symbols) setNseSearchResults(res.symbols);
      } catch (_e) {
        // silently fail — local suggestions still work
      } finally {
        setIsNseSearching(false);
      }
    }, 300);
    return () => { if (nseSearchTimerRef.current) clearTimeout(nseSearchTimerRef.current); };
  }, [manualSymbol]);

  // Merge local + NSE results, deduplicating by symbol, with local taking precedence
  const symbolSuggestions = useMemo(() => {
    const localSymbolSet = new Set(localSuggestions.map((s) => s.symbol.toUpperCase()));
    const merged = [...localSuggestions];
    for (const nseItem of nseSearchResults) {
      if (!localSymbolSet.has(nseItem.symbol.toUpperCase())) {
        merged.push({ ...nseItem, segment: 'NSE_ALL' }); // tag to differentiate in UI
      }
    }
    return merged.slice(0, 15);
  }, [localSuggestions, nseSearchResults]);

  const handleOpenManualModal = async (symbolToOpen?: string) => {
    const sym = (symbolToOpen || manualSymbol || '').trim().toUpperCase();
    setSymbolSuggestionsOpen(false);
    setErrorMsg(null);
    let cachedPrice = 0;
    let commentary: any = null;
    if (sym) {
      try {
        const cache = await loadAnalysisCache();
        if (cache?.stocks?.[sym]) {
          const s = cache.stocks[sym];
          cachedPrice = parseFloat(s.current_price ?? s.price ?? 0);
          commentary = s.commentary;
        }
      } catch (_e) {}

      if (cachedPrice <= 0) {
        try {
          const liveRes = await getSymbolAnalysis(sym);
          if (liveRes) {
            const fetchedPx = parseFloat(String(liveRes.current_price ?? (liveRes as any).price ?? 0));
            if (fetchedPx > 0) {
              cachedPrice = fetchedPx;
              commentary = liveRes.commentary;
            }
          }
        } catch (_e) {}
      }
    }

    const entryPx = cachedPrice > 0 ? cachedPrice : (parseFloat(customEntry) || 100);
    // For shorts: SL is ABOVE entry (+1.0%), T1 is BELOW entry (-3.0%), T2 is BELOW (-6.0%)
    // Only use commentary levels if they conform to short geometry (commentary defaults are usually for longs)
    const sl = (commentary?.stop_loss && parseFloat(commentary.stop_loss) > entryPx)
      ? parseFloat(commentary.stop_loss)
      : Number((entryPx * 1.01).toFixed(2));
    const t1 = (commentary?.target_price_1 && parseFloat(commentary.target_price_1) < entryPx)
      ? parseFloat(commentary.target_price_1)
      : Number((entryPx * 0.97).toFixed(2));
    const t2 = (commentary?.target_price_2 && parseFloat(commentary.target_price_2) < t1)
      ? parseFloat(commentary.target_price_2)
      : Number((entryPx * 0.94).toFixed(2));
    const defaultQty = entryPx > 0 ? Math.max(1, Math.floor(60000 / entryPx)) : 25;

    if (sym) setManualSymbol(sym);
    setCustomEntry(entryPx.toFixed(2));
    setCustomSL(sl.toFixed(2));
    setCustomT1(t1.toFixed(2));
    setCustomT2(t2.toFixed(2));
    setCustomQty(String(defaultQty));
    setIsManualModalOpen(true);
  };

  const handleConfirmAddManualTrade = async () => {
    const sym = manualSymbol.trim().toUpperCase();
    if (!sym) {
      setErrorMsg('Please enter a valid stock symbol.');
      return;
    }
    const entryPx = parseFloat(customEntry);
    let sl = parseFloat(customSL);
    let t1 = customT1 ? parseFloat(customT1) : NaN;
    let t2 = customT2 ? parseFloat(customT2) : NaN;
    const qty = parseInt(customQty, 10);

    if (isNaN(entryPx) || entryPx <= 0) {
      setErrorMsg('Please enter a valid Short Entry Price greater than 0.');
      return;
    }
    if (isNaN(sl) || sl <= 0) {
      setErrorMsg('Please enter a valid Stop Loss greater than 0.');
      return;
    }
    if (sl <= entryPx) {
      setErrorMsg('For SHORT (SELL) positions, Stop Loss must be strictly above Entry Price.');
      return;
    }
    if (isNaN(qty) || qty <= 0) {
      setErrorMsg('Please enter a valid Quantity of at least 1.');
      return;
    }

    // Auto-calculate or sanitize targets if missing or inverted
    if (isNaN(t1) || t1 >= entryPx) {
      t1 = Number((entryPx * 0.97).toFixed(2));
    }
    if (isNaN(t2) || t2 >= t1) {
      t2 = Number((entryPx * 0.94).toFixed(2));
    }

    setIsAddingManual(true);
    setErrorMsg(null);
    try {
      const payload: AddManualTradePayload = {
        symbol: sym,
        action: 'SELL',
        current_price: entryPx,
        entry_price: entryPx,
        stop_loss: sl,
        target_1: t1,
        target_2: t2,
        quantity: qty,
        setup_type: 'MANUAL_SHORT_CONVICTION',
        thesis: `Manual short position monitored by Live Watchdog (Advisory Only). Entry: ₹${entryPx}, SL: ₹${sl}, Qty: ${qty}.`,
      };

      const res = await addManualShortlistShortsTrade(payload);
      if (res && res.trade) {
        setTrades((prev) => {
          const exists = prev.some((t) => t.symbol === res.trade.symbol);
          if (exists) {
            return prev.map((t) => (t.symbol === res.trade.symbol ? res.trade : t));
          }
          return [res.trade, ...prev];
        });

        const [evRes, slotRes, posRes] = await Promise.all([
          getShortlistShortsTimeline(undefined, 80).catch(() => null),
          getCapitalSlotsSummary().catch(() => null),
          getWatchdogPositions().catch(() => null),
        ]);
        if (evRes?.events) setEvents(evRes.events);
        if (slotRes) setSlotsSummary(slotRes);
        if (posRes?.positions) {
          setWatchdogPositions(posRes.positions.filter((p: any) => (p.action || '').toUpperCase() === 'SELL'));
        }

        setIsManualModalOpen(false);
        setManualSymbol('');
      }
    } catch (err: any) {
      console.error('Failed to add manual short trade:', err);
      setErrorMsg(err?.response?.data?.detail || err.message || 'Failed to add manual short trade.');
    } finally {
      setIsAddingManual(false);
    }
  };

  // ── 3. Real-Time Surveillance & Watchdog Polling (2.5s Heartbeat) ───────────
  useEffect(() => {
    const settingInterval = setInterval(() => {
      getWatchdogSettings().then((res) => {
        if (res?.settings) {
          setSettings(res.settings);
          if (res.depth_intelligence) setDepthIntelligence(res.depth_intelligence);
        }
      }).catch(() => null);
    }, 12000);

    return () => clearInterval(settingInterval);
  }, []);

  useEffect(() => {
    if (!isSurveillanceActive) return;

    const interval = setInterval(async () => {
      try {
        const [survRes, slotRes, posRes] = await Promise.all([
          pollShortlistShortsSurveillance().catch(() => null),
          getCapitalSlotsSummary().catch(() => null),
          getWatchdogPositions().catch(() => null),
        ]);

        if (survRes && survRes.status === 'ok') {
          if (survRes.trades) {
            setTrades(survRes.trades);
          }
          if (survRes.new_events && survRes.new_events.length > 0) {
            setEvents((prev) => {
              const existingIds = new Set(prev.map((e) => e.id));
              const fresh = survRes.new_events.filter((e) => !existingIds.has(e.id));
              return [...fresh, ...prev].slice(0, 100);
            });
          }
          setLastEvaluatedAt(formatISTTime(new Date(), true));
        }

        if (slotRes) setSlotsSummary(slotRes);
        if (posRes && posRes.positions) {
          setWatchdogPositions(posRes.positions.filter((p: any) => (p.action || '').toUpperCase() === 'SELL'));
        }
      } catch (err) {
        console.debug('Shorts surveillance poll error:', err);
      }
    }, 2500);

    return () => clearInterval(interval);
  }, [isSurveillanceActive]);

  // ── 4. Action Handlers ─────────────────────────────────────────────────────
  const handleApprove = async (trade: ShortlistedTrade) => {
    setActionLoading((prev) => ({ ...prev, [trade.symbol]: true }));
    setErrorMsg(null);
    try {
      const res = await approveWatchdogShortsTrade(
        trade.symbol,
        trade.entry_price,
        (trade as any).security_id || '',
        (trade as any).quantity
      );
      if (res && res.status === 'BLOCKED') {
        setErrorMsg(`⚠️ Order Blocked: ${res.message}`);
      } else {
        const [posRes, slotRes] = await Promise.all([
          getWatchdogPositions().catch(() => null),
          getCapitalSlotsSummary().catch(() => null),
        ]);
        if (posRes?.positions) {
          setWatchdogPositions(posRes.positions.filter((p: any) => (p.action || '').toUpperCase() === 'SELL'));
        }
        if (slotRes) setSlotsSummary(slotRes);
      }
    } catch (err: any) {
      console.error('Failed to approve short trade:', err);
      setErrorMsg(err?.response?.data?.detail || err.message || 'Execution failed.');
    } finally {
      setActionLoading((prev) => ({ ...prev, [trade.symbol]: false }));
    }
  };

  const handleDecline = async (symbol: string) => {
    setActionLoading((prev) => ({ ...prev, [symbol]: true }));
    try {
      await declineWatchdogShortsTrade(symbol);
      setTrades((prev) => prev.filter((t) => t.symbol !== symbol));
      setWatchdogPositions((prev) => prev.filter((p) => p.symbol !== symbol));
      const slotRes = await getCapitalSlotsSummary().catch(() => null);
      if (slotRes) setSlotsSummary(slotRes);
    } catch (err: any) {
      console.error('Failed to decline short trade:', err);
    } finally {
      setActionLoading((prev) => ({ ...prev, [symbol]: false }));
    }
  };

  const handleDeleteTrade = async (symbol: string) => {
    try {
      await deleteShortlistShortsTrade(symbol);
      setTrades((prev) => prev.filter((t) => t.symbol !== symbol));
      const [slotRes, posRes] = await Promise.all([
        getCapitalSlotsSummary().catch(() => null),
        getWatchdogPositions().catch(() => null),
      ]);
      if (slotRes) setSlotsSummary(slotRes);
      if (posRes?.positions) {
        setWatchdogPositions(posRes.positions.filter((p: any) => (p.action || '').toUpperCase() === 'SELL'));
      }
    } catch (err) {
      console.error('Failed to delete short trade:', err);
    }
  };

  const handleEmergencyKillAll = async () => {
    if (!window.confirm('🚨 EMERGENCY KILL-SWITCH: Are you sure you want to market cover and exit ALL active short positions?')) {
      return;
    }
    try {
      await emergencyKillAllShortsPositions();
      setWatchdogPositions([]);
      const slotRes = await getCapitalSlotsSummary().catch(() => null);
      if (slotRes) setSlotsSummary(slotRes);
    } catch (err: any) {
      console.error('Failed emergency kill all shorts:', err);
      setErrorMsg('Emergency kill-switch failed. Please check broker terminal.');
    }
  };

  const handleToggleExecution = async () => {
    setIsTogglingExecution(true);
    try {
      const res = await toggleExecutionSwitch();
      if (res && res.status === 'ok') {
        setSettings((prev) => (prev ? { ...prev, execution_enabled: res.execution_enabled } : null));
      }
    } catch (err: any) {
      console.error('Failed to toggle master execution switch:', err);
    } finally {
      setIsTogglingExecution(false);
    }
  };

  const handleSaveSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSavingSettings(true);
    try {
      const updates = {
        total_capital: parseFloat(formTotalCapital) || 180000,
        per_trade_allocation: parseFloat(formPerTrade) || 60000,
        max_active_trades: parseInt(formMaxSlots, 10) || 3,
        max_hard_stop_loss_percent: parseFloat(formMaxSLPercent) || 1.0,
        breakeven_trigger_percent: parseFloat(formBreakevenPercent) || 0.8,
        structural_trail_percent: parseFloat(formTrailPercent) || 1.5,
        purge_slip_tolerance_percent: parseFloat(formPurgePercent) || 0.20,
      };
      const res = await updateWatchdogSettings(updates);
      if (res && res.settings) {
        setSettings(res.settings);
        setIsSettingsOpen(false);
      }
    } catch (err: any) {
      console.error('Failed to save watchdog settings:', err);
    } finally {
      setIsSavingSettings(false);
    }
  };

  const handleClearShortlist = async () => {
    if (!window.confirm('Clear all tracked short setups and timeline history?')) return;
    try {
      await clearShortlistShorts();
      setTrades([]);
      setEvents([]);
    } catch (err) {
      console.error('Failed to clear shorts live tracking:', err);
    }
  };

  // ── 5. Filtered Timeline Events ────────────────────────────────────────────
  const filteredEvents = useMemo(() => {
    if (timelineFilter === 'all') return events;
    if (timelineFilter === 'triggers') {
      return events.filter((e) => ['ENTRY_TRIGGERED', 'CONFIRMED_TRIGGER', 'SUSTAINED_BREAKDOWN', 'MANUAL_TRADE_ARMED', 'HUGE_SELLOFF_ALERT'].includes(e.event_type));
    }
    if (timelineFilter === 'targets') {
      return events.filter((e) => ['TP1_HIT', 'TP2_HIT', 'TAKE_PROFIT'].includes(e.event_type));
    }
    if (timelineFilter === 'risk') {
      return events.filter((e) => ['SL_HIT', 'STOPPED_OUT', 'MANUAL_ADVISORY_SL', 'MANUAL_ADVISORY_EXIT', 'HARD_SL_HIT', 'TRAILED_STOP_HIT', 'INSTITUTIONAL_INFLOW', 'MASSIVE_BUY_WALL', 'MASSIVE_SELL_WALL', 'ORDERBOOK_SHIFT', 'AUTO_SL_MODIFIED'].includes(e.event_type));
    }
    return events;
  }, [events, timelineFilter]);

  return (
    <div className="flex-1 flex flex-col min-h-0 bg-slate-950 text-slate-100 font-sans overflow-hidden">
      {/* ── Top Header & Surveillance Status HUD ───────────────────────────── */}
      <header className="h-12 px-4 border-b border-rose-950/80 bg-slate-900/80 flex items-center justify-between shrink-0 shadow-sm shadow-rose-950/30">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-rose-500/20 via-orange-500/20 to-amber-500/20 border border-rose-500/40 flex items-center justify-center text-rose-400 shadow-sm shadow-rose-950/50">
            <TrendingDown className="w-4 h-4" />
          </div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-sm font-black text-white tracking-tight">Live Trade Desk — SHORTS</h1>
            <span className="text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full bg-rose-950/80 text-rose-300 border border-rose-700/50 flex items-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${isSurveillanceActive ? 'bg-rose-400 animate-pulse' : 'bg-slate-500'}`} />
              {isSurveillanceActive ? 'Surveillance Active (3s)' : 'Surveillance Paused'}
            </span>
            <span className="text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full bg-rose-950/80 text-rose-300 border border-rose-700/50">
              🔴 SELL ONLY
            </span>
          </div>
        </div>

        {/* Action Controls */}
        <div className="flex items-center gap-2">
          {/* Master Execution Switch Toggle */}
          <button
            type="button"
            onClick={handleToggleExecution}
            disabled={isTogglingExecution}
            className={`px-3 py-1 rounded-full border text-[11px] font-bold tracking-tight transition-all flex items-center gap-1.5 shadow-sm active:scale-95 cursor-pointer disabled:opacity-50 ${
              settings?.execution_enabled !== false
                ? 'bg-rose-500/20 text-rose-300 border-rose-500/60 hover:bg-rose-500/30'
                : 'bg-amber-500/20 text-amber-300 border-amber-500/60 hover:bg-amber-500/30 ring-2 ring-amber-400/40 animate-pulse'
            }`}
            title={
              settings?.execution_enabled !== false
                ? 'Execution Engine is ARMED (ON). Click to turn OFF before Telegram & MCP.'
                : 'Execution Engine is STANDBY (OFF). Telegram alerts & MCP orders are blocked. Click to turn ON.'
            }
          >
            <span
              className={`w-2 h-2 rounded-full ${
                settings?.execution_enabled !== false ? 'bg-rose-400' : 'bg-amber-400'
              }`}
            />
            {settings?.execution_enabled !== false ? '⚡ Execution: ON' : '🛡️ Execution: OFF'}
          </button>

          {lastEvaluatedAt && (
            <span className="text-[10px] text-slate-400 hidden lg:inline mr-1">
              Surveillance Tick: <strong className="text-slate-200">{lastEvaluatedAt} IST</strong>
            </span>
          )}

          {/* Pause / Resume Surveillance */}
          <button
            onClick={() => setIsSurveillanceActive((prev) => !prev)}
            className={`p-1.5 rounded-lg border text-xs font-semibold transition-all flex items-center gap-1 ${
              isSurveillanceActive
                ? 'bg-slate-800/90 text-slate-300 border-slate-700 hover:bg-slate-700'
                : 'bg-amber-500/20 text-amber-300 border-amber-500/40'
            }`}
            title={isSurveillanceActive ? 'Pause Real-Time Surveillance' : 'Resume Real-Time Surveillance'}
          >
            {isSurveillanceActive ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
          </button>

          {/* Clear Shortlist Button */}
          {(trades.length > 0 || events.length > 0) && (
            <button
              onClick={handleClearShortlist}
              className="px-2.5 py-1.5 rounded-lg bg-rose-500/15 hover:bg-rose-500/25 text-rose-300 hover:text-rose-200 border border-rose-500/40 font-semibold text-xs transition-all flex items-center gap-1.5"
              title="Clear all tracked short setups and surveillance history"
            >
              <Trash2 className="w-3.5 h-3.5 text-rose-400" />
              <span>Clear All</span>
            </button>
          )}
        </div>
      </header>

      {/* ── Institutional Capital & Concurrency HUD (Shared ₹1.80L Pool) ── */}
      <div className="border-b border-slate-800 bg-slate-900/60 px-4 py-2.5 shrink-0 flex flex-wrap items-center justify-between gap-3 text-xs">
        {/* Left: Capital Metrics */}
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-rose-500/15 border border-rose-500/30 flex items-center justify-center text-rose-400">
              <Wallet className="w-3.5 h-3.5" />
            </div>
            <div>
              <div className="text-[10px] uppercase font-bold text-slate-400 leading-tight">Shared Desk Capital</div>
              <div className="font-mono font-black text-white text-xs">
                ₹{(slotsSummary?.total_capital || settings?.total_capital || 180000).toLocaleString('en-IN')}
                <span className="text-[10px] font-normal text-slate-400 ml-1.5">
                  (₹{((settings?.per_trade_allocation || 60000) / 1000).toFixed(0)}k/trade • Max {settings?.max_active_trades || 3} Slots Total)
                </span>
              </div>
            </div>
          </div>

          <div className="hidden sm:block h-6 w-[1px] bg-slate-800" />

          <div className="flex items-center gap-3">
            <div>
              <div className="text-[10px] uppercase font-semibold text-slate-400">Deployed (All)</div>
              <div className="font-mono font-bold text-orange-400">
                ₹{(slotsSummary?.deployed_capital || 0).toLocaleString('en-IN')}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase font-semibold text-slate-400">Available</div>
              <div className="font-mono font-bold text-emerald-400">
                ₹{(slotsSummary?.available_capital || settings?.total_capital || 180000).toLocaleString('en-IN')}
              </div>
            </div>
          </div>
        </div>

        {/* Center: Concurrent Slot Badges (Shared 3 slots) */}
        <div className="flex items-center gap-2">
          {Array.from({ length: settings?.max_active_trades || 3 }).map((_, slotIdx) => {
            const activePos = watchdogPositions[slotIdx];
            return (
              <div
                key={slotIdx}
                className={`px-2.5 py-1 rounded-lg border flex items-center gap-1.5 transition-all ${
                  activePos
                    ? 'bg-rose-950/80 border-rose-500/60 text-rose-200 shadow-sm shadow-rose-950'
                    : 'bg-slate-950/60 border-slate-800 text-slate-500'
                }`}
              >
                <span
                  className={`w-2 h-2 rounded-full ${
                    activePos ? 'bg-rose-400 animate-pulse' : 'bg-slate-700'
                  }`}
                />
                <span className="text-[10px] font-mono font-black uppercase">
                  Slot {slotIdx + 1}: {activePos ? activePos.symbol : 'EMPTY'}
                </span>
                {activePos && (
                  <span
                    className={`text-[9px] font-mono font-bold px-1.5 py-0.5 rounded border ${
                      (activePos.depth_level_active || '20-level') === '200-level'
                        ? 'bg-purple-900/60 text-purple-200 border-purple-500/50 shadow-[0_0_8px_rgba(168,85,247,0.3)]'
                        : 'bg-rose-900/60 text-rose-200 border-rose-500/40'
                    }`}
                    title={`Microstructure Feed: ${activePos.depth_level_active || '20-level'}`}
                  >
                    {activePos.depth_level_active || '20-level'}
                  </span>
                )}
                {activePos && (
                  <span className={`text-[10px] font-mono font-bold ${
                    (activePos.pnl_pct || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'
                  }`}>
                    {(activePos.pnl_pct || 0) >= 0 ? '+' : ''}{(activePos.pnl_pct || 0).toFixed(1)}%
                  </span>
                )}
              </div>
            );
          })}
        </div>

        {/* Right: Settings & Emergency Kill All */}
        <div className="flex items-center gap-2">
          {slotsSummary?.priority_queue && slotsSummary.priority_queue.length > 0 && (
            <div className="px-2.5 py-1 rounded-lg bg-amber-500/20 border border-amber-500/40 text-amber-300 text-[10px] font-bold flex items-center gap-1">
              <Zap className="w-3 h-3 text-amber-400 animate-bounce" />
              <span>Queue: {slotsSummary.priority_queue.length} Pending</span>
            </div>
          )}

          {/* Watchdog Settings Button */}
          <button
            type="button"
            onClick={async () => {
              try {
                const fresh = await getWatchdogSettings();
                if (fresh?.settings) {
                  setSettings(fresh.settings);
                  if (fresh.depth_intelligence) {
                    setDepthIntelligence(fresh.depth_intelligence);
                  }
                  setFormTotalCapital(String(fresh.settings.total_capital));
                  setFormPerTrade(String(fresh.settings.per_trade_allocation));
                  setFormMaxSlots(String(fresh.settings.max_active_trades));
                  setFormMaxSLPercent(String(fresh.settings.max_hard_stop_loss_percent));
                  setFormBreakevenPercent(String(fresh.settings.breakeven_trigger_percent));
                  setFormTrailPercent(String(fresh.settings.structural_trail_percent));
                  setFormPurgePercent(String(fresh.settings.purge_slip_tolerance_percent));
                }
              } catch (_e) {
                if (settings) {
                  setFormTotalCapital(String(settings.total_capital));
                  setFormPerTrade(String(settings.per_trade_allocation));
                  setFormMaxSlots(String(settings.max_active_trades));
                  setFormMaxSLPercent(String(settings.max_hard_stop_loss_percent));
                  setFormBreakevenPercent(String(settings.breakeven_trigger_percent));
                  setFormTrailPercent(String(settings.structural_trail_percent));
                  setFormPurgePercent(String(settings.purge_slip_tolerance_percent));
                }
              }
              setIsSettingsOpen(true);
            }}
            className="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 font-bold text-[11px] shadow-sm transition-all flex items-center gap-1.5 active:scale-95 cursor-pointer"
            title="Edit Shared Capital Settings & Risk Boundaries"
          >
            <Settings className="w-3.5 h-3.5 text-rose-400" />
            <span>Settings</span>
          </button>

          <button
            onClick={handleEmergencyKillAll}
            disabled={watchdogPositions.length === 0}
            className="px-3 py-1 rounded-lg bg-rose-600/80 hover:bg-rose-500 text-white border border-rose-400/60 font-black text-[11px] shadow-md shadow-rose-950/60 active:scale-95 transition-all flex items-center gap-1.5 disabled:opacity-30 disabled:cursor-not-allowed"
            title="Emergency Market Cover exit for all active short positions"
          >
            <AlertTriangle className="w-3.5 h-3.5" />
            <span>KILL ALL SHORTS</span>
          </button>
        </div>
      </div>

      {/* ── Master Execution Switch OFF Standby Banner ── */}
      {settings?.execution_enabled === false && (
        <div className="bg-gradient-to-r from-amber-950/90 via-slate-900 to-amber-950/90 border-b border-amber-500/60 px-4 py-2 text-xs flex items-center justify-between text-amber-200 shrink-0">
          <div className="flex items-center gap-2.5">
            <PowerOff className="w-4 h-4 text-amber-400 shrink-0 animate-pulse" />
            <div className="leading-snug">
              <strong className="text-white uppercase tracking-wider mr-1">Master Execution Switch is OFF:</strong>
              Breakdown setups remain in observation mode. <strong>Telegram alerts</strong> and <strong>Dhan MCP short orders</strong> are blocked before placement.
            </div>
          </div>
          <button
            onClick={handleToggleExecution}
            disabled={isTogglingExecution}
            className="px-3 py-1 rounded-md bg-amber-500 hover:bg-amber-400 text-black font-black text-xs shrink-0 shadow-md shadow-amber-950/50 active:scale-95 transition-all ml-3"
          >
            Turn ON Execution
          </button>
        </div>
      )}

      {/* Error alert */}
      {errorMsg && (
        <div className="bg-rose-950/60 border-b border-rose-500/40 px-4 py-2 text-rose-200 text-xs flex items-center justify-between">
          <div className="flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0" />
            <span>{errorMsg}</span>
          </div>
          <button
            onClick={() => setErrorMsg(null)}
            className="text-xs text-rose-400 hover:text-white font-bold"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* ── Main 2-Section Workspace: Top Setup Cards + Chronological Timeline ── */}
      <div className="flex-1 flex flex-col md:flex-row min-h-0 overflow-hidden">
        {/* ── LEFT / UPPER: Monitored Short Setups Cards (60% width on Desktop) ── */}
        <div className="flex-1 flex flex-col min-h-0 border-r border-slate-800/80 bg-slate-950/50 overflow-y-auto custom-scrollbar p-4 space-y-4">
          <div className="flex items-center justify-between shrink-0 pb-1">
            <div className="flex items-center gap-2">
              <Flame className="w-4 h-4 text-rose-400" />
              <h2 className="text-xs font-bold text-slate-200 uppercase tracking-wider">
                Active Tracked Short Setups ({trades.length} Monitored)
              </h2>
            </div>
            <span className="text-[11px] text-slate-400">
              Live Breakdown Surveillance • Hard Ceiling SL & Trailing Resistance
            </span>
          </div>

          {/* ── Manual Search & Add Short Stock Toolbar ── */}
          <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-2.5 space-y-2 shrink-0 shadow-md">
            <div className="flex flex-wrap items-center gap-2">
              <div ref={searchContainerRef} className="relative flex-1 min-w-[220px]">
                <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
                <input
                  type="text"
                  value={manualSymbol}
                  onChange={(e) => {
                    setManualSymbol(e.target.value.toUpperCase());
                    setSymbolSuggestionsOpen(true);
                  }}
                  onFocus={() => setSymbolSuggestionsOpen(true)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      handleOpenManualModal(manualSymbol);
                    }
                  }}
                  placeholder="Enter or search stock to short (e.g. TATASTEEL, SBIN, RELIANCE)..."
                  className="w-full pl-8 pr-3 py-1.5 bg-slate-950/90 border border-slate-700/80 rounded-lg text-xs text-white placeholder-slate-500 focus:outline-none focus:border-rose-500 font-mono"
                />

                {symbolSuggestionsOpen && (symbolSuggestions.length > 0 || isNseSearching) && (
                  <div className="absolute left-0 right-0 top-full mt-1 bg-slate-900 border border-slate-700 rounded-xl shadow-2xl z-50 overflow-hidden max-h-64 overflow-y-auto custom-scrollbar">
                    {isNseSearching && symbolSuggestions.length === 0 && (
                      <div className="px-3 py-2.5 text-[11px] text-slate-400 flex items-center gap-2">
                        <RefreshCw className="w-3 h-3 animate-spin" />
                        Searching all NSE stocks...
                      </div>
                    )}
                    {symbolSuggestions.map((s) => (
                      <button
                        key={s.symbol}
                        type="button"
                        onClick={() => {
                          setManualSymbol(s.symbol);
                          setSymbolSuggestionsOpen(false);
                          handleOpenManualModal(s.symbol);
                        }}
                        className="w-full px-3 py-2 text-left hover:bg-slate-800 flex items-center justify-between transition-all border-b border-slate-800/60 last:border-none"
                      >
                        <div>
                          <span className="font-bold text-white text-xs mr-2">{s.symbol}</span>
                          <span className="text-[11px] text-slate-400">{s.name !== s.symbol ? s.name : ''}</span>
                        </div>
                        <div className="flex items-center gap-1.5">
                          {(s as any).sector && (
                            <span className="text-[9px] px-1.5 py-0.5 rounded bg-slate-800 text-rose-300 border border-slate-700">
                              {(s as any).sector}
                            </span>
                          )}
                          {s.segment === 'NSE_ALL' && (
                            <span className="text-[8px] px-1.5 py-0.5 rounded bg-violet-500/20 text-violet-300 border border-violet-500/30 font-bold uppercase tracking-wide">
                              All NSE
                            </span>
                          )}
                        </div>
                      </button>
                    ))}
                    {isNseSearching && symbolSuggestions.length > 0 && (
                      <div className="px-3 py-1.5 text-[10px] text-slate-500 flex items-center gap-1.5 border-t border-slate-800">
                        <RefreshCw className="w-2.5 h-2.5 animate-spin" />
                        Searching more NSE stocks...
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* Add Short Stock Button (Opens Manual Position Config Modal) */}
              <button
                type="button"
                onClick={() => handleOpenManualModal(manualSymbol)}
                className="px-3.5 py-1.5 rounded-lg bg-rose-600 hover:bg-rose-500 text-white font-bold text-xs shadow-sm shadow-rose-950/60 active:scale-95 transition-all flex items-center gap-1.5 cursor-pointer"
                title="Open Manual Short Position Form (Entry, SL, Qty)"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>+ Track Manual Short</span>
              </button>

              {trades.length > 0 && (
                <button
                  type="button"
                  onClick={handleClearShortlist}
                  className="px-2.5 py-1.5 rounded-lg bg-rose-500/15 hover:bg-rose-500/25 text-rose-300 hover:text-rose-200 border border-rose-500/40 font-semibold text-xs transition-all flex items-center gap-1 ml-auto"
                >
                  <Trash2 className="w-3.5 h-3.5 text-rose-400" />
                  <span>Clear All</span>
                </button>
              )}
            </div>

            {showCustomLevels && (
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-2 border-t border-slate-800/80">
                <div>
                  <label className="text-[10px] text-slate-400 block mb-0.5">Short Entry / Breakdown Trigger (₹)</label>
                  <input
                    type="number"
                    step="0.05"
                    value={customEntry}
                    onChange={(e) => setCustomEntry(e.target.value)}
                    placeholder="e.g. 2450.00"
                    className="w-full px-2.5 py-1 bg-slate-950 border border-slate-700 rounded text-xs text-white font-mono focus:border-rose-500"
                  />
                </div>
                <div>
                  <label className="text-[10px] text-slate-400 block mb-0.5">Stop Loss Ceiling (₹ Above Entry)</label>
                  <input
                    type="number"
                    step="0.05"
                    value={customSL}
                    onChange={(e) => setCustomSL(e.target.value)}
                    placeholder="e.g. 2474.50 (+1.0%)"
                    className="w-full px-2.5 py-1 bg-slate-950 border border-slate-700 rounded text-xs text-white font-mono focus:border-rose-500"
                  />
                </div>
                <div>
                  <label className="text-[10px] text-slate-400 block mb-0.5">Target 1 (₹ Below Entry)</label>
                  <input
                    type="number"
                    step="0.05"
                    value={customT1}
                    onChange={(e) => setCustomT1(e.target.value)}
                    placeholder="e.g. 2376.50 (-3.0%)"
                    className="w-full px-2.5 py-1 bg-slate-950 border border-slate-700 rounded text-xs text-white font-mono focus:border-rose-500"
                  />
                </div>
                <div>
                  <label className="text-[10px] text-slate-400 block mb-0.5">Target 2 (₹ Below Entry)</label>
                  <input
                    type="number"
                    step="0.05"
                    value={customT2}
                    onChange={(e) => setCustomT2(e.target.value)}
                    placeholder="e.g. 2303.00 (-6.0%)"
                    className="w-full px-2.5 py-1 bg-slate-950 border border-slate-700 rounded text-xs text-white font-mono focus:border-rose-500"
                  />
                </div>
              </div>
            )}
          </div>

          {/* Trade Cards List */}
          {isLoading && trades.length === 0 ? (
            <div className="flex-1 flex flex-col items-center justify-center py-16 text-slate-500 space-y-3">
              <RefreshCw className="w-8 h-8 animate-spin text-rose-500" />
              <p className="text-xs">Loading Live Shorts Desk setups...</p>
            </div>
          ) : trades.length === 0 ? (
            <div className="flex-1 flex flex-col items-center justify-center py-20 text-center border border-dashed border-slate-800 rounded-2xl bg-slate-900/30 p-8 space-y-4">
              <div className="w-14 h-14 rounded-2xl bg-rose-500/10 border border-rose-500/20 flex items-center justify-center text-rose-400">
                <TrendingDown className="w-7 h-7" />
              </div>
              <div className="max-w-md space-y-1">
                <h3 className="text-base font-bold text-white">No Short Setups on Desk</h3>
                <p className="text-xs text-slate-400 leading-relaxed">
                  Breakdown candidates from Chart Analysis that meet institutional criteria (Score ≥ 85, RVOL ≥ 1.0, 5m RSI &gt; 27) will appear here automatically. You can also search and add any stock above to begin short risk surveillance.
                </p>
              </div>
            </div>
          ) : (
            <div className="space-y-3.5">
              {trades.map((trade) => {
                const entryPx = trade.entry_price || 0;
                const currPx = trade.current_price || entryPx;
                // For shorts, profit is when current price is BELOW entry price
                const pnlPct = entryPx > 0 ? ((entryPx - currPx) / entryPx) * 100 : 0;
                const isProfitable = pnlPct >= 0;
                const isPending = trade.status === 'PENDING_APPROVAL';
                const isActiveTrade = trade.status === 'ACTIVE_IN_TRADE';
                const isQueued = trade.status === 'QUEUED';

                return (
                  <div
                    key={trade.symbol}
                    className={`rounded-xl border p-4 transition-all duration-200 relative overflow-hidden backdrop-blur-md ${
                      isActiveTrade
                        ? 'bg-gradient-to-br from-rose-950/40 via-slate-900/90 to-slate-950 border-rose-500/50 shadow-lg shadow-rose-950/50'
                        : isPending
                        ? 'bg-gradient-to-br from-amber-950/30 via-slate-900/90 to-slate-950 border-amber-500/50 ring-1 ring-amber-500/30'
                        : isQueued
                        ? 'bg-slate-900/80 border-amber-600/30'
                        : 'bg-slate-900/60 border-slate-800 hover:border-slate-700'
                    }`}
                  >
                    {/* Top Row: Symbol, Badges, Prices */}
                    <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
                      <div className="flex items-center gap-2.5">
                        <span className="text-base font-black text-white tracking-tight font-mono">
                          {trade.symbol}
                        </span>
                        <span className="text-[10px] font-black uppercase px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/40">
                          SELL (SHORT)
                        </span>
                        <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
                          {trade.setup_type || 'BREAKDOWN'}
                        </span>
                        {trade.conviction && (
                          <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30">
                            {trade.conviction} CONVICTION
                          </span>
                        )}
                      </div>

                      {/* Right: Live Price & Unrealized Short PnL */}
                      <div className="flex items-center gap-3">
                        <div className="text-right">
                          <div className="text-[10px] text-slate-400 uppercase">Live LTP</div>
                          <div className="text-sm font-mono font-black text-white">
                            ₹{currPx.toFixed(2)}
                          </div>
                        </div>

                        <div className="text-right pl-2 border-l border-slate-800">
                          <div className="text-[10px] text-slate-400 uppercase">Short P&L</div>
                          <div className={`text-sm font-mono font-black ${isProfitable ? 'text-emerald-400' : 'text-rose-400'}`}>
                            {isProfitable ? '+' : ''}{pnlPct.toFixed(2)}%
                          </div>
                        </div>

                        {onOpenChart && (
                          <button
                            onClick={() => onOpenChart(trade.symbol)}
                            className="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white border border-slate-700"
                            title="Open full chart terminal"
                          >
                            <ExternalLink className="w-3.5 h-3.5" />
                          </button>
                        )}

                        <button
                          onClick={() => handleDeleteTrade(trade.symbol)}
                          className="p-1.5 rounded-lg hover:bg-rose-500/20 text-slate-500 hover:text-rose-300 transition-all"
                          title="Remove from desk"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>

                    {/* Levels Grid: Short Entry, Stop Loss Ceiling, Target 1, Target 2 */}
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 p-2.5 rounded-lg bg-slate-950/80 border border-slate-800/80 mb-3 text-xs">
                      <div>
                        <span className="text-[10px] text-slate-400 block uppercase">Trigger Entry</span>
                        <span className="font-mono font-bold text-white">₹{entryPx.toFixed(2)}</span>
                      </div>
                      <div>
                        <span className="text-[10px] text-rose-400 block uppercase">SL Ceiling (+1.0%)</span>
                        <span className="font-mono font-bold text-rose-300">₹{trade.stop_loss ? trade.stop_loss.toFixed(2) : '-'}</span>
                      </div>
                      <div>
                        <span className="text-[10px] text-emerald-400 block uppercase">Target 1 (-3.0%)</span>
                        <span className="font-mono font-bold text-emerald-300">₹{trade.target_1 ? trade.target_1.toFixed(2) : '-'}</span>
                      </div>
                      <div>
                        <span className="text-[10px] text-cyan-400 block uppercase">Target 2 (-6.0%)</span>
                        <span className="font-mono font-bold text-cyan-300">₹{trade.target_2 ? trade.target_2.toFixed(2) : '-'}</span>
                      </div>
                    </div>

                    {/* Thesis & Evidence */}
                    {(trade.thesis || trade.tape_evidence) && (
                      <div className="text-[11px] text-slate-300 space-y-1 mb-3 bg-slate-900/40 p-2 rounded-lg border border-slate-800/50">
                        {trade.thesis && <p><strong className="text-slate-400">Thesis:</strong> {trade.thesis}</p>}
                        {trade.tape_evidence && (
                          <p className="text-[10px] text-slate-400 font-mono">
                            📊 {trade.tape_evidence}
                          </p>
                        )}
                      </div>
                    )}

                    {/* Bottom Status & Approval Action Bar */}
                    <div className="flex flex-wrap items-center justify-between gap-2 pt-2 border-t border-slate-800/70">
                      <div className="flex items-center gap-2">
                        <span className={`text-[10px] font-black uppercase px-2.5 py-1 rounded-full border ${
                          isActiveTrade
                            ? 'bg-rose-500/20 text-rose-300 border-rose-500/50 animate-pulse'
                            : isPending
                            ? 'bg-amber-500/20 text-amber-300 border-amber-500/50 animate-pulse'
                            : isQueued
                            ? 'bg-slate-800 text-amber-300 border-amber-500/30'
                            : 'bg-slate-800 text-slate-400 border-slate-700'
                        }`}>
                          {isActiveTrade ? '🔴 ACTIVE IN SHORT TRADE' : isPending ? '⏳ AWAITING APPROVAL' : isQueued ? '⏳ QUEUED (SLOTS FULL)' : trade.status}
                        </span>

                        {trade.rsi_5m && (
                          <span className="text-[10px] font-mono text-slate-400 px-2 py-0.5 rounded bg-slate-900 border border-slate-800">
                            5m RSI: <strong className={trade.rsi_5m <= 30 ? 'text-rose-400' : 'text-slate-200'}>{trade.rsi_5m.toFixed(1)}</strong>
                          </span>
                        )}
                      </div>

                      {/* Interactive Buttons */}
                      {isPending && (
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => handleApprove(trade)}
                            disabled={actionLoading[trade.symbol]}
                            className="px-3.5 py-1.5 rounded-lg bg-gradient-to-r from-rose-600 to-red-600 hover:from-rose-500 hover:to-red-500 text-white font-black text-xs shadow-md shadow-rose-950/60 active:scale-95 transition-all flex items-center gap-1.5 disabled:opacity-50"
                          >
                            <Check className="w-3.5 h-3.5" />
                            <span>{actionLoading[trade.symbol] ? 'Routing...' : '⚡ APPROVE SELL (₹60k)'}</span>
                          </button>
                          <button
                            type="button"
                            onClick={() => handleDecline(trade.symbol)}
                            disabled={actionLoading[trade.symbol]}
                            className="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 font-bold text-xs border border-slate-700 active:scale-95 transition-all flex items-center gap-1"
                          >
                            <X className="w-3.5 h-3.5" />
                            <span>Decline</span>
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* ── RIGHT / LOWER: Chronological Timeline Log (40% width on Desktop) ── */}
        <div className="w-full md:w-[420px] lg:w-[460px] flex flex-col min-h-0 bg-slate-900/50 p-4 overflow-hidden">
          <div className="flex items-center justify-between shrink-0 pb-3 border-b border-slate-800">
            <div className="flex items-center gap-2">
              <Clock className="w-4 h-4 text-rose-400" />
              <h2 className="text-xs font-bold text-slate-200 uppercase tracking-wider">
                Shorts Microstructure Timeline
              </h2>
            </div>
            <span className="text-[10px] font-mono text-slate-400">
              {filteredEvents.length} events
            </span>
          </div>

          {/* Timeline Filter Pills */}
          <div className="flex items-center gap-1.5 py-2.5 shrink-0 overflow-x-auto">
            {(['all', 'triggers', 'targets', 'risk'] as const).map((f) => (
              <button
                key={f}
                onClick={() => setTimelineFilter(f)}
                className={`px-2.5 py-1 rounded-full text-[10px] font-bold uppercase transition-all ${
                  timelineFilter === f
                    ? 'bg-rose-500/25 text-rose-300 border border-rose-500/50'
                    : 'bg-slate-900 text-slate-400 hover:text-slate-200 border border-slate-800'
                }`}
              >
                {f}
              </button>
            ))}
          </div>

          {/* Chronological Events List */}
          <div className="flex-1 overflow-y-auto custom-scrollbar space-y-2.5 pr-1 pt-1">
            {filteredEvents.length === 0 ? (
              <div className="py-12 text-center text-slate-500 text-xs">
                No timeline micro-events recorded yet for shorts desk.
              </div>
            ) : (
              filteredEvents.map((ev) => {
                const isAdvisorySL = ev.event_type === 'MANUAL_ADVISORY_SL' || ev.event_type === 'AUTO_SL_MODIFIED' || ev.message?.includes('SET NEW STOP LOSS') || ev.message?.includes('stop loss');
                const isAdvisoryExit = ev.event_type === 'MANUAL_ADVISORY_EXIT' || ev.event_type === 'HARD_SL_HIT' || ev.message?.includes('EXIT NOW') || ev.message?.includes('Exit recommended');
                const isCashOut = ev.event_type === 'TRAILED_STOP_HIT' || ev.message?.includes('cash out');
                const isTakeProfit = ev.event_type === 'TAKE_PROFIT' || ev.message?.includes('Take your profits');
                const isDump = ev.event_type === 'HUGE_SELLOFF_ALERT' || ev.message?.includes('dump') || ev.message?.includes('selling');
                const isInflow = ev.event_type === 'INSTITUTIONAL_INFLOW' || ev.message?.includes('institutional buyers');
                const isBuyWall = ev.event_type === 'MASSIVE_BUY_WALL' || ev.message?.includes('buy wall');
                const isSellWall = ev.event_type === 'MASSIVE_SELL_WALL' || ev.message?.includes('sell wall');
                const isBookShift = ev.event_type === 'ORDERBOOK_SHIFT' || ev.message?.includes('Order book just flipped');
                const isExit = ev.event_type === 'EXIT' || ev.event_type === 'STOPPED_OUT';
                const isTP = ev.event_type === 'TP1_HIT' || ev.event_type === 'TP2_HIT';
                const isTrigger = ev.event_type === 'CONFIRMED_TRIGGER' || ev.event_type === 'ENTRY_TRIGGERED' || ev.event_type === 'MANUAL_TRADE_ARMED';

                return (
                  <div
                    key={ev.id || `${ev.symbol}-${ev.timestamp_ist}`}
                    className={`p-2.5 rounded-lg border text-xs space-y-1 transition-all ${
                      isAdvisoryExit
                        ? 'border-rose-500 bg-rose-950/70 shadow-rose-950/60 animate-pulse'
                        : isAdvisorySL
                        ? 'border-amber-500 bg-amber-950/60 shadow-amber-950/50'
                        : isDump
                        ? 'border-emerald-600/70 bg-emerald-950/40'
                        : isInflow
                        ? 'border-rose-600/70 bg-rose-950/50'
                        : isBuyWall
                        ? 'border-amber-600/70 bg-amber-950/40'
                        : isSellWall
                        ? 'border-cyan-600/70 bg-cyan-950/40'
                        : isBookShift
                        ? 'border-purple-600/70 bg-purple-950/40'
                        : isTakeProfit || isCashOut
                        ? 'border-emerald-500/70 bg-emerald-950/50'
                        : 'border-slate-800/80 bg-slate-950/70 hover:border-slate-700'
                    }`}
                  >
                    <div className="flex items-center justify-between text-[10px]">
                      <div className="flex items-center gap-1.5">
                        <span className="font-mono font-black text-white">{ev.symbol}</span>
                        <span className={`px-1.5 py-0.2 rounded font-bold uppercase ${
                          isAdvisoryExit
                            ? 'bg-rose-600 text-white font-black animate-pulse'
                            : isAdvisorySL
                            ? 'bg-amber-500 text-black font-black'
                            : isDump
                            ? 'bg-emerald-900 text-emerald-200 border border-emerald-500/60'
                            : isInflow
                            ? 'bg-rose-900 text-rose-200 border border-rose-600/60'
                            : isBuyWall
                            ? 'bg-amber-900 text-amber-200 border border-amber-600/60'
                            : isSellWall
                            ? 'bg-cyan-900 text-cyan-200 border border-cyan-500/60'
                            : isBookShift
                            ? 'bg-purple-900 text-purple-200 border border-purple-500/60'
                            : isTakeProfit
                            ? 'bg-emerald-600 text-white font-black'
                            : isCashOut
                            ? 'bg-amber-600 text-white font-black'
                            : isTP
                            ? 'bg-emerald-500/20 text-emerald-300'
                            : isExit
                            ? 'bg-rose-500/20 text-rose-300'
                            : isTrigger
                            ? 'bg-amber-500/20 text-amber-300'
                            : 'bg-slate-800 text-slate-300'
                        }`}>
                          {isAdvisoryExit
                            ? '🚨 EXIT NOW'
                            : isAdvisorySL
                            ? '📢 MOVE STOP LOSS'
                            : isDump
                            ? '🔥 BIG DUMP'
                            : isInflow
                            ? '⚠️ BUY INFLOW'
                            : isBuyWall
                            ? '🧱 BIG BUY WALL'
                            : isSellWall
                            ? '🧱 BIG SELL WALL'
                            : isBookShift
                            ? '⚡ ORDER BOOK SHIFT'
                            : isTakeProfit
                            ? '💰 TAKE PROFIT'
                            : isCashOut
                            ? '🛑 CASH OUT'
                            : ev.event_type.replace(/_/g, ' ')}
                        </span>
                      </div>
                      <span className="text-slate-500 font-mono">{ev.timestamp_ist}</span>
                    </div>

                    <p className={`text-[11px] leading-relaxed ${
                      isAdvisoryExit || isInflow ? 'text-rose-100 font-bold' :
                      isAdvisorySL ? 'text-amber-100 font-semibold' :
                      isDump || isTakeProfit ? 'text-emerald-100 font-semibold' :
                      isSellWall ? 'text-cyan-100 font-semibold' :
                      isBuyWall ? 'text-amber-100 font-semibold' :
                      'text-slate-300'
                    }`}>
                      {ev.message}
                    </p>

                    {ev.price_at_event && (
                      <div className="text-[10px] text-slate-400 font-mono">
                        Price at event: <strong className="text-slate-200">₹{Number(ev.price_at_event).toFixed(2)}</strong>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>

      {/* ── Watchdog Settings Modal (Shared across Longs & Shorts) ── */}
      {isSettingsOpen && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-700 rounded-2xl w-full max-w-lg overflow-hidden shadow-2xl space-y-4 p-5">
            <div className="flex items-center justify-between border-b border-slate-800 pb-3">
              <div className="flex items-center gap-2">
                <Settings className="w-5 h-5 text-rose-400" />
                <h3 className="font-bold text-white text-sm">Watchdog & Capital Settings</h3>
              </div>
              <button
                type="button"
                onClick={() => setIsSettingsOpen(false)}
                className="p-1 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-white"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <form onSubmit={handleSaveSettings} className="space-y-4 text-xs">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-slate-300 block mb-1 font-semibold">Total Capital (₹)</label>
                  <input
                    type="number"
                    value={formTotalCapital}
                    onChange={(e) => setFormTotalCapital(e.target.value)}
                    className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono"
                  />
                </div>
                <div>
                  <label className="text-slate-300 block mb-1 font-semibold">Allocation Per Trade (₹)</label>
                  <input
                    type="number"
                    value={formPerTrade}
                    onChange={(e) => setFormPerTrade(e.target.value)}
                    className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-slate-300 block mb-1 font-semibold">Max Active Slots (System-Wide)</label>
                  <input
                    type="number"
                    value={formMaxSlots}
                    onChange={(e) => setFormMaxSlots(e.target.value)}
                    className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono"
                  />
                </div>
                <div>
                  <label className="text-slate-300 block mb-1 font-semibold">Hard SL Ceiling (%)</label>
                  <input
                    type="number"
                    step="0.1"
                    value={formMaxSLPercent}
                    onChange={(e) => setFormMaxSLPercent(e.target.value)}
                    className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono"
                  />
                </div>
              </div>

              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="text-slate-300 block mb-1 font-semibold">Breakeven Lock (%)</label>
                  <input
                    type="number"
                    step="0.1"
                    value={formBreakevenPercent}
                    onChange={(e) => setFormBreakevenPercent(e.target.value)}
                    className="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono"
                  />
                </div>
                <div>
                  <label className="text-slate-300 block mb-1 font-semibold">Structural Trail (%)</label>
                  <input
                    type="number"
                    step="0.1"
                    value={formTrailPercent}
                    onChange={(e) => setFormTrailPercent(e.target.value)}
                    className="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono"
                  />
                </div>
                <div>
                  <label className="text-slate-300 block mb-1 font-semibold">Auto-Purge Slip (%)</label>
                  <input
                    type="number"
                    step="0.05"
                    value={formPurgePercent}
                    onChange={(e) => setFormPurgePercent(e.target.value)}
                    className="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono"
                  />
                </div>
              </div>

              <div className="p-3 rounded-xl bg-slate-950 border border-slate-800 text-[11px] text-slate-400 space-y-1">
                <p>💡 <strong>Shared Space:</strong> These settings apply system-wide across both Longs and Shorts desks.</p>
                <p>200-depth / 20-depth sockets and the ₹1.80L capital pool are shared dynamically.</p>
              </div>

              <div className="flex justify-end gap-2 pt-2 border-t border-slate-800">
                <button
                  type="button"
                  onClick={() => setIsSettingsOpen(false)}
                  className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isSavingSettings}
                  className="px-4 py-1.5 rounded-lg bg-rose-600 hover:bg-rose-500 text-white font-bold disabled:opacity-50"
                >
                  {isSavingSettings ? 'Saving...' : 'Save Settings'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
      {/* ── Track Manual Short Stock Modal ── */}
      {isManualModalOpen && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 overflow-y-auto">
          <div className="bg-slate-900 border border-rose-500/60 rounded-2xl w-full max-w-lg shadow-2xl overflow-hidden flex flex-col my-8">
            <div className="px-5 py-3.5 border-b border-slate-800 bg-slate-950/80 flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className="w-8 h-8 rounded-lg bg-rose-500/20 border border-rose-500/40 flex items-center justify-center text-rose-400">
                  <Shield className="w-4 h-4" />
                </div>
                <div>
                  <h3 className="text-sm font-black text-white">Track Manual Short Stock (Shorts Desk)</h3>
                  <p className="text-[10px] text-slate-400">Slot Assignment • 200/20 Depth Allotted • Watchdog Advisory</p>
                </div>
              </div>
              <button
                onClick={() => setIsManualModalOpen(false)}
                className="p-1 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800 transition-all cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="p-5 space-y-4 text-xs">
              {/* Mode Notice */}
              <div className="p-3 rounded-xl bg-rose-950/40 border border-rose-500/40 space-y-1.5 text-slate-300 leading-relaxed text-[11px]">
                <div className="font-bold text-rose-300 flex items-center gap-1.5 uppercase tracking-wide text-[10px]">
                  <Activity className="w-3.5 h-3.5 text-rose-400" />
                  <span>Advisory Mode Active (Manual Short Position)</span>
                </div>
                <div>
                  Watchdog continuously tracks <strong>LTP, 5m VWAP, 20-EMA, RSI, CVD Delta, Deep OBI & Wall detection</strong>.
                  Because this trade is manually added, Watchdog will <strong>NOT</strong> place, modify, or buy-to-cover on Dhan automatically.
                  Instead, it will stream real-time updates like <strong>&quot;SET NEW STOP LOSS HERE&quot;</strong> and exit advisories to your timeline & Telegram.
                </div>
              </div>

              {/* Form Grid */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {/* Symbol with Live Search Autocomplete */}
                <div className="sm:col-span-2 relative">
                  <label className="text-[10px] text-slate-300 uppercase font-bold block mb-1">
                    Stock Symbol <span className="text-rose-400">*</span>
                  </label>
                  <input
                    type="text"
                    value={manualSymbol}
                    onChange={(e) => {
                      setManualSymbol(e.target.value.toUpperCase());
                      setModalSuggestionsOpen(true);
                    }}
                    onFocus={() => setModalSuggestionsOpen(true)}
                    placeholder="Search or enter any NSE stock (e.g. GALLANTT, TATASTEEL)..."
                    className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono font-bold text-sm focus:outline-none focus:border-rose-500 uppercase"
                  />
                  {modalSuggestionsOpen && symbolSuggestions.length > 0 && (
                    <div className="absolute left-0 right-0 top-full mt-1 bg-slate-900 border border-slate-700 rounded-xl shadow-2xl z-50 overflow-hidden max-h-48 overflow-y-auto custom-scrollbar">
                      {symbolSuggestions.map((s) => (
                        <button
                          key={s.symbol}
                          type="button"
                          onClick={() => {
                            setManualSymbol(s.symbol);
                            setModalSuggestionsOpen(false);
                            handleOpenManualModal(s.symbol);
                          }}
                          className="w-full px-3 py-2 text-left hover:bg-slate-800 flex items-center justify-between transition-all border-b border-slate-800/60 last:border-none cursor-pointer"
                        >
                          <div>
                            <span className="font-bold text-white text-xs mr-2">{s.symbol}</span>
                            <span className="text-[11px] text-slate-400">{s.name !== s.symbol ? s.name : ''}</span>
                          </div>
                          <span className="text-[9px] px-1.5 py-0.5 rounded bg-slate-800 text-rose-300 border border-slate-700">
                            NSE_EQ
                          </span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                {/* Entry Price */}
                <div>
                  <label className="text-[10px] text-slate-300 uppercase font-bold block mb-1">
                    Short Entry / Trigger Price (₹) <span className="text-rose-400">*</span>
                  </label>
                  <input
                    type="number"
                    step="0.05"
                    value={customEntry}
                    onChange={(e) => {
                      const val = e.target.value;
                      setCustomEntry(val);
                      const px = parseFloat(val);
                      if (!isNaN(px) && px > 0) {
                        setCustomSL((px * 1.01).toFixed(2));
                        setCustomT1((px * 0.97).toFixed(2));
                        setCustomT2((px * 0.94).toFixed(2));
                        setCustomQty(String(Math.max(1, Math.floor(60000 / px))));
                      }
                    }}
                    placeholder="e.g. 2450.00"
                    className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono font-bold text-xs focus:outline-none focus:border-rose-500"
                  />
                </div>

                {/* Stop Loss (Must be > Entry) */}
                <div>
                  <label className="text-[10px] text-slate-300 uppercase font-bold block mb-1">
                    Stop Loss Ceiling (₹) <span className="text-rose-400">* (+1.0%)</span>
                  </label>
                  <input
                    type="number"
                    step="0.05"
                    value={customSL}
                    onChange={(e) => setCustomSL(e.target.value)}
                    placeholder="Must be > Entry"
                    className="w-full px-3 py-2 bg-slate-950 border border-rose-800/80 rounded-lg text-rose-300 font-mono font-bold text-xs focus:outline-none focus:border-rose-500"
                  />
                </div>

                {/* Quantity */}
                <div>
                  <label className="text-[10px] text-slate-300 uppercase font-bold block mb-1">
                    Quantity (Shares) <span className="text-rose-400">*</span>
                  </label>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={customQty}
                    onChange={(e) => setCustomQty(e.target.value)}
                    placeholder="e.g. 25"
                    className="w-full px-3 py-2 bg-slate-950 border border-cyan-800/80 rounded-lg text-cyan-200 font-mono font-bold text-xs focus:outline-none focus:border-cyan-500"
                  />
                </div>

                {/* Target 1 */}
                <div>
                  <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-1">
                    Target 1 (₹) <span className="text-emerald-400 font-normal">(-3% Below Entry)</span>
                  </label>
                  <input
                    type="number"
                    step="0.05"
                    value={customT1}
                    onChange={(e) => setCustomT1(e.target.value)}
                    placeholder="e.g. 2380.00"
                    className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded-lg text-emerald-300 font-mono text-xs focus:outline-none focus:border-emerald-500"
                  />
                </div>

                {/* Target 2 */}
                <div>
                  <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-1">
                    Target 2 (₹) <span className="text-cyan-400 font-normal">(-6% Runner)</span>
                  </label>
                  <input
                    type="number"
                    step="0.05"
                    value={customT2}
                    onChange={(e) => setCustomT2(e.target.value)}
                    placeholder="e.g. 2300.00"
                    className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded-lg text-cyan-300 font-mono text-xs focus:outline-none focus:border-cyan-500"
                  />
                </div>
              </div>

              {/* Sizing & System Allocation Card */}
              <div className="p-3 rounded-xl bg-slate-950/80 border border-slate-800 grid grid-cols-3 gap-2 text-center">
                <div>
                  <div className="text-[9px] uppercase font-semibold text-slate-400">Position Value</div>
                  <div className="font-mono font-bold text-white text-xs mt-0.5">
                    ₹{((parseFloat(customEntry) || 0) * (parseInt(customQty, 10) || 0)).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                  </div>
                </div>
                <div>
                  <div className="text-[9px] uppercase font-semibold text-slate-400">Max Risk (SL)</div>
                  <div className="font-mono font-bold text-rose-400 text-xs mt-0.5">
                    ₹{Math.max(0, ((parseFloat(customSL) || 0) - (parseFloat(customEntry) || 0)) * (parseInt(customQty, 10) || 0)).toFixed(1)}
                  </div>
                </div>
                <div>
                  <div className="text-[9px] uppercase font-semibold text-slate-400">Depth / Slot</div>
                  <div className="font-mono font-bold text-purple-300 text-xs mt-0.5">
                    200L / Slot {watchdogPositions.length + 1} of 3
                  </div>
                </div>
              </div>
            </div>

            {/* Modal Actions */}
            <div className="px-5 py-3.5 border-t border-slate-800 bg-slate-950/80 flex items-center justify-end gap-2.5">
              <button
                type="button"
                onClick={() => setIsManualModalOpen(false)}
                className="px-3.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold transition-all cursor-pointer"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleConfirmAddManualTrade}
                disabled={isAddingManual || !manualSymbol.trim() || !customEntry || !customSL || !customQty}
                className="px-4 py-1.5 rounded-lg bg-rose-600 hover:bg-rose-500 text-white font-bold text-xs shadow-md shadow-rose-950/60 active:scale-95 transition-all flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
              >
                <Check className="w-3.5 h-3.5" />
                <span>{isAddingManual ? 'Arming Watchdog...' : 'Arm Manual Short Surveillance'}</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
