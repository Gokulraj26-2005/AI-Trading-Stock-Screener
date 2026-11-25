import React, { useCallback, useEffect, useRef, useState, Suspense, lazy } from 'react';

// Route-level code splitting via dynamic lazy imports
const ChartsTerminalPage = lazy(() => import('./components/ChartsTerminalPage').then(m => ({ default: m.ChartsTerminalPage })));
const MultiTimeframeAnalysisPage = lazy(() => import('./components/MultiTimeframeAnalysisPage').then(m => ({ default: m.MultiTimeframeAnalysisPage })));
const ShortlistedPage = lazy(() => import('./components/ShortlistedPage').then(m => ({ default: m.ShortlistedPage })));
const ShortlistedShortsPage = lazy(() => import('./components/ShortlistedShortsPage').then(m => ({ default: m.ShortlistedShortsPage })));
const ShortScreenerLivePage = lazy(() => import('./components/ShortScreenerLivePage').then(m => ({ default: m.ShortScreenerLivePage })));
import { Layout } from './components/Layout';
import { ErrorBoundary } from './components/ErrorBoundary';
import { ArrowRight, X } from 'lucide-react';
import { ThemeProvider } from './contexts/ThemeContext';
import { PrivacyProvider } from './contexts/PrivacyContext';
import {
  createSessionToken,
  consumeDhanToken,
  getBrokerStatus,
  getDhanLoginUrl,
  getSessionToken,
  logoutBrokerSession,
  setMockMode,
  setSessionToken,
} from './api';

type AuthState = 'checking' | 'needs-login' | 'redirecting' | 'exchanging' | 'ready' | 'error';

function getErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null) {
    const maybeError = error as {
      response?: { data?: { detail?: string } };
      message?: string;
    };
    if (maybeError.response?.data?.detail) {
      return maybeError.response.data.detail;
    }
    if (maybeError.message) {
      return maybeError.message;
    }
  }

  return 'Unexpected authentication error.';
}

function isRetryableBackendError(message: string): boolean {
  const lower = message.toLowerCase();
  return (
    lower.includes('network error') ||
    lower.includes('failed to fetch') ||
    lower.includes('econnrefused') ||
    lower.includes('timeout') ||
    lower.includes('502') ||
    lower.includes('503') ||
    lower.includes('504')
  );
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function clearAuthCallbackParams(): void {
  const cleanedUrl = `${window.location.origin}${window.location.pathname}${window.location.hash}`;
  window.history.replaceState({}, document.title, cleanedUrl);
}


export default function App() {
  const [token, setToken] = useState<string>(() => getSessionToken());
  const [currentView, setCurrentView] = useState(() => {
    if (typeof window !== 'undefined') {
      const p = window.location.pathname.toLowerCase();
      if (p.includes('short-screener')) {
        return 'short-screener';
      }
      if (p.includes('shortlist-shorts')) {
        return 'shortlisted-shorts';
      }
      if (p.includes('shortlist')) {
        return 'shortlisted';
      }
      if (p.includes('chart') && !p.includes('analysis')) {
        return 'charts';
      }
    }
    return 'chart-analysis';
  });
  const [selectedChartSymbol, setSelectedChartSymbol] = useState<string | null>(null);
  const [isDevBypass, setIsDevBypass] = useState<boolean>(() => {
    return typeof window !== 'undefined' && localStorage.getItem('dev_bypass_auth') === 'true';
  });

  const handleTabChange = useCallback((tab: string) => {
    setCurrentView(tab);
    if (typeof window !== 'undefined') {
      const targetPath =
        tab === 'short-screener'
          ? '/short-screener'
          : tab === 'charts'
          ? '/charts'
          : tab === 'shortlisted'
          ? '/shortlisted'
          : tab === 'shortlisted-shorts'
          ? '/shortlisted-shorts'
          : '/analysis';
      if (window.location.pathname !== targetPath) {
        window.history.pushState(null, '', targetPath);
      }
    }
  }, []);

  useEffect(() => {
    const handlePopState = () => {
      const p = window.location.pathname.toLowerCase();
      if (p.includes('short-screener')) {
        setCurrentView('short-screener');

      } else if (p.includes('shortlist-shorts')) {
        setCurrentView('shortlisted-shorts');
      } else if (p.includes('shortlist')) {
        setCurrentView('shortlisted');
      } else if (p.includes('chart') && !p.includes('analysis')) {
        setCurrentView('charts');
      } else {
        setCurrentView('chart-analysis');
      }
    };
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);

  useEffect(() => {
    const handleOpenChart = (e: any) => {
      if (e.detail?.symbol) {
        setSelectedChartSymbol(e.detail.symbol);
        handleTabChange('charts');
      }
    };
    window.addEventListener('open_chart', handleOpenChart);
    return () => window.removeEventListener('open_chart', handleOpenChart);
  }, [handleTabChange]);
  const [showLoginModal, setShowLoginModal] = useState<boolean>(false);
  const [authState, setAuthState] = useState<AuthState>('ready');
  const [authMessage, setAuthMessage] = useState('Initializing Intraday AI Desk...');
  const persistSessionToken = useCallback((nextToken: string | null) => {
    const cleaned = setSessionToken(nextToken || '');
    setToken(cleaned);
  }, []);

  const mintSessionToken = useCallback(async (bootstrapKey?: string | null) => {
    try {
      const session = await createSessionToken({
        bootstrapKey,
        authorizationToken: token,
      });
      const sessionToken = String(session?.access_token || '').trim();
      if (sessionToken) {
        persistSessionToken(sessionToken);
        return;
      }
    } catch (_err) {
      // Graceful fallback to dev preview token
    }
    const current = getSessionToken();
    if (!current) {
      persistSessionToken('dev_preview_mock_token');
    }
  }, [persistSessionToken, token]);

  const refreshDhanStatus = useCallback(async (bootstrapKey?: string | null) => {
    if (localStorage.getItem('dhan_logged_out') === 'true') {
      setIsDevBypass(true);
      setAuthState('ready');
      setAuthMessage('Dhan is disconnected. Using Preview Mode.');
      return;
    }

    try {
      const status = await getBrokerStatus();
      if (status.has_access_token) {
        await mintSessionToken(bootstrapKey);
        setIsDevBypass(false);
        setAuthState('ready');
        setAuthMessage('Broker session active.');
      } else {
        setIsDevBypass(true);
        setAuthState('ready');
        setAuthMessage('Preview Mode active (Broker not connected).');
      }
    } catch (error) {
      setIsDevBypass(true);
      setAuthState('ready');
      setAuthMessage('Preview Mode active (Broker offline).');
    }
  }, [mintSessionToken]);

  const refreshDhanStatusWithRetry = useCallback(async (
    bootstrapKey?: string | null,
    attempts: number = 3,
    initialRetryDelayMs: number = 1000,
    maxRetryDelayMs: number = 5000,
  ) => {
    if (localStorage.getItem('dhan_logged_out') === 'true') {
      setIsDevBypass(true);
      setAuthState('ready');
      setAuthMessage('Dhan is disconnected. Using Preview Mode.');
      return;
    }

    let retryDelayMs = initialRetryDelayMs;

    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      try {
        const status = await getBrokerStatus();
        if (status.has_access_token) {
          await mintSessionToken(bootstrapKey);
          setIsDevBypass(false);
          setAuthState('ready');
          setAuthMessage('Broker session active.');
          return;
        } else {
          setIsDevBypass(true);
          setAuthState('ready');
          setAuthMessage('Preview Mode active (Broker not connected).');
          return;
        }
      } catch (error) {
        const message = getErrorMessage(error);
        const retryable = isRetryableBackendError(message);

        if (!retryable || attempt === attempts) {
          setIsDevBypass(true);
          setAuthState('ready');
          setAuthMessage('Preview Mode active (Broker offline).');
          return;
        }

        await delay(retryDelayMs);
        retryDelayMs = Math.min(Math.round(retryDelayMs * 1.5), maxRetryDelayMs);
      }
    }
  }, [mintSessionToken]);

  useEffect(() => {
    let alive = true;

    const initializeAuth = async () => {
      const tokenId = new URLSearchParams(window.location.search).get('tokenId');
      if (tokenId && window.opener && window.opener !== window) {
        setAuthState('exchanging');
        setAuthMessage('Completing Dhan login...');
        try {
          await consumeDhanToken(tokenId);
          window.opener.postMessage({ type: 'dhan-auth-complete' }, window.location.origin);
          window.close();
        } catch (error) {
          setAuthState('error');
          setAuthMessage(getErrorMessage(error));
        }
        return;
      }

      if (typeof window !== 'undefined' && localStorage.getItem('dev_bypass_auth') === 'true') {
        setIsDevBypass(true);
        setAuthState('ready');
        setAuthMessage('Preview Mode active (Mock data enabled).');
        try {
          await setMockMode(true);
        } catch {
          // ignore
        }
        await mintSessionToken();
        return;
      }

      await refreshDhanStatusWithRetry(undefined);
    };

    const onDhanAuthComplete = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.data?.type !== 'dhan-auth-complete') {
        return;
      }
      localStorage.removeItem('dhan_logged_out');
      void refreshDhanStatusWithRetry(undefined, 1);
    };
    window.addEventListener('message', onDhanAuthComplete);
    initializeAuth();

    return () => {
      alive = false;
      window.removeEventListener('message', onDhanAuthComplete);
    };
  }, [mintSessionToken, refreshDhanStatusWithRetry]);

  const handleSkipLogin = useCallback(async () => {
    localStorage.setItem('dev_bypass_auth', 'true');
    setIsDevBypass(true);
    setShowLoginModal(false);
    setAuthState('ready');
    setAuthMessage('Preview Mode active (Mock data enabled).');
    try {
      await setMockMode(true);
    } catch {
      // ignore
    }
    try {
      await mintSessionToken();
    } catch {
      persistSessionToken('dev_preview_mock_token');
    }
  }, [mintSessionToken, persistSessionToken]);

  const handleExitPreview = useCallback(() => {
    setShowLoginModal(true);
  }, []);

  const onLogoutFromDhan = useCallback(async () => {
    localStorage.removeItem('dev_bypass_auth');
    try {
      await logoutBrokerSession();
    } catch {
      // ignore
    }
    persistSessionToken(null);
    clearAuthCallbackParams();
    setIsDevBypass(true);
    setAuthMessage('Dhan session disconnected. Running in Preview Mode.');
  }, [persistSessionToken]);

  const onConnectDhan = useCallback(async () => {
    localStorage.removeItem('dhan_logged_out');
    setAuthState('checking');
    setAuthMessage('Connecting to Dhan and validating the access token...');
    await refreshDhanStatusWithRetry(undefined, 1);
  }, [refreshDhanStatusWithRetry]);

  const onOpenDhanLogin = useCallback(async () => {
    const popup = window.open(
      'about:blank',
      'dhan-login',
      'popup=yes,width=520,height=760,resizable=yes,scrollbars=yes,status=no,menubar=no,toolbar=no'
    );
    if (!popup) {
      setAuthState('error');
      setAuthMessage('Popup was blocked. Allow popups for this site and click Open Dhan Login again.');
      return;
    }

    try {
      setAuthState('redirecting');
      setAuthMessage('Opening Dhan login...');
      const { login_url } = await getDhanLoginUrl();
      popup.location.href = login_url;
      popup.focus();
      setAuthMessage('Complete Dhan login in the popup. This window will continue automatically.');
    } catch (error) {
      popup.close();
      setAuthState('error');
      setAuthMessage(getErrorMessage(error));
    }
  }, []);

  const renderContent = () => {
    switch (currentView) {
      case 'charts':
        return <ChartsTerminalPage token={token} initialSymbol={selectedChartSymbol || undefined} />;
      case 'shortlisted':
        return (
          <ShortlistedPage
            token={token}
            onOpenChart={(sym) => {
              setSelectedChartSymbol(sym);
              handleTabChange('charts');
            }}
          />
        );
      case 'shortlisted-shorts':
        return (
          <ShortlistedShortsPage
            token={token}
            onOpenChart={(sym) => {
              setSelectedChartSymbol(sym);
              handleTabChange('charts');
            }}
          />
        );
      case 'short-screener':
        return (
          <ShortScreenerLivePage
            token={token}
            onOpenChart={(sym) => {
              setSelectedChartSymbol(sym);
              handleTabChange('charts');
            }}
            onNavigateToShortsDesk={() => {
              handleTabChange('shortlisted-shorts');
            }}
          />
        );
      default:
        return null;
    }
  };

  const isLoading = authState === 'checking' || authState === 'exchanging' || authState === 'redirecting';

  return (
    <ThemeProvider>
      <PrivacyProvider>
        {isDevBypass && (
          <div className="fixed top-0 left-0 right-0 z-40 bg-gradient-to-r from-amber-500/20 via-amber-600/20 to-amber-500/20 border-b border-amber-500/30 px-6 py-2 flex items-center justify-between text-xs text-amber-200 backdrop-blur-md">
            <div className="flex items-center gap-2.5">
              <span className="inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse"></span>
              <span className="font-semibold tracking-wide uppercase">Preview Mode Active</span>
              <span className="text-amber-300/80 hidden sm:inline">— Mock depth and price simulation active.</span>
            </div>
            <button
              onClick={() => setShowLoginModal(true)}
              className="px-2.5 py-0.5 rounded bg-amber-500/20 hover:bg-amber-500/40 text-amber-100 border border-amber-500/40 transition-all font-medium text-[11px]"
            >
              Sign In / Connect Broker
            </button>
          </div>
        )}
        <Layout
          activeTab={currentView}
          onTabChange={handleTabChange}
          token={token}
          onLoginClick={() => setShowLoginModal(true)}
          onLogoutClick={onLogoutFromDhan}
        >
          <ErrorBoundary fallbackTitle="Trading Terminal Render Error">
            <Suspense fallback={
              <div className="flex-1 flex items-center justify-center min-h-[400px]">
                <div className="flex flex-col items-center gap-3">
                  <div className="w-8 h-8 rounded-full border-2 border-cyan-400 border-t-transparent animate-spin" />
                  <span className="text-xs text-slate-400 uppercase tracking-widest">Loading Desk Module...</span>
                </div>
              </div>
            }>
              <div className={`h-full w-full flex flex-col min-h-0 overflow-hidden ${isDevBypass ? "pt-8" : ""}`}>
                {/* Preserved Chart Analysis container: stays in memory when navigating to other tabs */}
                <div className={`h-full w-full flex flex-col min-h-0 overflow-hidden ${currentView === 'chart-analysis' ? '' : 'hidden'}`}>
                  <MultiTimeframeAnalysisPage token={token} />
                </div>
                {currentView !== 'chart-analysis' && renderContent()}
              </div>
            </Suspense>
          </ErrorBoundary>
        </Layout>

        {showLoginModal && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md">
            <div className="relative w-full max-w-xl glass-panel p-8 md:p-10 border border-white/20 bg-slate-950/95 rounded-2xl shadow-2xl">
              <button
                onClick={() => setShowLoginModal(false)}
                className="absolute top-4 right-4 p-2 text-slate-400 hover:text-white rounded-lg hover:bg-white/10"
              >
                <X size={20} />
              </button>
              <p className="text-xs uppercase tracking-[0.28em] text-cyan-200/80 mb-3">VIA Trading Desk</p>
              <h1 className="text-2xl md:text-3xl font-black text-white leading-tight mb-4">Connect Broker (Dhan)</h1>
              <p className="text-sm text-slate-200/85 leading-relaxed mb-4">
                Connect your Dhan account through the API login flow. A popup will open for Dhan credentials and two-factor verification.
              </p>
              <div className="rounded-xl border border-white/15 bg-black/30 px-4 py-3 mb-6 text-sm text-slate-200/90">
                {authMessage}
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <button
                  className="glass-button bg-blue-500/20 border-blue-300/40 text-blue-100 hover:bg-blue-500/30 px-5 py-2.5 rounded-xl font-medium"
                  onClick={onOpenDhanLogin}
                >
                  Open Dhan Login
                </button>
                <button
                  className="glass-button bg-cyan-500/20 border-cyan-300/40 text-cyan-100 hover:bg-cyan-500/30 px-5 py-2.5 rounded-xl font-medium"
                  onClick={onConnectDhan}
                  disabled={isLoading}
                >
                  {isLoading ? 'Connecting...' : 'Validate Session'}
                </button>
                <button
                  className="glass-button bg-white/10 border-white/20 text-slate-200 hover:bg-white/15 px-5 py-2.5 rounded-xl font-medium"
                  onClick={() => setShowLoginModal(false)}
                >
                  Close
                </button>
              </div>
            </div>
          </div>
        )}
      </PrivacyProvider>
    </ThemeProvider>
  );
}

