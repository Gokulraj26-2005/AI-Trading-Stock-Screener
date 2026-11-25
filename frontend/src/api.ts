// frontend/src/api.ts
import axios from 'axios';

const STORAGE_KEY = 'via_session_token';

let sessionToken: string = typeof window !== 'undefined' ? localStorage.getItem(STORAGE_KEY) || '' : '';

export const api = axios.create({
  baseURL: '/api',
  withCredentials: true,
});

/**
 * Global request interceptor to inject the session token into protected API routes.
 */
api.interceptors.request.use((config) => {
  const effectiveToken = sessionToken || (typeof window !== 'undefined' && localStorage.getItem('dev_bypass_auth') === 'true' ? 'dev_preview_mock_token' : '');
  if (effectiveToken && !config.headers.Authorization) {
    // Basic heuristic: public login/exchange/status routes don't get the bearer token
    const url = config.url || '';
    const isPublic = url.includes('/broker/status') || url.includes('/auth/session-token');
    
    if (!isPublic) {
      config.headers.Authorization = `Bearer ${effectiveToken}`;
    }
  }
  return config;
});

export const getSessionToken = () => {
  if (!sessionToken && typeof window !== 'undefined') {
    sessionToken = localStorage.getItem(STORAGE_KEY) || '';
  }
  if (!sessionToken && typeof window !== 'undefined' && localStorage.getItem('dev_bypass_auth') === 'true') {
    return 'dev_preview_mock_token';
  }
  return sessionToken;
};

export const setSessionToken = (token: string | null) => {
  sessionToken = (token || '').trim();
  if (typeof window !== 'undefined') {
    if (sessionToken) {
      localStorage.setItem(STORAGE_KEY, sessionToken);
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
  }
  return sessionToken;
};

export const clearSessionToken = () => {
  sessionToken = '';
  if (typeof window !== 'undefined') {
    localStorage.removeItem(STORAGE_KEY);
  }
};

/**
 * Auth Router Endpoints
 */

export const createSessionToken = async (params: { bootstrapKey?: string; authorizationToken?: string }) => {
  const headers: Record<string, string> = {};
  if (params.bootstrapKey) {
    headers['X-Session-Bootstrap-Key'] = params.bootstrapKey.trim();
  }
  if (params.authorizationToken) {
    headers['Authorization'] = `Bearer ${params.authorizationToken.trim()}`;
  }

  const response = await api.post('/auth/session-token', {}, { headers, withCredentials: true });
  if (response.data?.access_token) {
    setSessionToken(response.data.access_token);
  }
  return response.data;
};

/**
 * Broker Endpoints
 */

export const setMockMode = async (enable: boolean = true) => {
  try {
    const response = await api.post('/broker/mock-mode', { enable });
    return response.data;
  } catch (_e) {
    return { status: 'mock_mode_fallback' };
  }
};

export const getBrokerStatus = async () => {
  try {
    const response = await api.get('/broker/status');
    if (response.data) {
      return response.data;
    }
  } catch (_e) {
    // fallback
  }
  return { has_access_token: false, configured: false };
};

export const getDhanLoginUrl = async () => {
  const response = await api.get('/dhan/login-url');
  return response.data as { login_url: string };
};

export const consumeDhanToken = async (tokenId: string) => {
  const response = await api.post('/dhan/consume-token', { token_id: tokenId });
  return response.data;
};

export const logoutBrokerSession = async () => {
  try {
    const response = await api.post('/broker/logout');
    clearSessionToken();
    return response.data;
  } catch {
    clearSessionToken();
    return { status: 'logged_out' };
  }
};

/**
 * Pro Chart & Orderflow Endpoints
 */

export interface ChartSearchMatch {
  symbol: string;
  security_id: string;
  name: string;
  segment: string;
  priority: number;
}

export interface ChartCandle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface ChartIndicatorPoint {
  time: number;
  value: number;
  color?: string;
}

export interface WallItem {
  price: number;
  qty: number;
  side: 'bid' | 'ask';
  strength: number;
  orders?: number | null;
  is_absorption?: boolean;
}

export interface ChartPayload {
  symbol: string;
  token: string;
  interval: string;
  last_price: number;
  change_pct: number;
  day_high: number;
  day_low: number;
  vwap: number;
  candles: ChartCandle[];
  indicators: {
    vwap: ChartIndicatorPoint[];
    vwap_upper_1: ChartIndicatorPoint[];
    vwap_lower_1: ChartIndicatorPoint[];
    vwap_upper_2: ChartIndicatorPoint[];
    vwap_lower_2: ChartIndicatorPoint[];
    vwap_upper_3: ChartIndicatorPoint[];
    vwap_lower_3: ChartIndicatorPoint[];
    volume_delta: ChartIndicatorPoint[];
    cvd: ChartIndicatorPoint[];
  };
  order_flow: {
    running_cvd: number;
    deep_obi: number;
    institutional_walls: WallItem[];
  };
  quant_metrics: {
    signal: string;
    confidence: number;
    state: string;
    regime: string;
    c_value: number;
    vcp_confirmed: boolean;
    ewma_atr_rs: number;
    expected_move_1atr: number;
    expected_move_2atr: number;
    expected_high: number;
    expected_low: number;
    levels: {
      entry: number;
      sl: number;
      target1: number;
      target2: number;
      rr_ratio: number;
      kelly_qty: number;
    };
  };
}

export const searchChartSymbols = async (query: string): Promise<ChartSearchMatch[]> => {
  const response = await api.get('/chart/search', { params: { q: query } });
  return (response.data?.matches || []) as ChartSearchMatch[];
};

export const getChartData = async (
  symbol: string,
  interval: string = '5m'
): Promise<ChartPayload> => {
  const params: Record<string, any> = { interval };
  const response = await api.get(`/chart/${encodeURIComponent(symbol)}`, { params });
  return response.data as ChartPayload;
};

// ── Multi-Timeframe Chart Analysis API ────────────────────────────────────────

export interface AnalysisSymbolItem {
  symbol: string;
  security_id: string;
  name: string;
  segment: string;
}

export interface CandlePoint {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface LinePoint {
  time: number;
  value: number;
}

export interface ChartMarkerItem {
  time: number;
  position: 'aboveBar' | 'belowBar' | 'inBar';
  color: string;
  shape: 'arrowUp' | 'arrowDown' | 'circle';
  text: string;
}

export interface PriceLineItem {
  price: number;
  label: string;
  color: string;
  lineStyle: 'solid' | 'dashed' | 'dotted';
  lineWidth: number;
}

export interface InstitutionalFlowDetail {
  score: number; // -100 to +100
  classification: 'STRONG_ACCUMULATION' | 'ACCUMULATION' | 'NEUTRAL' | 'DISTRIBUTION' | 'STRONG_DISTRIBUTION';
  confidence: number; // 0 to 100
  coverage: number; // 0 to 100
  trend: {
    trend_5d: number;
    trend_20d: number;
    persistence_10d: number;
  };
  components: {
    volume: number | null;
    cvd: number | null;
    large_trades: number | null;
    depth: number | null;
    delivery: number | null;
    fno_oi: number | null;
    price_acceptance: number | null;
  };
  signals: string[];
  conflicts: string[];
  stealth_phase: 'STEALTH_ACCUMULATION' | 'STEALTH_DISTRIBUTION' | null;
  summary: string;
}

export interface FinBERTHeadlineItem {
  title: string;
  source: string;
  hours_ago: number;
  sentiment: 'BULLISH' | 'NEUTRAL' | 'BEARISH';
  score: number;
  is_regulatory?: boolean;
  prob_positive?: number;
  prob_negative?: number;
  prob_neutral?: number;
}

export interface NarrativeAlignmentDetail {
  state: string;
  badge: string;
  type: 'success' | 'danger' | 'warning' | 'info' | 'neutral';
  detail: string;
}

export interface FinBERTSentimentDetail {
  sentiment_score: number;
  sentiment_label: 'STRONG_BULLISH' | 'BULLISH' | 'NEUTRAL' | 'BEARISH' | 'STRONG_BEARISH';
  probabilities: {
    positive: number;
    neutral: number;
    negative: number;
  };
  article_count: number;
  headlines: FinBERTHeadlineItem[];
  narrative_alignment?: NarrativeAlignmentDetail;
  summary: string;
  scanned_at?: string;
  lookback_window?: string;
}

export interface TechnicalCommentaryData {
  structure_base: string;
  multi_tf_trend: string;
  indicator_interaction: string;
  volatility_state: string;
  actionable_thesis: string;
  bias: 'BULLISH_BREAKOUT' | '50EMA_BOUNCE' | 'MONTHLY_VWAP_TEST' | 'BEARISH_BREAKDOWN' | 'SQUEEZE_COIL' | 'NEUTRAL_CHOP';
  potential_score: number;
  quality_tier: 'A+' | 'A' | 'B' | 'C';
  trigger_price: number | null;
  target_price_1: number | null;
  target_price_2: number | null;
  stop_loss: number | null;
  latest_news: string;
  institutional_flow: string;
  institutional_flow_detail?: InstitutionalFlowDetail | null;
  sentiment_detail?: FinBERTSentimentDetail | null;
  narrative_alignment?: NarrativeAlignmentDetail | null;
}

export interface SymbolAnalysisPayload {
  symbol: string;
  current_price: number;
  price?: number;
  prev_close: number;
  change_pct: number;
  timeframes: {
    '1D': {
      candles: CandlePoint[];
      ema50: LinePoint[];
      sma50: LinePoint[];
      sma200: LinePoint[];
      avwap_month: LinePoint[];
      avwap_week: LinePoint[];
    };
    '1W': {
      candles: CandlePoint[];
      ema20?: LinePoint[];
      ema50?: LinePoint[];
      sma50?: LinePoint[];
      sma100?: LinePoint[];
      avwap?: LinePoint[];
    };
    '1M': {
      candles: CandlePoint[];
      ema10?: LinePoint[];
      ema20?: LinePoint[];
      sma20?: LinePoint[];
      avwap?: LinePoint[];
    };
  };
  consolidation: {
    is_consolidating: boolean;
    resistance: number;
    support: number;
    range_span_pct: number;
    duration_bars: number;
    start_time: number;
    end_time: number;
    containment_ratio: number;
  };
  ma_status: {
    touching_ema50: boolean;
    bouncing_ema50: boolean;
    dist_ema50_pct: number;
    touching_avwap: boolean;
    bouncing_avwap: boolean;
    dist_avwap_pct: number;
    confluence_shelf: boolean;
    confluence_level: number | null;
  };
  metrics: {
    ema50: number | null;
    sma50: number | null;
    sma200: number | null;
    avwap_month: number | null;
    squeeze_percentile: number | null;
    atr14: number | null;
    weekly_trend: string;
  };
  commentary: TechnicalCommentaryData;
  markers: ChartMarkerItem[];
  price_lines: PriceLineItem[];
  analyzed_at: string;
}

export interface IntradayAnalysisPayload {
  symbol: string;
  mode: 'intraday';
  timeframe: '5m';
  current_price: number;
  vwap: number;
  bars_count: number;
  candles_5m: CandlePoint[];
  vwap_series: LinePoint[];
  analyzed_at: string;
}

export const getAnalysisSymbols = async (universe: string = 'all'): Promise<{ universe: string; count: number; symbols: AnalysisSymbolItem[] }> => {
  const response = await api.get('/analysis/symbols', { params: { universe } });
  return response.data;
};

/**
 * Search ALL NSE-listed equities from the Dhan instrument CSV.
 * Not limited to the predefined 637-stock universe — returns any matching NSE stock.
 */
export const searchNseSymbols = async (
  query: string,
  limit: number = 15,
): Promise<{ status: string; query: string; count: number; symbols: AnalysisSymbolItem[] }> => {
  const response = await api.get('/analysis/search-nse', { params: { q: query, limit } });
  return response.data;
};

export const getSymbolAnalysis = async (
  symbol: string,
  options?: {
    mode?: 'swing' | 'intraday';
    force_refresh?: boolean;
  }
): Promise<SymbolAnalysisPayload> => {
  const params: Record<string, any> = {};
  if (options?.mode) params.mode = options.mode;
  if (options?.force_refresh) params.force_refresh = options.force_refresh;
  const response = await api.get(`/analysis/${encodeURIComponent(symbol)}`, { params });
  return response.data as SymbolAnalysisPayload;
};

export const getIntradayAnalysis = async (
  symbol: string
): Promise<IntradayAnalysisPayload> => {
  const response = await api.get(`/analysis/intraday/${encodeURIComponent(symbol)}`);
  return response.data as IntradayAnalysisPayload;
};

export interface LiveTickItem {
  ltp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  change_pct: number;
  volume: number;
  rsi_5m: number | null;
  rvol: number | null;
  source?: string;
  observed_at?: number;
  quote_age_ms?: number;
  rsi_is_real?: boolean;
}

export interface LiveTicksResponse {
  status: string;
  timestamp: string;
  ticks: Record<string, LiveTickItem>;
  trigger_states?: Record<string, { status: string; sustain_seconds: number; slip_count: number; correlation_id: string; event?: any }>;
  trigger_events?: Array<{ sequence_id: number; event_id: string; symbol: string; status: string; correlation_id: string; observed_at: number }>;
  last_sequence_id?: number;
}

export const getLiveTicks = async (
  symbols: string[],
  trigger_specs: Array<{ symbol: string; trigger_price: number; stop_loss?: number; target_1?: number; target_2?: number; direction?: 'BULL' | 'BEAR'; correlation_id?: string }> = [],
  after_sequence_id: number = 0,
): Promise<LiveTicksResponse> => {
  const response = await api.post('/analysis/live-ticks', { symbols, trigger_specs, after_sequence_id }, { timeout: 10000 });
  return response.data as LiveTicksResponse;
};

// ── AI Shortlist & Surveillance Types & Endpoints ─────────────────────────

export interface ShortlistedTrade {
  symbol: string;
  action: 'BUY' | 'SELL';
  setup_type: string;
  entry_price: number;
  stop_loss: number;
  target_1: number;
  target_2: number;
  risk_reward_ratio: number;
  conviction: 'VERY_HIGH' | 'HIGH' | 'MEDIUM';
  status: 'WATCHING' | 'TRIGGERED' | 'TP1_HIT' | 'TP2_HIT' | 'STOPPED_OUT' | 'ACTIVE' | 'PENDING_APPROVAL' | 'QUEUED' | 'CONFIRMED_TRIGGER' | 'SUSTAINED_BREAKOUT' | 'EXECUTION_PAUSED' | 'ACTIVE_IN_TRADE' | 'SL_HIT' | 'CLOSED' | string;
  current_price: number;
  pnl_pct: number;
  catalyst_summary?: string;
  tape_evidence?: string;
  thesis?: string;
  invalidation_rule?: string;
  model_name?: string;
  rsi_5m?: number | null;
  rvol?: number | null;
  created_at: string;
  updated_at: string;
}

export interface ShortlistTimelineEvent {
  id: number;
  symbol: string;
  timestamp_ist: string;
  event_type: 'AI_SHORTLISTED' | 'ENTRY_TRIGGERED' | 'MOMENTUM_SURGE' | 'VOLUME_SPIKE' | 'TP1_HIT' | 'TP2_HIT' | 'SL_HIT' | 'CONFIRMED_TRIGGER' | 'SUSTAINED_BREAKOUT' | 'MANUAL_TRADE_ARMED' | 'STOPPED_OUT' | string;
  price_at_event?: number | null;
  message: string;
  severity: 'info' | 'success' | 'warning' | 'danger';
  created_at: string;
}

export interface ShortlistGeneratePayload {
  universe_data: any[];
  model: string;
  api_key?: string;
  provider: 'openrouter' | 'gemini' | 'openai' | 'anthropic' | 'ollama';
  custom_endpoint?: string;
}

export const getShortlist = async (): Promise<{ status: string; trades: ShortlistedTrade[]; count: number }> => {
  const res = await api.get('/shortlist');
  return res.data;
};

export const generateShortlist = async (
  payload: ShortlistGeneratePayload
): Promise<{ status: string; trades: ShortlistedTrade[]; count: number; model_used: string }> => {
  const res = await api.post('/shortlist/generate', payload);
  return res.data;
};

export const pollShortlistSurveillance = async (): Promise<{
  status: string;
  trades: ShortlistedTrade[];
  new_events: ShortlistTimelineEvent[];
  evaluated_at: number;
}> => {
  const res = await api.post('/shortlist/poll-status');
  return res.data;
};

export const getShortlistTimeline = async (
  symbol?: string,
  limit: number = 60
): Promise<{ status: string; events: ShortlistTimelineEvent[]; count: number }> => {
  const params: Record<string, any> = { limit };
  if (symbol) params.symbol = symbol;
  const res = await api.get('/shortlist/timeline', { params });
  return res.data;
};

export const deleteShortlistTrade = async (symbol: string): Promise<{ status: string; message: string }> => {
  const res = await api.delete(`/shortlist/${encodeURIComponent(symbol)}`);
  return res.data;
};

export const clearShortlist = async (): Promise<{ status: string; message: string }> => {
  const res = await api.post('/shortlist/clear');
  return res.data;
};

export interface AddManualTradePayload {
  symbol: string;
  action?: 'BUY' | 'SELL';
  current_price?: number;
  entry_price?: number;
  stop_loss?: number;
  target_1?: number;
  target_2?: number;
  quantity?: number;
  setup_type?: string;
  thesis?: string;
  catalyst_summary?: string;
  tape_evidence?: string;
  risk_reward_ratio?: number;
}

export const addManualShortlistTrade = async (
  payload: AddManualTradePayload
): Promise<{ status: string; trade: ShortlistedTrade }> => {
  const res = await api.post('/shortlist/add', payload);
  return res.data;
};

// ── Live Quant Watchdog & Capital Management Endpoints ───────────────────────

export interface WatchdogPositionItem {
  symbol: string;
  security_id: string;
  entry_price: number;
  quantity: number;
  order_id: string;
  hard_stop_loss: number;
  current_stop_loss: number;
  peak_price: number;
  breakeven_locked: boolean;
  is_runner_mode: boolean;
  status: string;
  allocated_capital: number;
  entry_time: number;
  pnl: number;
  pnl_pct: number;
  depth_level_active?: string;
}

export interface CapitalSlotsSummary {
  total_capital: number;
  per_trade_allocation: number;
  max_slots: number;
  active_slots_count: number;
  available_slots_count: number;
  deployed_capital: number;
  available_capital: number;
  active_positions: any[];
  priority_queue: any[];
}

export const getWatchdogPositions = async (): Promise<{
  status: string;
  count: number;
  max_slots: number;
  positions: WatchdogPositionItem[];
}> => {
  const res = await api.get('/watchdog/positions');
  return res.data;
};

export const getCapitalSlotsSummary = async (): Promise<CapitalSlotsSummary> => {
  const res = await api.get('/watchdog/slots');
  return res.data;
};

export const approveWatchdogTrade = async (
  symbol: string,
  trigger_price: number,
  security_id: string = '',
  quantity?: number
): Promise<{ status: string; message: string; order_id?: string }> => {
  const res = await api.post('/watchdog/approve', {
    symbol,
    trigger_price,
    security_id,
    quantity,
  });
  return res.data;
};

export const declineWatchdogTrade = async (symbol: string): Promise<{ status: string; symbol: string }> => {
  const res = await api.post('/watchdog/decline', { symbol });
  return res.data;
};

export const emergencyKillAllPositions = async (): Promise<{ status: string; results: any }> => {
  const res = await api.post('/watchdog/kill-all');
  return res.data;
};

export const promoteWatchdogCandidate = async (payload: {
  symbol: string;
  score: number;
  bias: string;
  live_status: string;
  trigger_price: number;
  current_price: number;
  rsi_5m?: number | null;
  rvol?: number | null;
  quote_source?: string | null;
  quote_age_ms?: number | null;
  rsi_is_real?: boolean;
  trigger_correlation_id?: string | null;
  thesis?: string;
  stop_loss?: number | null;
}): Promise<{ promoted: boolean; reason?: string; symbol?: string }> => {
  const res = await api.post('/watchdog/promote-candidate', payload);
  return res.data;
};

export interface WatchdogSettings {
  execution_enabled: boolean;
  total_capital: number;
  per_trade_allocation: number;
  max_active_trades: number;
  max_hard_stop_loss_pct: number;
  max_hard_stop_loss_percent: number;
  breakeven_trigger_pct: number;
  breakeven_trigger_percent: number;
  fee_buffer_pct: number;
  fee_buffer_percent: number;
  structural_trail_pct: number;
  structural_trail_percent: number;
  purge_slip_tolerance_pct: number;
  purge_slip_tolerance_percent: number;
  min_potential_score: number;
  max_entry_rsi_5m: number;
  min_entry_rvol: number;
  telegram_configured: boolean;
}

export interface DepthIntelligenceTelemetry {
  per_position: Array<{
    symbol: string;
    depth_level_active: string;
    security_id: string;
  }>;
  summary: {
    enabled: boolean;
    active_200depth_connections: number;
    max_200depth_slots: number;
    positions_using_200level: string[];
    positions_using_20level: string[];
  };
  fallback_chain: string;
  analytical_pool_size: number;
  watchdog_reserved_slots: number;
}

export const getWatchdogSettings = async (): Promise<{
  status: string;
  settings: WatchdogSettings;
  depth_intelligence?: DepthIntelligenceTelemetry;
}> => {
  const res = await api.get('/watchdog/settings');
  return res.data;
};

export const updateWatchdogSettings = async (
  updates: Partial<{
    execution_enabled: boolean;
    total_capital: number;
    per_trade_allocation: number;
    max_active_trades: number;
    max_hard_stop_loss_percent: number;
    breakeven_trigger_percent: number;
    structural_trail_percent: number;
    purge_slip_tolerance_percent: number;
  }>
): Promise<{ status: string; message: string; settings: WatchdogSettings }> => {
  const res = await api.post('/watchdog/settings', updates);
  return res.data;
};

export const toggleExecutionSwitch = async (
  enabled?: boolean
): Promise<{ status: string; execution_enabled: boolean; message: string }> => {
  const res = await api.post('/watchdog/toggle-execution', enabled !== undefined ? { enabled } : {});
  return res.data;
};

// ── Shorts Live Tracking & Shorts Watchdog Endpoints ─────────────────────────

export const getShortlistShorts = async (): Promise<{ status: string; trades: ShortlistedTrade[]; count: number }> => {
  const res = await api.get('/shortlist-shorts');
  return res.data;
};

export const pollShortlistShortsSurveillance = async (): Promise<{
  status: string;
  trades: ShortlistedTrade[];
  new_events: ShortlistTimelineEvent[];
  evaluated_at: number;
}> => {
  const res = await api.post('/shortlist-shorts/poll-status');
  return res.data;
};

export const getShortlistShortsTimeline = async (
  symbol?: string,
  limit: number = 60
): Promise<{ status: string; events: ShortlistTimelineEvent[]; count: number }> => {
  const params: Record<string, any> = { limit };
  if (symbol) params.symbol = symbol;
  const res = await api.get('/shortlist-shorts/timeline', { params });
  return res.data;
};

export const deleteShortlistShortsTrade = async (symbol: string): Promise<{ status: string; message: string }> => {
  const res = await api.delete(`/shortlist-shorts/${encodeURIComponent(symbol)}`);
  return res.data;
};

export const clearShortlistShorts = async (): Promise<{ status: string; message: string }> => {
  const res = await api.post('/shortlist-shorts/clear');
  return res.data;
};

export const addManualShortlistShortsTrade = async (
  payload: AddManualTradePayload
): Promise<{ status: string; trade: ShortlistedTrade }> => {
  const res = await api.post('/shortlist-shorts/add', { ...payload, action: 'SELL' });
  return res.data;
};

export const getWatchdogShortsPositions = async (): Promise<{
  status: string;
  count: number;
  max_slots: number;
  positions: WatchdogPositionItem[];
}> => {
  const res = await api.get('/watchdog-shorts/positions');
  return res.data;
};

export const approveWatchdogShortsTrade = async (
  symbol: string,
  trigger_price: number,
  security_id: string = '',
  quantity?: number
): Promise<{ status: string; message: string; order_id?: string }> => {
  const res = await api.post('/watchdog-shorts/approve', {
    symbol,
    trigger_price,
    security_id,
    quantity,
    action: 'SELL',
  });
  return res.data;
};

export const declineWatchdogShortsTrade = async (symbol: string): Promise<{ status: string; symbol: string }> => {
  const res = await api.post('/watchdog-shorts/decline', { symbol });
  return res.data;
};

export const emergencyKillAllShortsPositions = async (): Promise<{ status: string; results: any }> => {
  const res = await api.post('/watchdog-shorts/kill-all');
  return res.data;
};

export const promoteWatchdogShortsCandidate = async (payload: {
  symbol: string;
  score: number;
  bias: string;
  live_status: string;
  trigger_price: number;
  current_price: number;
  rsi_5m?: number | null;
  rvol?: number | null;
  quote_source?: string | null;
  quote_age_ms?: number | null;
  rsi_is_real?: boolean;
  trigger_correlation_id?: string | null;
  thesis?: string;
  stop_loss?: number | null;
}): Promise<{ promoted: boolean; reason?: string; symbol?: string }> => {
  const res = await api.post('/watchdog-shorts/promote-candidate', payload);
  return res.data;
};

// ── Live Stock Screener for Short Candidates ────────────────────────────────

export interface LiveShortStock {
  symbol: string;
  token: number;
  ltp: number;
  prev_close: number;
  change_pct: number;
  high: number;
  low: number;
  volume: number;
  rsi: number;
  rsi_velocity: number;
  is_rapid_rsi: boolean;
  news_catalyst_score: number;
  news_catalyst_label: string;
  is_news_evaluated?: boolean;
  news_article_count?: number;
  news_status?: 'EVALUATED' | 'NO_NEWS' | 'UNEVALUATED';
  ifp_score: number;
  ifp_label: string;
  short_rank: number;
  updated_at: number;
  updated_at_iso?: string;
}

export interface LiveShortScreenerResponse {
  is_active: boolean;
  market_status?: string;
  as_of: string;
  total_count: number;
  stocks: LiveShortStock[];
  overbought_count: number;
  rapid_shift_count: number;
}

export const getShortScreenerPower = async (): Promise<{ is_active: boolean }> => {
  const res = await api.get('/short-screener/power');
  return res.data;
};

export const setShortScreenerPower = async (
  active: boolean
): Promise<{ status: string; is_active: boolean }> => {
  const res = await api.post('/short-screener/power', { active });
  return res.data;
};

export const getLiveShortScreenerStocks = async (): Promise<LiveShortScreenerResponse> => {
  const res = await api.get('/short-screener/stocks');
  return res.data;
};

export const getShortScreenerScan = async (): Promise<LiveShortScreenerResponse> => {
  const res = await api.get('/short-screener/scan');
  return res.data;
};

export interface FinBERTNewsItem {
  title: string;
  source: string;
  hours_ago: number;
  time_ago: string;
  sentiment: 'BULLISH' | 'BEARISH' | 'NEUTRAL';
  score: number;
  is_regulatory?: boolean;
  prob_positive?: number;
  prob_negative?: number;
  prob_neutral?: number;
  detail?: string;
}

export interface FinBERTNewsResponse {
  symbol: string;
  sentiment_score: number;
  sentiment_label: string;
  is_evaluated?: boolean;
  status?: 'EVALUATED' | 'NO_NEWS' | 'UNEVALUATED';
  probabilities: {
    positive: number;
    neutral: number;
    negative: number;
  };
  article_count: number;
  headlines: FinBERTNewsItem[];
  summary: string;
  narrative_alignment?: {
    state: string;
    badge: string;
    type: string;
    detail: string;
  };
  scanned_at: string;
  lookback_window: string;
}

export const getShortScreenerNews = async (symbol: string): Promise<FinBERTNewsResponse> => {
  const res = await api.get(`/short-screener/news/${encodeURIComponent(symbol)}`);
  return res.data;
};



