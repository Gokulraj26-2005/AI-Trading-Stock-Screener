import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Shield,
  Target,
  Clock,
  TrendingUp,
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
  Power,
  PowerOff,
  Sliders,
} from 'lucide-react';
import {
  getShortlist,
  pollShortlistSurveillance,
  getShortlistTimeline,
  deleteShortlistTrade,
  clearShortlist,
  getAnalysisSymbols,
  searchNseSymbols,
  getSymbolAnalysis,
  addManualShortlistTrade,
  getCapitalSlotsSummary,
  getWatchdogPositions,
  approveWatchdogTrade,
  declineWatchdogTrade,
  emergencyKillAllPositions,
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

interface ShortlistedPageProps {
  token?: string;
  onOpenChart?: (symbol: string) => void;
}

export function ShortlistedPage({ token: _token, onOpenChart }: ShortlistedPageProps) {
  // ── 1. Data & Real-Time Surveillance State ─────────────────────────────────
  const [trades, setTrades] = useState<ShortlistedTrade[]>([]);
  const [events, setEvents] = useState<ShortlistTimelineEvent[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [isSurveillanceActive, setIsSurveillanceActive] = useState<boolean>(true);
  const [lastEvaluatedAt, setLastEvaluatedAt] = useState<string | null>(null);
  const [timelineFilter, setTimelineFilter] = useState<'all' | 'triggers' | 'targets' | 'risk'>('all');

  // Watchdog & Capital Slot Allocation State (₹1.80L total, ₹60k/trade, 3 slots)
  const [slotsSummary, setSlotsSummary] = useState<CapitalSlotsSummary | null>(null);
  const [watchdogPositions, setWatchdogPositions] = useState<WatchdogPositionItem[]>([]);
  const [actionLoading, setActionLoading] = useState<Record<string, boolean>>({});

  // ── 1B. Watchdog Settings & Master Execution Switch State ───────────────────
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

  // ── 2. Data Hydration & Polling ────────────────────────────────────────────
  const loadInitialData = useCallback(async () => {
    setIsLoading(true);
    try {
      const [tradeRes, eventRes, slotRes, posRes, settingRes] = await Promise.all([
        getShortlist().catch(() => null),
        getShortlistTimeline(undefined, 80).catch(() => null),
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
        setWatchdogPositions(posRes.positions.filter((p: any) => (p.action || 'BUY').toUpperCase() === 'BUY'));
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
      console.error('Failed to load initial live tracking data:', err);
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
    const sl = commentary?.stop_loss ? parseFloat(commentary.stop_loss) : Number((entryPx * 0.985).toFixed(2));
    const t1 = commentary?.target_price_1 ? parseFloat(commentary.target_price_1) : Number((entryPx * 1.03).toFixed(2));
    const t2 = commentary?.target_price_2 ? parseFloat(commentary.target_price_2) : Number((entryPx * 1.06).toFixed(2));
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
    const sl = parseFloat(customSL);
    const qty = parseInt(customQty, 10);

    if (isNaN(entryPx) || entryPx <= 0) {
      setErrorMsg('Please enter a valid Entry Price greater than 0.');
      return;
    }
    if (isNaN(sl) || sl <= 0) {
      setErrorMsg('Please enter a valid Stop Loss greater than 0.');
      return;
    }
    if (sl >= entryPx) {
      setErrorMsg('For BUY positions, Stop Loss must be strictly below Entry Price.');
      return;
    }
    if (isNaN(qty) || qty <= 0) {
      setErrorMsg('Please enter a valid Quantity of at least 1.');
      return;
    }

    setIsAddingManual(true);
    setErrorMsg(null);
    try {
      const payload: AddManualTradePayload = {
        symbol: sym,
        action: 'BUY',
        current_price: entryPx,
        entry_price: entryPx,
        stop_loss: sl,
        target_1: customT1 ? parseFloat(customT1) : Number((entryPx * 1.03).toFixed(2)),
        target_2: customT2 ? parseFloat(customT2) : Number((entryPx * 1.06).toFixed(2)),
        quantity: qty,
        setup_type: 'MANUAL_CONVICTION',
        thesis: `Manual position monitored by Live Watchdog (Advisory Only). Entry: ₹${entryPx}, SL: ₹${sl}, Qty: ${qty}.`,
      };

      const res = await addManualShortlistTrade(payload);
      if (res && res.trade) {
        setTrades((prev) => {
          const exists = prev.some((t) => t.symbol === res.trade.symbol);
          if (exists) {
            return prev.map((t) => (t.symbol === res.trade.symbol ? res.trade : t));
          }
          return [res.trade, ...prev];
        });

        const [evRes, slotRes, posRes] = await Promise.all([
          getShortlistTimeline(undefined, 80).catch(() => null),
          getCapitalSlotsSummary().catch(() => null),
          getWatchdogPositions().catch(() => null),
        ]);
        if (evRes?.events) setEvents(evRes.events);
        if (slotRes) setSlotsSummary(slotRes);
        if (posRes?.positions) {
          setWatchdogPositions(posRes.positions.filter((p: any) => (p.action || 'BUY').toUpperCase() === 'BUY'));
        }

        setIsManualModalOpen(false);
        setManualSymbol('');
      }
    } catch (err: any) {
      console.error('Failed to add manual trade:', err);
      setErrorMsg(err?.response?.data?.detail || err.message || 'Failed to add manual trade.');
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
          pollShortlistSurveillance().catch(() => null),
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
          setWatchdogPositions(posRes.positions.filter((p: any) => (p.action || 'BUY').toUpperCase() === 'BUY'));
        }
      } catch (err) {
        console.debug('Surveillance poll error:', err);
      }
    }, 2500);

    return () => clearInterval(interval);
  }, [isSurveillanceActive]);

  // ── 3B. Master Execution Switch & Settings Handlers ───────────────────────
  const handleToggleExecution = async () => {
    setIsTogglingExecution(true);
    setErrorMsg(null);
    try {
      const res = await toggleExecutionSwitch();
      if (res && res.status === 'ok') {
        setSettings((prev) => {
          if (!prev) {
            return {
              execution_enabled: res.execution_enabled,
              total_capital: 180000,
              per_trade_allocation: 60000,
              max_active_trades: 3,
              max_hard_stop_loss_pct: 0.01,
              max_hard_stop_loss_percent: 1.0,
              breakeven_trigger_pct: 0.008,
              breakeven_trigger_percent: 0.8,
              fee_buffer_pct: 0.001,
              fee_buffer_percent: 0.1,
              structural_trail_pct: 0.015,
              structural_trail_percent: 1.5,
              purge_slip_tolerance_pct: 0.002,
              purge_slip_tolerance_percent: 0.2,
              min_potential_score: 85,
              max_entry_rsi_5m: 73,
              min_entry_rvol: 1.0,
              telegram_configured: true,
            };
          }
          return { ...prev, execution_enabled: res.execution_enabled };
        });
        const fresh = await getWatchdogSettings().catch(() => null);
        if (fresh && fresh.settings) {
          setSettings(fresh.settings);
        }
      }
    } catch (err: any) {
      console.error('Failed to toggle execution switch:', err);
      setErrorMsg(err?.response?.data?.detail || err.message || 'Failed to toggle execution switch');
    } finally {
      setIsTogglingExecution(false);
    }
  };

  const handleSaveSettings = async () => {
    setIsSavingSettings(true);
    setErrorMsg(null);
    try {
      const res = await updateWatchdogSettings({
        total_capital: parseFloat(formTotalCapital) || 180000,
        per_trade_allocation: parseFloat(formPerTrade) || 60000,
        max_active_trades: parseInt(formMaxSlots, 10) || 3,
        max_hard_stop_loss_percent: parseFloat(formMaxSLPercent) || 1.0,
        breakeven_trigger_percent: parseFloat(formBreakevenPercent) || 0.8,
        structural_trail_percent: parseFloat(formTrailPercent) || 1.5,
        purge_slip_tolerance_percent: parseFloat(formPurgePercent) || 0.20,
      });
      if (res && res.settings) {
        setSettings(res.settings);
        const slotRes = await getCapitalSlotsSummary().catch(() => null);
        if (slotRes) setSlotsSummary(slotRes);
        setIsSettingsOpen(false);
      }
    } catch (err: any) {
      console.error('Failed to save settings:', err);
      setErrorMsg(err?.response?.data?.detail || err.message || 'Failed to save settings');
    } finally {
      setIsSavingSettings(false);
    }
  };

  // ── 3B. Watchdog Interactive Approval & Risk Handlers ─────────────────────
  const handleApproveTrade = async (symbol: string, triggerPrice: number, securityId?: string) => {
    setActionLoading((prev) => ({ ...prev, [symbol]: true }));
    setErrorMsg(null);
    try {
      const res = await approveWatchdogTrade(symbol, triggerPrice, securityId || '');
      if (res && (res.status === 'EXECUTED' || res.status === 'QUEUED')) {
        const [slotRes, posRes, tradeRes] = await Promise.all([
          getCapitalSlotsSummary().catch(() => null),
          getWatchdogPositions().catch(() => null),
          getShortlist().catch(() => null),
        ]);
        if (slotRes) setSlotsSummary(slotRes);
        if (posRes && posRes.positions) setWatchdogPositions(posRes.positions);
        if (tradeRes && tradeRes.trades) setTrades(tradeRes.trades);
      }
    } catch (err: any) {
      console.error('Approval failed:', err);
      setErrorMsg(err?.response?.data?.detail || err.message || 'Approval failed');
    } finally {
      setActionLoading((prev) => ({ ...prev, [symbol]: false }));
    }
  };

  const handleDeclineTrade = async (symbol: string) => {
    setActionLoading((prev) => ({ ...prev, [symbol]: true }));
    try {
      await declineWatchdogTrade(symbol);
      setTrades((prev) => prev.filter((t) => t.symbol !== symbol));
    } catch (err: any) {
      console.error('Decline failed:', err);
    } finally {
      setActionLoading((prev) => ({ ...prev, [symbol]: false }));
    }
  };

  const handleEmergencyKillAll = async () => {
    if (!window.confirm('🚨 EMERGENCY: Market exit all active Watchdog positions and cancel orders immediately?')) return;
    try {
      await emergencyKillAllPositions();
      const [slotRes, posRes] = await Promise.all([
        getCapitalSlotsSummary().catch(() => null),
        getWatchdogPositions().catch(() => null),
      ]);
      if (slotRes) setSlotsSummary(slotRes);
      if (posRes && posRes.positions) setWatchdogPositions(posRes.positions);
    } catch (err: any) {
      console.error('Kill all failed:', err);
      setErrorMsg(err?.response?.data?.detail || err.message || 'Emergency kill-all failed');
    }
  };

  // ── 4. Delete Trade & Clear Shortlist ──────────────────────────────────────
  const handleDeleteTrade = async (sym: string) => {
    try {
      await deleteShortlistTrade(sym);
      setTrades((prev) => prev.filter((t) => t.symbol !== sym));
      const [slotRes, posRes] = await Promise.all([
        getCapitalSlotsSummary().catch(() => null),
        getWatchdogPositions().catch(() => null),
      ]);
      if (slotRes) setSlotsSummary(slotRes);
      if (posRes?.positions) {
        setWatchdogPositions(posRes.positions.filter((p: any) => (p.action || 'BUY').toUpperCase() === 'BUY'));
      }
    } catch (err) {
      console.error('Failed to delete trade:', err);
    }
  };

  const handleClearShortlist = async () => {
    if (!window.confirm('Clear all active live tracked setups and surveillance history?')) return;
    try {
      await clearShortlist();
      setTrades([]);
      setEvents([]);
    } catch (err) {
      console.error('Failed to clear live tracking:', err);
    }
  };

  // ── 5. Filtered Timeline Events ────────────────────────────────────────────
  const filteredEvents = useMemo(() => {
    if (timelineFilter === 'all') return events;
    if (timelineFilter === 'triggers') {
      return events.filter((e) => ['ENTRY_TRIGGERED', 'CONFIRMED_TRIGGER', 'SUSTAINED_BREAKOUT', 'MANUAL_TRADE_ARMED', 'INSTITUTIONAL_INFLOW'].includes(e.event_type));
    }
    if (timelineFilter === 'targets') {
      return events.filter((e) => ['TP1_HIT', 'TP2_HIT', 'TAKE_PROFIT'].includes(e.event_type));
    }
    if (timelineFilter === 'risk') {
      return events.filter((e) => ['SL_HIT', 'STOPPED_OUT', 'MANUAL_ADVISORY_SL', 'MANUAL_ADVISORY_EXIT', 'HARD_SL_HIT', 'TRAILED_STOP_HIT', 'HUGE_SELLOFF_ALERT', 'MASSIVE_BUY_WALL', 'MASSIVE_SELL_WALL', 'ORDERBOOK_SHIFT', 'AUTO_SL_MODIFIED'].includes(e.event_type));
    }
    return events;
  }, [events, timelineFilter]);

  return (
    <div className="flex-1 flex flex-col min-h-0 bg-slate-950 text-slate-100 font-sans overflow-hidden">
      {/* ── Top Header & Surveillance Status HUD ───────────────────────────── */}
      <header className="h-12 px-4 border-b border-slate-800/90 bg-slate-900/70 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-cyan-500/20 via-blue-500/20 to-emerald-500/20 border border-cyan-500/40 flex items-center justify-center text-cyan-400 shadow-sm shadow-cyan-950/50">
            <Activity className="w-4 h-4" />
          </div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-sm font-black text-white tracking-tight">Live Trade Desk — LONGS</h1>
            <span className="text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full bg-emerald-950/80 text-emerald-300 border border-emerald-700/50 flex items-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${isSurveillanceActive ? 'bg-emerald-400 animate-pulse' : 'bg-slate-500'}`} />
              {isSurveillanceActive ? 'Surveillance Active (3s)' : 'Surveillance Paused'}
            </span>
            <span className="text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full bg-cyan-950/80 text-cyan-300 border border-cyan-700/50">
              🟢 BUY ONLY
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
                ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/60 hover:bg-emerald-500/30'
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
                settings?.execution_enabled !== false ? 'bg-emerald-400' : 'bg-amber-400'
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
              title="Clear all tracked setups and surveillance history"
            >
              <Trash2 className="w-3.5 h-3.5 text-rose-400" />
              <span>Clear All</span>
            </button>
          )}
        </div>
      </header>

      {/* ── Institutional Capital & Concurrency HUD (Customizable via Settings) ── */}
      <div className="border-b border-slate-800 bg-slate-900/60 px-4 py-2.5 shrink-0 flex flex-wrap items-center justify-between gap-3 text-xs">
        {/* Left: Capital Metrics */}
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-emerald-500/15 border border-emerald-500/30 flex items-center justify-center text-emerald-400">
              <Wallet className="w-3.5 h-3.5" />
            </div>
            <div>
              <div className="text-[10px] uppercase font-bold text-slate-400 leading-tight">Desk Capital</div>
              <div className="font-mono font-black text-white text-xs">
                ₹{(slotsSummary?.total_capital || settings?.total_capital || 180000).toLocaleString('en-IN')}
                <span className="text-[10px] font-normal text-slate-400 ml-1.5">
                  (₹{((settings?.per_trade_allocation || 60000) / 1000).toFixed(0)}k/trade • Max {settings?.max_active_trades || 3} Slots)
                </span>
              </div>
            </div>
          </div>

          <div className="hidden sm:block h-6 w-[1px] bg-slate-800" />

          <div className="flex items-center gap-3">
            <div>
              <div className="text-[10px] uppercase font-semibold text-slate-400">Deployed</div>
              <div className="font-mono font-bold text-cyan-400">
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

        {/* Center: Concurrent Slot Badges (Dynamic based on max_active_trades) */}
        <div className="flex items-center gap-2">
          {Array.from({ length: settings?.max_active_trades || 3 }).map((_, slotIdx) => {
            const activePos = watchdogPositions[slotIdx];
            return (
              <div
                key={slotIdx}
                className={`px-2.5 py-1 rounded-lg border flex items-center gap-1.5 transition-all ${
                  activePos
                    ? 'bg-cyan-950/80 border-cyan-500/60 text-cyan-200 shadow-sm shadow-cyan-950'
                    : 'bg-slate-950/60 border-slate-800 text-slate-500'
                }`}
              >
                <span
                  className={`w-2 h-2 rounded-full ${
                    activePos ? 'bg-cyan-400 animate-pulse' : 'bg-slate-700'
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
                        : 'bg-cyan-900/60 text-cyan-200 border-cyan-500/40'
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

        {/* Right: Settings, Priority Queue & Emergency Kill All */}
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
            title="Edit Total Capital, Per Trade Amount, Max Concurrent Trades, and Stop Loss"
          >
            <Settings className="w-3.5 h-3.5 text-cyan-400" />
            <span>Settings</span>
          </button>

          <button
            onClick={handleEmergencyKillAll}
            disabled={watchdogPositions.length === 0}
            className="px-3 py-1 rounded-lg bg-rose-600/80 hover:bg-rose-500 text-white border border-rose-400/60 font-black text-[11px] shadow-md shadow-rose-950/60 active:scale-95 transition-all flex items-center gap-1.5 disabled:opacity-30 disabled:cursor-not-allowed"
            title="Emergency Market Exit all positions and cancel orders immediately"
          >
            <AlertTriangle className="w-3.5 h-3.5" />
            <span>KILL ALL (EXIT)</span>
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
              Breakout setups remain strictly in observation mode. <strong>Telegram alerts</strong> and <strong>Dhan MCP order routing</strong> are blocked before placement.
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
        
        {/* ── LEFT / UPPER: Monitored Setups Cards (60% width on Desktop) ── */}
        <div className="flex-1 flex flex-col min-h-0 border-r border-slate-800/80 bg-slate-950/50 overflow-y-auto custom-scrollbar p-4 space-y-4">
          
          <div className="flex items-center justify-between shrink-0 pb-1">
            <div className="flex items-center gap-2">
              <Flame className="w-4 h-4 text-amber-400" />
              <h2 className="text-xs font-bold text-slate-200 uppercase tracking-wider">
                Active Tracked Setups ({trades.length} Monitored)
              </h2>
            </div>
            <span className="text-[11px] text-slate-400">
              Live Surveillance • Automatic Trigger, SL & Target Ticks
            </span>
          </div>

          {/* Priority Queue Banner if 3/3 slots occupied */}
          {slotsSummary?.priority_queue && slotsSummary.priority_queue.length > 0 && (
            <div className="p-3 rounded-xl bg-amber-950/30 border border-amber-500/40 space-y-2">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-amber-300 font-bold text-xs">
                  <Zap className="w-4 h-4 text-amber-400 animate-pulse" />
                  <span>Priority Queue (3/3 Slots Active - Auto-Promoting on Vacancy)</span>
                </div>
                <span className="text-[10px] font-mono text-amber-400">
                  {slotsSummary.priority_queue.length} in queue
                </span>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2">
                {slotsSummary.priority_queue.map((qItem: any, idx: number) => (
                  <div
                    key={qItem.symbol || idx}
                    className="p-2 rounded-lg bg-slate-900/90 border border-amber-600/30 flex items-center justify-between text-xs"
                  >
                    <div>
                      <span className="font-black text-white mr-1.5">{qItem.symbol}</span>
                      <span className="text-[10px] text-slate-400 font-mono">₹{qItem.trigger_price}</span>
                    </div>
                    <span className="text-[9px] font-bold px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300">
                      #{idx + 1} NEXT
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── Manual Search & Add Stock Toolbar ── */}
          <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-2.5 space-y-2 shrink-0 shadow-md">
            <div className="flex flex-wrap items-center gap-2">
              {/* Autocomplete Input */}
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
                  placeholder="Enter or search stock (e.g. RELIANCE, TCS, INFY)..."
                  className="w-full pl-8 pr-3 py-1.5 bg-slate-950/90 border border-slate-700/80 rounded-lg text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500 font-mono"
                />

                {/* Autocomplete Suggestions Dropdown */}
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
                            <span className="text-[9px] px-1.5 py-0.5 rounded bg-slate-800 text-cyan-300 border border-slate-700">
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

              {/* Add Stock Button (Opens Manual Position Config Modal) */}
              <button
                type="button"
                onClick={() => handleOpenManualModal(manualSymbol)}
                className="px-3.5 py-1.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white font-bold text-xs shadow-sm shadow-cyan-950/60 active:scale-95 transition-all flex items-center gap-1.5 cursor-pointer"
                title="Open Manual Position Form (Entry, SL, Qty)"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>+ Track Manual Stock</span>
              </button>

              {/* Clear Button */}
              {trades.length > 0 && (
                <button
                  type="button"
                  onClick={handleClearShortlist}
                  className="px-2.5 py-1.5 rounded-lg bg-rose-500/15 hover:bg-rose-500/25 text-rose-300 hover:text-rose-200 border border-rose-500/40 font-semibold text-xs transition-all flex items-center gap-1 ml-auto"
                  title="Clear all tracked setups and surveillance history"
                >
                  <Trash2 className="w-3.5 h-3.5 text-rose-400" />
                  <span>Clear All</span>
                </button>
              )}
            </div>

            {/* Expandable Custom Levels Grid */}
            {showCustomLevels && (
              <div className="pt-2 border-t border-slate-800/80 grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                <div>
                  <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-0.5">Entry/Trigger</label>
                  <input
                    type="number"
                    step="0.05"
                    value={customEntry}
                    onChange={(e) => setCustomEntry(e.target.value)}
                    placeholder="Auto (Chart)"
                    className="w-full px-2 py-1 bg-slate-950 border border-slate-700/80 rounded-md text-xs text-white placeholder-slate-600 focus:outline-none focus:border-cyan-500 font-mono"
                  />
                </div>
                <div>
                  <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-0.5">Stop Loss</label>
                  <input
                    type="number"
                    step="0.05"
                    value={customSL}
                    onChange={(e) => setCustomSL(e.target.value)}
                    placeholder="Auto (1.5%)"
                    className="w-full px-2 py-1 bg-slate-950 border border-slate-700/80 rounded-md text-xs text-white placeholder-slate-600 focus:outline-none focus:border-cyan-500 font-mono"
                  />
                </div>
                <div>
                  <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-0.5">Target 1</label>
                  <input
                    type="number"
                    step="0.05"
                    value={customT1}
                    onChange={(e) => setCustomT1(e.target.value)}
                    placeholder="Auto (3.0%)"
                    className="w-full px-2 py-1 bg-slate-950 border border-slate-700/80 rounded-md text-xs text-white placeholder-slate-600 focus:outline-none focus:border-cyan-500 font-mono"
                  />
                </div>
                <div>
                  <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-0.5">Target 2</label>
                  <input
                    type="number"
                    step="0.05"
                    value={customT2}
                    onChange={(e) => setCustomT2(e.target.value)}
                    placeholder="Auto (6.0%)"
                    className="w-full px-2 py-1 bg-slate-950 border border-slate-700/80 rounded-md text-xs text-white placeholder-slate-600 focus:outline-none focus:border-cyan-500 font-mono"
                  />
                </div>
              </div>
            )}
          </div>

          {/* ── Active Tracked Setup Cards ── */}
          {isLoading ? (
            <div className="h-64 flex flex-col items-center justify-center text-slate-400 text-xs gap-2">
              <RefreshCw className="w-5 h-5 animate-spin text-cyan-400" />
              <span>Hydrating surveillance state...</span>
            </div>
          ) : trades.length === 0 ? (
            <div className="h-72 border border-dashed border-slate-800 rounded-2xl flex flex-col items-center justify-center p-8 text-center space-y-3">
              <div className="w-12 h-12 rounded-xl bg-slate-900/80 border border-slate-800 flex items-center justify-center text-slate-500">
                <Activity className="w-6 h-6 text-cyan-400/60" />
              </div>
              <div className="max-w-md space-y-1">
                <h3 className="text-sm font-bold text-slate-200">No Active Tracked Setups</h3>
                <p className="text-xs text-slate-400 leading-relaxed">
                  Search any stock above to add to live tracking, or click <strong>"+ Track Setup"</strong> on any analyzed stock in the Chart Analysis desk. Real-time surveillance evaluates entry triggers, targets, and stops every 3 seconds.
                </p>
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {trades.map((t) => {
                const isProfitable = (t.pnl_pct || 0) >= 0;
                const status = t.status || 'WATCHING';
                const activeWatchdog = watchdogPositions.find((p) => p.symbol === t.symbol);
                const isPendingApproval = status === 'PENDING_APPROVAL';
                const isQueued = status === 'QUEUED';
                const triggerPrice = t.entry_price || t.current_price || 0;
                const alloc = settings?.per_trade_allocation || 60000;
                const lev = (t as any).leverage || settings?.mis_leverage || 5.0;
                const estQty = (t as any).quantity && (t as any).quantity > 0
                  ? (t as any).quantity
                  : (triggerPrice > 0 ? Math.floor((alloc * lev) / triggerPrice) : 0);
                
                return (
                  <div
                    key={t.symbol}
                    className="relative rounded-2xl border border-slate-800 bg-gradient-to-b from-slate-900/90 to-slate-950 p-4 shadow-xl hover:border-slate-700 transition-all flex flex-col justify-between space-y-3"
                  >
                    {/* Header: Symbol, Status Badge, Dismiss */}
                    <div className="flex items-start justify-between">
                      <div className="flex items-center gap-2.5">
                        <div className="w-9 h-9 rounded-xl bg-slate-800/80 border border-slate-700 flex items-center justify-center font-black text-xs text-white">
                          {t.symbol.slice(0, 3)}
                        </div>
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-black text-white">{t.symbol}</span>
                            <span className="text-[10px] font-bold px-1.5 py-0.2 rounded bg-cyan-950/80 text-cyan-300 border border-cyan-800/50">
                              {t.setup_type.replace(/_/g, ' ')}
                            </span>
                          </div>
                          <span className="text-[10px] text-slate-400 font-mono">
                            NSE • Conviction: <strong className="text-purple-300">{t.conviction}</strong>
                          </span>
                        </div>
                      </div>

                      {/* Status Badge & Chart Link */}
                      <div className="flex items-center gap-1.5">
                        <span
                          className={`text-[10px] font-black uppercase tracking-wider px-2 py-0.5 rounded-md border flex items-center gap-1 ${
                            status === 'EXIT_ADVISED'
                              ? 'bg-rose-500/30 text-rose-200 border-rose-400 shadow-md shadow-rose-950/70 animate-pulse'
                              : status === 'ACTIVE' || status === 'ACTIVE_IN_TRADE' || activeWatchdog
                              ? activeWatchdog?.is_external
                                ? 'bg-cyan-500/20 text-cyan-200 border-cyan-400/60 shadow-sm shadow-cyan-950'
                                : 'bg-emerald-500/20 text-emerald-300 border-emerald-500/50 shadow-sm shadow-emerald-950/50 animate-pulse'
                              : status === 'TRIGGERED' || status === 'CONFIRMED_TRIGGER' || status === 'SUSTAINED_BREAKOUT'
                              ? 'bg-cyan-500/20 text-cyan-300 border-cyan-500/50 shadow-sm shadow-cyan-950/50 animate-pulse'
                              : isPendingApproval
                              ? 'bg-amber-500/20 text-amber-300 border-amber-500/60 shadow-md shadow-amber-950/40 animate-pulse'
                              : isQueued
                              ? 'bg-purple-500/20 text-purple-300 border-purple-500/50'
                              : status === 'TP1_HIT'
                              ? 'bg-emerald-500/25 text-emerald-300 border-emerald-500/50 shadow-sm shadow-emerald-950'
                              : status === 'TP2_HIT'
                              ? 'bg-emerald-400/30 text-emerald-200 border-emerald-400 shadow-md shadow-emerald-950 font-extrabold'
                              : status === 'SL_HIT' || status === 'STOPPED_OUT'
                              ? 'bg-rose-500/25 text-rose-300 border-rose-500/50'
                              : status === 'CLOSED'
                              ? 'bg-slate-600/25 text-slate-300 border-slate-600/50'
                              : 'bg-amber-500/15 text-amber-300 border-amber-500/40'
                          }`}
                        >
                          <span className={`w-1.5 h-1.5 rounded-full ${
                            status === 'EXIT_ADVISED' ? 'bg-rose-400 animate-ping' : status === 'ACTIVE' || status === 'ACTIVE_IN_TRADE' || activeWatchdog ? (activeWatchdog?.is_external ? 'bg-cyan-400' : 'bg-emerald-400') : status === 'TRIGGERED' || status === 'CONFIRMED_TRIGGER' ? 'bg-cyan-400' : isPendingApproval ? 'bg-amber-400' : status.startsWith('TP') ? 'bg-emerald-400' : status === 'SL_HIT' || status === 'STOPPED_OUT' ? 'bg-rose-400' : 'bg-amber-400'
                          }`} />
                          {isPendingApproval ? 'HITL APPROVAL REQUIRED' : status === 'EXIT_ADVISED' ? '🚨 EXIT ADVISED' : status === 'ACTIVE' && activeWatchdog ? (activeWatchdog.is_external ? '🛡️ ADVISORY ONLY (MANUAL)' : '🛡️ LIVE WATCHDOG') : status.replace(/_/g, ' ')}
                        </span>

                        {onOpenChart && (
                          <button
                            onClick={() => onOpenChart(t.symbol)}
                            className="p-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white transition-all"
                            title="Open in Charts & Order Flow"
                          >
                            <ExternalLink className="w-3.5 h-3.5" />
                          </button>
                        )}

                        <button
                          onClick={() => handleDeleteTrade(t.symbol)}
                          className="p-1 rounded bg-slate-800/60 hover:bg-rose-900/40 text-slate-400 hover:text-rose-300 transition-all"
                          title="Remove from live tracking"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>

                    {/* Live Telemetry Bar: LTP + PnL */}
                    <div className="p-2.5 rounded-xl bg-slate-950/80 border border-slate-800/80 flex items-center justify-between">
                      <div>
                        <div className="text-[10px] text-slate-400 uppercase font-semibold">Live Price (LTP)</div>
                        <div className="text-lg font-black text-white tracking-tight">
                          ₹{(t.current_price || t.entry_price).toFixed(2)}
                        </div>
                      </div>

                      <div className="text-right">
                        <div className="text-[10px] text-slate-400 uppercase font-semibold">Unrealized P&L</div>
                        <div className={`text-sm font-black flex items-center gap-0.5 justify-end ${
                          isProfitable ? 'text-emerald-400' : 'text-rose-400'
                        }`}>
                          {isProfitable ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
                          <span>{t.pnl_pct !== undefined && t.pnl_pct !== null ? `${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(2)}%` : '0.00%'}</span>
                        </div>
                      </div>
                    </div>

                    {/* Levels Matrix: Entry, SL, T1, T2, RR */}
                    <div className="grid grid-cols-4 gap-1.5 text-center text-xs">
                      <div className="p-1.5 rounded-lg bg-slate-900/90 border border-slate-800">
                        <div className="text-[9px] text-slate-400 uppercase font-semibold">Trigger Entry</div>
                        <div className="font-mono font-bold text-cyan-300">₹{t.entry_price?.toFixed(2)}</div>
                      </div>
                      <div className="p-1.5 rounded-lg bg-slate-900/90 border border-slate-800">
                        <div className="text-[9px] text-slate-400 uppercase font-semibold">Stop Loss</div>
                        <div className="font-mono font-bold text-rose-400">₹{t.stop_loss?.toFixed(2)}</div>
                      </div>
                      <div className="p-1.5 rounded-lg bg-slate-900/90 border border-slate-800">
                        <div className="text-[9px] text-slate-400 uppercase font-semibold">Target 1</div>
                        <div className="font-mono font-bold text-emerald-400">₹{t.target_1?.toFixed(2)}</div>
                      </div>
                      <div className="p-1.5 rounded-lg bg-slate-900/90 border border-slate-800">
                        <div className="text-[9px] text-slate-400 uppercase font-semibold">Target 2</div>
                        <div className="font-mono font-bold text-emerald-300">₹{t.target_2?.toFixed(2)}</div>
                      </div>
                    </div>

                    {/* ── 1-Click HITL Interactive Approval Box ── */}
                    {isPendingApproval && (
                      <div className="p-3 rounded-xl bg-gradient-to-r from-cyan-950/80 via-blue-950/60 to-slate-900 border border-cyan-500/50 space-y-2.5 shadow-lg shadow-cyan-950/40">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-1.5 text-cyan-300 font-bold text-xs">
                            <Zap className="w-3.5 h-3.5 text-cyan-400 animate-pulse" />
                            <span>1-CLICK TRADE APPROVAL REQUIRED</span>
                          </div>
                          <span className="text-[10px] font-mono text-cyan-400 bg-cyan-900/60 px-2 py-0.5 rounded border border-cyan-700/60">
                            ₹{((settings?.per_trade_allocation || 60000) / 1000).toFixed(0)}k MIS ({lev}x) • ~{estQty} Shares
                          </span>
                        </div>

                        <div className="text-[11px] text-slate-300 leading-tight">
                          Sustained breakout confirmed. Requires manual execution clearance. Max 3 concurrent slots.
                        </div>

                        <div className="grid grid-cols-2 gap-2 pt-1">
                          <button
                            onClick={() => handleApproveTrade(t.symbol, triggerPrice, t.security_id)}
                            disabled={actionLoading[t.symbol]}
                            className="px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-black text-xs shadow-md shadow-emerald-950/50 active:scale-95 transition-all flex items-center justify-center gap-1.5 disabled:opacity-50"
                          >
                            <Check className="w-3.5 h-3.5" />
                            <span>{actionLoading[t.symbol] ? 'Executing...' : `APPROVE (${estQty} shares)`}</span>
                          </button>

                          <button
                            onClick={() => handleDeclineTrade(t.symbol)}
                            disabled={actionLoading[t.symbol]}
                            className="px-3 py-2 rounded-lg bg-rose-600/80 hover:bg-rose-500 text-white font-bold text-xs shadow-md shadow-rose-950/50 active:scale-95 transition-all flex items-center justify-center gap-1.5 disabled:opacity-50"
                          >
                            <X className="w-3.5 h-3.5" />
                            <span>DECLINE</span>
                          </button>
                        </div>

                        <div className="text-[10px] text-slate-400 flex items-center justify-between pt-0.5">
                          <span>Auto-purges if price slips &gt;0.20%</span>
                          <span className="text-cyan-400/80">Telegram Synced</span>
                        </div>
                      </div>
                    )}

                    {/* ── Active Watchdog Quant Telemetry Box ── */}
                    {activeWatchdog && (
                      <div className="p-3 rounded-xl bg-slate-900/90 border border-emerald-500/50 space-y-2 shadow-lg shadow-emerald-950/30">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-1.5 text-emerald-300 font-black text-xs tracking-wide">
                            <span className="w-2 h-2 rounded-full bg-emerald-400 animate-ping" />
                            <span>LIVE QUANT WATCHDOG ACTIVE</span>
                          </div>
                          <span className="text-[10px] font-mono text-emerald-400 bg-emerald-950/80 px-2 py-0.5 rounded border border-emerald-800">
                            ₹60k MIS • {activeWatchdog.quantity} Qty
                          </span>
                        </div>

                        <div className="grid grid-cols-3 gap-1.5 text-center text-[10px]">
                          <div className="p-1.5 rounded-lg bg-slate-950/80 border border-rose-900/50">
                            <div className="text-slate-400 uppercase font-semibold">1% Hard SL</div>
                            <div className="font-mono font-bold text-rose-400">₹{activeWatchdog.hard_stop_loss.toFixed(2)}</div>
                          </div>
                          <div className="p-1.5 rounded-lg bg-slate-950/80 border border-amber-900/50">
                            <div className="text-slate-400 uppercase font-semibold">Dynamic Trail</div>
                            <div className="font-mono font-bold text-amber-300">₹{activeWatchdog.current_stop_loss.toFixed(2)}</div>
                          </div>
                          <div className="p-1.5 rounded-lg bg-slate-950/80 border border-emerald-900/50">
                            <div className="text-slate-400 uppercase font-semibold">Breakeven Lock</div>
                            <div className={`font-mono font-bold ${activeWatchdog.breakeven_locked ? 'text-emerald-400' : 'text-slate-400'}`}>
                              {activeWatchdog.breakeven_locked ? 'LOCKED (+0.1%)' : 'ARMED (+0.8%)'}
                            </div>
                          </div>
                        </div>

                        <div className="flex items-center justify-between text-[10px] text-slate-300 pt-0.5">
                          <span className="flex items-center gap-1">
                            <Shield className="w-3 h-3 text-cyan-400" />
                            {activeWatchdog.is_runner_mode ? '🏃 Runner Trail: VWAP/20-EMA' : '🎯 Target 1 Phase'}
                          </span>
                          <span className={`font-mono font-bold ${activeWatchdog.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                            Live PnL: ₹{activeWatchdog.pnl.toFixed(2)} ({activeWatchdog.pnl_pct >= 0 ? '+' : ''}{activeWatchdog.pnl_pct.toFixed(2)}%)
                          </span>
                        </div>
                      </div>
                    )}

                    {/* ── Queued State Display ── */}
                    {isQueued && (
                      <div className="p-2.5 rounded-xl bg-amber-950/40 border border-amber-500/40 text-amber-200 text-xs flex items-center gap-2">
                        <Zap className="w-4 h-4 text-amber-400 shrink-0 animate-bounce" />
                        <div className="text-[11px] leading-tight">
                          <strong>Priority Queued:</strong> All 3 capital slots full (₹1.80L deployed). Will auto-fire ₹60k MIS order upon next slot vacancy.
                        </div>
                      </div>
                    )}

                    {/* Metrics Row: Risk/Reward, 5m RSI & RVOL Tags */}
                    {(() => {
                      const rsiVal = t.rsi_5m ?? (() => {
                        const match = t.tape_evidence?.match(/5m RSI\s+([\d.]+)/i);
                        return match ? parseFloat(match[1]) : null;
                      })();
                      const rvolVal = t.rvol ?? (() => {
                        const match = t.tape_evidence?.match(/RVOL\s+([\d.]+)x/i);
                        return match ? parseFloat(match[1]) : null;
                      })();
                      const isRsiOB = rsiVal != null && rsiVal >= 73;
                      const isRsiCaution = rsiVal != null && rsiVal >= 65 && rsiVal < 73;

                      return (
                        <div className="flex items-center justify-between text-[11px] text-slate-300 px-1 pt-1 border-t border-slate-800/60">
                          <div className="flex items-center gap-1.5">
                            <Target className="w-3.5 h-3.5 text-emerald-400" />
                            <span>R:R <strong>1:{t.risk_reward_ratio || 2.5}</strong></span>
                          </div>

                          <div className="flex items-center gap-1.5 font-mono text-[10px]">
                            {rsiVal != null ? (
                              <span
                                title={isRsiOB ? '5m RSI ≥ 73 (Overbought). Retracement risk.' : isRsiCaution ? '5m RSI 65-73 (Caution zone)' : '5m RSI healthy (<65)'}
                                className={`px-1.5 py-0.5 rounded border font-bold ${
                                  isRsiOB
                                    ? 'bg-rose-500/20 text-rose-300 border-rose-500/50'
                                    : isRsiCaution
                                    ? 'bg-amber-500/20 text-amber-300 border-amber-500/50'
                                    : 'bg-emerald-950/60 text-emerald-300 border-emerald-800/60'
                                }`}
                              >
                                5m RSI: {rsiVal.toFixed(1)}{isRsiOB ? ' ⚠️' : ''}
                              </span>
                            ) : (
                              <span className="px-1.5 py-0.5 rounded border bg-slate-800/60 text-slate-400 border-slate-700">
                                5m RSI: Active
                              </span>
                            )}

                            {rvolVal != null && (
                              <span
                                title={`Relative Volume: ${rvolVal.toFixed(2)}x`}
                                className={`px-1.5 py-0.5 rounded border font-bold ${
                                  rvolVal >= 2.0
                                    ? 'bg-purple-950/60 text-purple-300 border-purple-800/60'
                                    : rvolVal >= 1.0
                                    ? 'bg-cyan-950/60 text-cyan-300 border-cyan-800/60'
                                    : 'bg-slate-800/60 text-slate-400 border-slate-700'
                                }`}
                              >
                                RVOL: {rvolVal.toFixed(1)}x
                              </span>
                            )}
                          </div>
                        </div>
                      );
                    })()}

                    {/* FinBERT Catalysts & Trade Thesis */}
                    <div className="p-2.5 rounded-xl bg-slate-900/50 border border-slate-800/80 space-y-1.5 text-[11px]">
                      {t.catalyst_summary && (
                        <div className="text-slate-300 line-clamp-2">
                          <strong className="text-amber-300">Catalyst (24h):</strong> {t.catalyst_summary}
                        </div>
                      )}
                      {t.thesis && (
                        <div className="text-slate-300 leading-snug line-clamp-3">
                          <strong className="text-cyan-300">Trade Thesis:</strong> {t.thesis}
                        </div>
                      )}
                      {t.invalidation_rule && (
                        <div className="text-[10px] text-rose-300/80 font-mono">
                          Invalidation: {t.invalidation_rule}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* ── RIGHT / LOWER: Chronological Timeline Log Panel (40% width) ───── */}
        <div className="w-full md:w-[380px] lg:w-[420px] shrink-0 flex flex-col min-h-0 bg-slate-900/40">
          {/* Timeline Header & Filter Pills */}
          <div className="p-3 border-b border-slate-800/90 flex flex-col gap-2 shrink-0">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Clock className="w-4 h-4 text-cyan-400" />
                <h3 className="text-xs font-bold text-white uppercase tracking-wider">
                  Timeline Stream (IST)
                </h3>
              </div>
              <span className="text-[10px] font-mono text-slate-400">
                {filteredEvents.length} events
              </span>
            </div>

            {/* Filter Pills */}
            <div className="flex items-center gap-1 text-[10px]">
              {(['all', 'triggers', 'targets', 'risk'] as const).map((filter) => (
                <button
                  key={filter}
                  onClick={() => setTimelineFilter(filter)}
                  className={`px-2 py-0.5 rounded-md uppercase font-bold tracking-wider transition-all ${
                    timelineFilter === filter
                      ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/40'
                      : 'bg-slate-800/70 text-slate-400 hover:text-slate-200 border border-transparent'
                  }`}
                >
                  {filter}
                </button>
              ))}
            </div>
          </div>

          {/* Timeline Event Feed */}
          <div className="flex-1 overflow-y-auto custom-scrollbar p-3 space-y-2.5">
            {filteredEvents.length === 0 ? (
              <div className="h-48 flex flex-col items-center justify-center text-slate-500 text-xs text-center p-4">
                <Clock className="w-6 h-6 mb-2 opacity-50" />
                <span>No surveillance events logged yet.</span>
                <span className="text-[10px] text-slate-600 mt-1">
                  Micro-events will append automatically as price ticks arrive.
                </span>
              </div>
            ) : (
              filteredEvents.map((e) => {
                const isAdvisorySL = e.event_type === 'MANUAL_ADVISORY_SL' || e.event_type === 'AUTO_SL_MODIFIED' || e.message?.includes('SET NEW STOP LOSS') || e.message?.includes('stop loss');
                const isAdvisoryExit = e.event_type === 'MANUAL_ADVISORY_EXIT' || e.event_type === 'HARD_SL_HIT' || e.message?.includes('EXIT NOW') || e.message?.includes('Exit recommended');
                const isCashOut = e.event_type === 'TRAILED_STOP_HIT' || e.message?.includes('cash out');
                const isTakeProfit = e.event_type === 'TAKE_PROFIT' || e.message?.includes('Take your profits');
                const isDump = e.event_type === 'HUGE_SELLOFF_ALERT' || e.message?.includes('sell-off') || e.message?.includes('dump');
                const isInflow = e.event_type === 'INSTITUTIONAL_INFLOW' || e.message?.includes('institutional buying');
                const isBuyWall = e.event_type === 'MASSIVE_BUY_WALL' || e.message?.includes('buy wall');
                const isSellWall = e.event_type === 'MASSIVE_SELL_WALL' || e.message?.includes('sell wall');
                const isBookShift = e.event_type === 'ORDERBOOK_SHIFT' || e.message?.includes('Order book just flipped');
                const isTrigger = e.event_type === 'ENTRY_TRIGGERED' || e.event_type === 'CONFIRMED_TRIGGER' || e.event_type === 'SUSTAINED_BREAKOUT' || e.event_type === 'MANUAL_TRADE_ARMED';
                const isTarget = e.event_type === 'TP1_HIT' || e.event_type === 'TP2_HIT';
                const isRisk = e.event_type === 'SL_HIT' || e.event_type === 'STOPPED_OUT';

                return (
                  <div
                    key={e.id}
                    className={`p-2.5 rounded-xl border space-y-1.5 shadow-sm transition-all text-xs ${
                      isAdvisoryExit
                        ? 'border-rose-500 bg-rose-950/70 shadow-rose-950/60 animate-pulse'
                        : isAdvisorySL
                        ? 'border-amber-500 bg-amber-950/60 shadow-amber-950/50'
                        : isDump
                        ? 'border-rose-600/70 bg-rose-950/50'
                        : isInflow
                        ? 'border-emerald-500/70 bg-emerald-950/40'
                        : isBuyWall
                        ? 'border-cyan-500/70 bg-cyan-950/40'
                        : isSellWall
                        ? 'border-amber-600/70 bg-amber-950/40'
                        : isBookShift
                        ? 'border-purple-600/70 bg-purple-950/40'
                        : isTakeProfit || isCashOut
                        ? 'border-emerald-500/70 bg-emerald-950/50'
                        : 'border-slate-800/90 bg-slate-950/70 hover:border-slate-700'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-1.5">
                        <span className="font-bold text-white text-[11px] px-1.5 py-0.2 rounded bg-slate-800 border border-slate-700">
                          {e.symbol}
                        </span>
                        <span
                          className={`text-[9px] font-black uppercase tracking-wider px-1.5 py-0.2 rounded ${
                            isAdvisoryExit
                              ? 'bg-rose-600 text-white font-black animate-pulse'
                              : isAdvisorySL
                              ? 'bg-amber-500 text-black font-black'
                              : isDump
                              ? 'bg-rose-900 text-rose-200 border border-rose-600/60'
                              : isInflow
                              ? 'bg-emerald-900 text-emerald-200 border border-emerald-500/60'
                              : isBuyWall
                              ? 'bg-cyan-900 text-cyan-200 border border-cyan-500/60'
                              : isSellWall
                              ? 'bg-amber-900 text-amber-200 border border-amber-600/60'
                              : isBookShift
                              ? 'bg-purple-900 text-purple-200 border border-purple-500/60'
                              : isTakeProfit
                              ? 'bg-emerald-600 text-white font-black'
                              : isCashOut
                              ? 'bg-amber-600 text-white font-black'
                              : isTrigger
                              ? 'bg-cyan-950 text-cyan-300 border border-cyan-800/60'
                              : isTarget
                              ? 'bg-emerald-950 text-emerald-300 border border-emerald-800/60'
                              : isRisk
                              ? 'bg-rose-950 text-rose-300 border border-rose-800/60'
                              : 'bg-slate-800 text-slate-300'
                          }`}
                        >
                          {isAdvisoryExit
                            ? '🚨 EXIT NOW'
                            : isAdvisorySL
                            ? '📢 MOVE STOP LOSS'
                            : isDump
                            ? '⚠️ HUGE SELL-OFF'
                            : isInflow
                            ? '🔥 INSTITUTIONAL BUY'
                            : isBuyWall
                            ? '🧱 BIG BUY WALL'
                            : isSellWall
                            ? '⚠️ BIG SELL WALL'
                            : isBookShift
                            ? '⚡ ORDER BOOK SHIFT'
                            : isTakeProfit
                            ? '💰 TAKE PROFIT'
                            : isCashOut
                            ? '🛑 CASH OUT'
                            : e.event_type.replace(/_/g, ' ')}
                        </span>
                      </div>
                      <span className="font-mono text-[10px] text-slate-400">
                        {e.timestamp_ist}
                      </span>
                    </div>

                    <p className={`text-[11px] leading-snug ${
                      isAdvisoryExit || isDump ? 'text-rose-100 font-bold' :
                      isAdvisorySL ? 'text-amber-100 font-semibold' :
                      isInflow || isTakeProfit ? 'text-emerald-100 font-semibold' :
                      isBuyWall ? 'text-cyan-100 font-semibold' :
                      isSellWall ? 'text-amber-100 font-semibold' :
                      'text-slate-300'
                    }`}>
                      {e.message}
                    </p>

                    {e.price_at_event && (
                      <div className="text-[10px] font-mono text-slate-400 pt-0.5">
                        Price: <strong className="text-white">₹{e.price_at_event.toFixed(2)}</strong>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>

      {/* ── Watchdog Risk & Capital Settings Modal ── */}
      {isSettingsOpen && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 overflow-y-auto">
          <div className="bg-slate-900 border border-slate-700 rounded-2xl w-full max-w-xl shadow-2xl overflow-hidden flex flex-col my-8">
            {/* Modal Header */}
            <div className="px-5 py-3.5 border-b border-slate-800 bg-slate-950/70 flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className="w-8 h-8 rounded-lg bg-cyan-500/15 border border-cyan-500/30 flex items-center justify-center text-cyan-400">
                  <Sliders className="w-4 h-4" />
                </div>
                <div>
                  <h3 className="text-sm font-black text-white">Live Watchdog & Capital Settings</h3>
                  <p className="text-[10px] text-slate-400">Configure sizing, max concurrency, and risk boundaries</p>
                </div>
              </div>
              <button
                onClick={() => setIsSettingsOpen(false)}
                className="p-1 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800 transition-all"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Modal Body */}
            <div className="p-5 space-y-4 max-h-[75vh] overflow-y-auto custom-scrollbar text-xs">
              
              {/* 1. Master Execution Killswitch */}
              <div className="p-3.5 rounded-xl border border-slate-800 bg-slate-950/60 space-y-2">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="font-bold text-white flex items-center gap-1.5">
                      <Power className="w-3.5 h-3.5 text-cyan-400" />
                      <span>Master Execution Switch</span>
                    </div>
                    <p className="text-[10px] text-slate-400">
                      Toggle ON/OFF before orders reach Telegram alerts & Dhan MCP placement.
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={handleToggleExecution}
                    disabled={isTogglingExecution}
                    className={`px-3 py-1.5 rounded-lg font-black text-xs transition-all border ${
                      settings?.execution_enabled !== false
                        ? 'bg-emerald-600 hover:bg-emerald-500 text-white border-emerald-400 shadow-md shadow-emerald-950/40'
                        : 'bg-amber-600 hover:bg-amber-500 text-white border-amber-400 shadow-md shadow-amber-950/40'
                    }`}
                  >
                    {settings?.execution_enabled !== false ? '⚡ ARMED (ON)' : '🛡️ STANDBY (OFF)'}
                  </button>
                </div>
              </div>

              {/* 2. Capital & Concurrency Sizing */}
              <div className="space-y-3 pt-1">
                <div className="text-[11px] font-bold text-cyan-300 uppercase tracking-wider flex items-center gap-1.5">
                  <Wallet className="w-3.5 h-3.5" />
                  <span>Capital Allocation & Concurrent Trades</span>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <div>
                    <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-1">
                      Total Desk Capital (₹)
                    </label>
                    <input
                      type="number"
                      value={formTotalCapital}
                      onChange={(e) => setFormTotalCapital(e.target.value)}
                      className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono text-xs focus:outline-none focus:border-cyan-500"
                    />
                  </div>

                  <div>
                    <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-1">
                      Per Trade Amount (₹)
                    </label>
                    <input
                      type="number"
                      value={formPerTrade}
                      onChange={(e) => setFormPerTrade(e.target.value)}
                      className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono text-xs focus:outline-none focus:border-cyan-500"
                    />
                  </div>

                  <div>
                    <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-1">
                      Number of Trades at Time
                    </label>
                    <input
                      type="number"
                      min="1"
                      max="10"
                      value={formMaxSlots}
                      onChange={(e) => setFormMaxSlots(e.target.value)}
                      className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono text-xs focus:outline-none focus:border-cyan-500"
                    />
                  </div>
                </div>

                <div className="p-2.5 rounded-lg bg-slate-950/40 border border-slate-800 text-[11px] text-slate-300 flex items-center justify-between">
                  <span>Slot Sizing Capacity:</span>
                  <span className="font-mono text-cyan-300 font-bold">
                    floor(₹{Number(formPerTrade || 0).toLocaleString('en-IN')} / Trigger) • {formMaxSlots} Trades = ₹{(Number(formPerTrade || 0) * Number(formMaxSlots || 0)).toLocaleString('en-IN')} Capacity
                  </span>
                </div>
              </div>

              {/* 3. Risk Boundaries */}
              <div className="space-y-3 pt-2">
                <div className="text-[11px] font-bold text-rose-300 uppercase tracking-wider flex items-center gap-1.5">
                  <Shield className="w-3.5 h-3.5" />
                  <span>Risk Boundaries & Trailing Rules</span>
                </div>

                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5">
                  <div>
                    <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-1">
                      Max Hard SL (%)
                    </label>
                    <input
                      type="number"
                      step="0.1"
                      value={formMaxSLPercent}
                      onChange={(e) => setFormMaxSLPercent(e.target.value)}
                      className="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-rose-400 font-mono text-xs focus:outline-none focus:border-rose-500"
                    />
                  </div>

                  <div>
                    <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-1">
                      Breakeven Lock (%)
                    </label>
                    <input
                      type="number"
                      step="0.1"
                      value={formBreakevenPercent}
                      onChange={(e) => setFormBreakevenPercent(e.target.value)}
                      className="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-emerald-400 font-mono text-xs focus:outline-none focus:border-emerald-500"
                    />
                  </div>

                  <div>
                    <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-1">
                      Structural Trail (%)
                    </label>
                    <input
                      type="number"
                      step="0.1"
                      value={formTrailPercent}
                      onChange={(e) => setFormTrailPercent(e.target.value)}
                      className="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-amber-300 font-mono text-xs focus:outline-none focus:border-amber-500"
                    />
                  </div>

                  <div>
                    <label className="text-[10px] text-slate-400 uppercase font-semibold block mb-1">
                      Auto-Purge Slip (%)
                    </label>
                    <input
                      type="number"
                      step="0.05"
                      value={formPurgePercent}
                      onChange={(e) => setFormPurgePercent(e.target.value)}
                      className="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-slate-200 font-mono text-xs focus:outline-none focus:border-cyan-500"
                    />
                  </div>
                </div>

                <div className="text-[10px] text-slate-400 flex items-center justify-between px-1">
                  <span>Max Risk Ceiling: <strong className="text-rose-400 font-mono">₹{((Number(formPerTrade || 0) * (Number(formMaxSLPercent || 1) / 100))).toFixed(0)}</strong> / trade</span>
                  <span>Trailing anchors: <strong className="text-cyan-300">5m VWAP & 20-EMA</strong></span>
                </div>
              </div>

              {/* Order Book Depth Intelligence & Microstructure Level */}
              <div className="pt-2 border-t border-slate-800 space-y-2">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-1.5">
                    <Activity className="w-3.5 h-3.5 text-purple-400" />
                    <span className="text-[10px] text-purple-300 uppercase font-bold tracking-wider">
                      Microstructure Depth Intelligence
                    </span>
                  </div>
                  <span className="text-[10px] font-mono text-slate-400">
                    Fallback: <strong className="text-cyan-300">{depthIntelligence?.fallback_chain || '200-Level → 20-Level (Never 5-Level)'}</strong>
                  </span>
                </div>

                <div className="p-3 rounded-lg bg-slate-950 border border-slate-800 space-y-2.5">
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
                    <div className="p-2 rounded bg-slate-900/80 border border-slate-800/80">
                      <div className="text-[10px] text-slate-400 uppercase font-semibold">Watchdog Sockets</div>
                      <div className="font-bold text-white mt-0.5 flex items-center gap-1.5">
                        <span className="w-2 h-2 rounded-full bg-purple-400 animate-pulse" />
                        <span>200-Level Depth Feed</span>
                      </div>
                      <div className="text-[10px] text-slate-500 mt-0.5">1 dedicated socket / active trade (max 3)</div>
                    </div>

                    <div className="p-2 rounded bg-slate-900/80 border border-slate-800/80">
                      <div className="text-[10px] text-slate-400 uppercase font-semibold">Continuous Pool</div>
                      <div className="font-bold text-white mt-0.5 flex items-center gap-1.5">
                        <span className="w-2 h-2 rounded-full bg-cyan-400" />
                        <span>Top {depthIntelligence?.analytical_pool_size || 47} Stocks (20-Level)</span>
                      </div>
                      <div className="text-[10px] text-slate-500 mt-0.5">3 slots reserved for watchdog trades</div>
                    </div>

                    <div className="p-2 rounded bg-slate-900/80 border border-slate-800/80">
                      <div className="text-[10px] text-slate-400 uppercase font-semibold">Active Trades Depth</div>
                      <div className="font-bold text-purple-300 mt-0.5 font-mono">
                        {depthIntelligence?.summary?.active_200depth_connections ?? (watchdogPositions.filter(p => p.depth_level_active === '200-level').length)} Active 200-Depth / {watchdogPositions.length} Trades
                      </div>
                      <div className="text-[10px] text-slate-500 mt-0.5">Instant upgrade upon connection</div>
                    </div>
                  </div>

                  {/* Active Positions Depth Level Table */}
                  {watchdogPositions.length > 0 ? (
                    <div className="space-y-1.5 pt-1 border-t border-slate-900">
                      <div className="text-[10px] uppercase font-bold text-slate-400">Current Position Feeds:</div>
                      <div className="space-y-1">
                        {watchdogPositions.map((pos) => {
                          const level = pos.depth_level_active || '20-level';
                          return (
                            <div key={pos.symbol} className="flex items-center justify-between text-xs px-2.5 py-1.5 rounded bg-slate-900 border border-slate-800">
                              <span className="font-bold text-white font-mono">{pos.symbol}</span>
                              <div className="flex items-center gap-2">
                                <span className={`text-[10px] font-bold px-2 py-0.5 rounded font-mono ${
                                  level === '200-level'
                                    ? 'bg-purple-900/50 text-purple-300 border border-purple-500/40 shadow-[0_0_8px_rgba(168,85,247,0.3)]'
                                    : 'bg-cyan-900/50 text-cyan-300 border border-cyan-500/40'
                                }`}>
                                  {level.toUpperCase()} ORDER BOOK
                                </span>
                                <span className="text-[10px] text-slate-400">Active SL & Exhaustion Engine</span>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  ) : (
                    <div className="text-[11px] text-slate-400 italic">
                      No active trades currently. When a stock enters Watchdog, it automatically claims a dedicated 200-level order book feed.
                    </div>
                  )}
                </div>
              </div>

              {/* 4. Quick Institutional Presets */}
              <div className="pt-2 border-t border-slate-800 space-y-2">
                <span className="text-[10px] text-slate-400 uppercase font-bold tracking-wider">Quick Presets</span>
                <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setFormTotalCapital('180000');
                      setFormPerTrade('60000');
                      setFormMaxSlots('3');
                      setFormMaxSLPercent('1.0');
                      setFormBreakevenPercent('0.8');
                      setFormTrailPercent('1.5');
                      setFormPurgePercent('0.20');
                    }}
                    className="p-2 rounded-lg bg-slate-950 border border-slate-800 hover:border-cyan-500/50 text-left transition-all"
                  >
                    <div className="font-bold text-white text-[11px]">1.80L Standard</div>
                    <div className="text-[10px] text-slate-400">3 trades x 60k • 1% SL</div>
                  </button>

                  <button
                    type="button"
                    onClick={() => {
                      setFormTotalCapital('100000');
                      setFormPerTrade('50000');
                      setFormMaxSlots('2');
                      setFormMaxSLPercent('0.8');
                      setFormBreakevenPercent('0.6');
                      setFormTrailPercent('1.2');
                      setFormPurgePercent('0.15');
                    }}
                    className="p-2 rounded-lg bg-slate-950 border border-slate-800 hover:border-cyan-500/50 text-left transition-all"
                  >
                    <div className="font-bold text-white text-[11px]">1.00L Conservative</div>
                    <div className="text-[10px] text-slate-400">2 trades x 50k • 0.8% SL</div>
                  </button>

                  <button
                    type="button"
                    onClick={() => {
                      setFormTotalCapital('240000');
                      setFormPerTrade('80000');
                      setFormMaxSlots('3');
                      setFormMaxSLPercent('1.2');
                      setFormBreakevenPercent('0.8');
                      setFormTrailPercent('1.5');
                      setFormPurgePercent('0.20');
                    }}
                    className="p-2 rounded-lg bg-slate-950 border border-slate-800 hover:border-cyan-500/50 text-left transition-all"
                  >
                    <div className="font-bold text-white text-[11px]">2.40L High-Conviction</div>
                    <div className="text-[10px] text-slate-400">3 trades x 80k • 1.2% SL</div>
                  </button>
                </div>
              </div>

            </div>

            {/* Modal Footer */}
            <div className="px-5 py-3 border-t border-slate-800 bg-slate-950/70 flex items-center justify-between">
              <button
                type="button"
                onClick={() => setIsSettingsOpen(false)}
                className="px-3.5 py-1.5 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800 text-xs font-semibold transition-all"
              >
                Cancel
              </button>

              <button
                type="button"
                onClick={handleSaveSettings}
                disabled={isSavingSettings}
                className="px-4 py-1.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white font-bold text-xs shadow-md shadow-cyan-950/60 active:scale-95 transition-all flex items-center gap-1.5 disabled:opacity-50"
              >
                <Check className="w-3.5 h-3.5" />
                <span>{isSavingSettings ? 'Saving...' : 'Save Configuration'}</span>
              </button>
            </div>
          </div>
        </div>
      )}
      {/* ── Track Manual Long Stock Modal ── */}
      {isManualModalOpen && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 overflow-y-auto">
          <div className="bg-slate-900 border border-cyan-500/60 rounded-2xl w-full max-w-lg shadow-2xl overflow-hidden flex flex-col my-8">
            <div className="px-5 py-3.5 border-b border-slate-800 bg-slate-950/80 flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className="w-8 h-8 rounded-lg bg-cyan-500/20 border border-cyan-500/40 flex items-center justify-center text-cyan-400">
                  <Shield className="w-4 h-4" />
                </div>
                <div>
                  <h3 className="text-sm font-black text-white">Track Manual Stock (Live Desk)</h3>
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
              {/* Mode Explanation Notice */}
              <div className="p-3 rounded-xl bg-cyan-950/40 border border-cyan-500/40 space-y-1.5 text-slate-300 leading-relaxed text-[11px]">
                <div className="font-bold text-cyan-300 flex items-center gap-1.5 uppercase tracking-wide text-[10px]">
                  <Activity className="w-3.5 h-3.5 text-cyan-400" />
                  <span>Advisory Mode Active (Manual Position)</span>
                </div>
                <div>
                  Watchdog continuously tracks <strong>LTP, 5m VWAP, 20-EMA, RSI, CVD Delta, Deep OBI & Wall detection</strong>.
                  Because this trade is manually added, Watchdog will <strong>NOT</strong> place, modify, or sell/buy on Dhan automatically.
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
                    placeholder="Search or enter any NSE stock (e.g. RELIANCE, TCS, INFY)..."
                    className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono font-bold text-sm focus:outline-none focus:border-cyan-500 uppercase"
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
                          <span className="text-[9px] px-1.5 py-0.5 rounded bg-slate-800 text-cyan-300 border border-slate-700">
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
                    Entry / Trigger Price (₹) <span className="text-rose-400">*</span>
                  </label>
                  <input
                    type="number"
                    step="0.05"
                    value={customEntry}
                    onChange={(e) => setCustomEntry(e.target.value)}
                    placeholder="e.g. 2450.00"
                    className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded-lg text-white font-mono font-bold text-xs focus:outline-none focus:border-cyan-500"
                  />
                </div>

                {/* Stop Loss */}
                <div>
                  <label className="text-[10px] text-slate-300 uppercase font-bold block mb-1">
                    Stop Loss (SL) (₹) <span className="text-rose-400">*</span>
                  </label>
                  <input
                    type="number"
                    step="0.05"
                    value={customSL}
                    onChange={(e) => setCustomSL(e.target.value)}
                    placeholder="Must be < Entry"
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
                    Target 1 (₹) <span className="text-slate-500 font-normal">(Optional)</span>
                  </label>
                  <input
                    type="number"
                    step="0.05"
                    value={customT1}
                    onChange={(e) => setCustomT1(e.target.value)}
                    placeholder="e.g. 2520.00"
                    className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded-lg text-emerald-300 font-mono text-xs focus:outline-none focus:border-emerald-500"
                  />
                </div>
              </div>

              {/* Real-time Sizing & System Allocation Card */}
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
                    ₹{Math.max(0, ((parseFloat(customEntry) || 0) - (parseFloat(customSL) || 0)) * (parseInt(customQty, 10) || 0)).toFixed(1)}
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
                className="px-4 py-1.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white font-bold text-xs shadow-md shadow-cyan-950/60 active:scale-95 transition-all flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
              >
                <Check className="w-3.5 h-3.5" />
                <span>{isAddingManual ? 'Arming Watchdog...' : 'Arm Manual Position Surveillance'}</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

