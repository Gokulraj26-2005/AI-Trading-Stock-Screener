import { AxiosRequestConfig, AxiosResponse } from 'axios';
import { afterEach, beforeEach, vi } from 'vitest';
import {
  api,
  clearSessionToken,
  createSessionToken,
  getSessionToken,
  setSessionToken,
  getChartData,
} from './api';

function readAuthorizationHeader(headers: unknown): string {
  if (!headers || typeof headers !== 'object') {
    return '';
  }

  const candidate = headers as {
    get?: (name: string) => string | undefined;
    Authorization?: string;
    authorization?: string;
  };

  if (typeof candidate.get === 'function') {
    return candidate.get('Authorization') || candidate.get('authorization') || '';
  }

  return candidate.Authorization || candidate.authorization || '';
}

function adapterEcho(config: AxiosRequestConfig): Promise<AxiosResponse> {
  return Promise.resolve({
    data: {},
    status: 200,
    statusText: 'OK',
    headers: {},
    config,
  });
}

describe('session token storage helpers', () => {
  beforeEach(() => {
    clearSessionToken();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('stores and retrieves a normalized token', () => {
    const stored = setSessionToken('  abc123  ');

    expect(stored).toBe('abc123');
    expect(getSessionToken()).toBe('abc123');
  });

  it('clears token from in-memory cache', () => {
    setSessionToken('token');
    clearSessionToken();

    expect(getSessionToken()).toBe('');
  });

  it('returns empty token for null input', () => {
    const stored = setSessionToken(null);

    expect(stored).toBe('');
    expect(getSessionToken()).toBe('');
  });

  it('injects bearer token for protected API requests', async () => {
    setSessionToken('test-token');

    const response = await api.get('/user/profile', {
      adapter: adapterEcho,
    });

    const auth = readAuthorizationHeader(response.config.headers);
    expect(auth).toBe('Bearer test-token');
  });

  it('does not inject bearer token for public API paths', async () => {
    setSessionToken('test-token');

    const response = await api.get('/broker/status', {
      adapter: adapterEcho,
    });

    const auth = readAuthorizationHeader(response.config.headers);
    expect(auth).toBe('');
  });

  it('preserves explicit authorization header from caller', async () => {
    setSessionToken('cached-token');

    const response = await api.get('/user/profile', {
      headers: {
        Authorization: 'Bearer explicit-token',
      },
      adapter: adapterEcho,
    });

    const auth = readAuthorizationHeader(response.config.headers);
    expect(auth).toBe('Bearer explicit-token');
  });

  it('passes bootstrap and authorization headers when minting session token', async () => {
    const postSpy = vi.spyOn(api, 'post').mockResolvedValue({
      data: {
        access_token: 'session-token',
        token_type: 'bearer',
        expires_in: 3600,
        user_id: 'dev_user',
      },
    } as any);

    await createSessionToken({
      bootstrapKey: '  bootstrap-123  ',
      authorizationToken: '  app-token  ',
    });

    expect(postSpy).toHaveBeenCalledWith(
      '/auth/session-token',
      {},
      {
        headers: {
          'X-Session-Bootstrap-Key': 'bootstrap-123',
          Authorization: 'Bearer app-token',
        },
        withCredentials: true,
      },
    );
  });
});

describe('getChartData API helper', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('fetches chart data with default interval', async () => {
    const getSpy = vi.spyOn(api, 'get').mockResolvedValue({
      data: {
        symbol: 'RELIANCE',
        interval: '5m',
      },
    } as any);

    const result = await getChartData('RELIANCE');
    expect(getSpy).toHaveBeenCalledWith('/chart/RELIANCE', {
      params: { interval: '5m' },
    });
    expect(result.symbol).toBe('RELIANCE');
  });

  it('fetches chart data with custom interval', async () => {
    const getSpy = vi.spyOn(api, 'get').mockResolvedValue({
      data: {
        symbol: 'TCS',
        interval: '15m',
      },
    } as any);

    const result = await getChartData('TCS', '15m');
    expect(getSpy).toHaveBeenCalledWith('/chart/TCS', {
      params: { interval: '15m' },
    });
    expect(result.symbol).toBe('TCS');
  });
});


