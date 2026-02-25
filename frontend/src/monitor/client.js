import {
  MONITOR_ERROR_CODE,
  MONITOR_SCHEMA_VERSION,
  normalizeMonitorKind
} from './types.js';

function joinUrl(baseUrl, path) {
  if (!baseUrl) {
    return path;
  }
  return `${baseUrl.replace(/\/+$/, '')}${path}`;
}

export class MonitorApiError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'MonitorApiError';
    this.code = details.code || MONITOR_ERROR_CODE.INVALID_PAYLOAD;
    this.status = details.status || 0;
    this.payload = details.payload;
  }
}

export class MonitorApiClient {
  constructor(options = {}) {
    this.baseUrl = options.baseUrl || '';
    this.token = options.token || '';
    this.fetchImpl = options.fetchImpl || window.fetch.bind(window);
  }

  async _request(path, options = {}) {
    const token = options.token || this.token;
    const headers = {
      ...(options.json ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { 'x-api-token': token } : {}),
      ...(options.headers || {})
    };
    const res = await this.fetchImpl(joinUrl(this.baseUrl, path), {
      method: options.method || 'GET',
      headers,
      body: options.body
    });
    let data = {};
    try {
      data = await res.json();
    } catch {
      data = {};
    }
    if (!res.ok || data.status === 'error') {
      throw new MonitorApiError(data.error_message || data.message || `HTTP ${res.status}`, {
        code: data.error_code || data.code || MONITOR_ERROR_CODE.INVALID_PAYLOAD,
        status: res.status,
        payload: data
      });
    }
    return { res, data };
  }

  async ingest(kind, event, options = {}) {
    const normalizedKind = normalizeMonitorKind(kind);
    if (!normalizedKind) {
      throw new MonitorApiError(`Invalid kind: ${kind}`, {
        code: MONITOR_ERROR_CODE.INVALID_KIND
      });
    }
    if (!event || typeof event !== 'object') {
      throw new MonitorApiError('Invalid payload: event must be object', {
        code: MONITOR_ERROR_CODE.INVALID_PAYLOAD
      });
    }
    const payload = {
      schema_version: MONITOR_SCHEMA_VERSION,
      ...event
    };
    const { data } = await this._request(`/api/v1/ingest/${normalizedKind}`, {
      method: 'POST',
      json: true,
      body: JSON.stringify(payload),
      token: options.token
    });
    return data;
  }

  async getSnapshot(options = {}) {
    const params = new URLSearchParams();
    if (options.topologyEpoch != null) {
      params.set('topology_epoch', String(options.topologyEpoch));
    }
    const query = params.toString();
    const path = `/api/v1/monitor/snapshot${query ? `?${query}` : ''}`;
    const token = options.token || this.token;
    const headers = {
      ...(token ? { 'x-api-token': token } : {})
    };
    if (options.etag) {
      headers['If-None-Match'] = options.etag;
    }
    const res = await this.fetchImpl(joinUrl(this.baseUrl, path), {
      method: 'GET',
      headers
    });
    if (res.status === 304) {
      return {
        notModified: true,
        status: 304,
        etag: res.headers?.get?.('etag') || options.etag || '',
        data: null
      };
    }
    let data = {};
    try {
      data = await res.json();
    } catch {
      data = {};
    }
    if (!res.ok || data.status === 'error') {
      throw new MonitorApiError(data.error_message || data.message || `HTTP ${res.status}`, {
        code: data.error_code || data.code || MONITOR_ERROR_CODE.INVALID_PAYLOAD,
        status: res.status,
        payload: data
      });
    }
    return {
      notModified: false,
      status: res.status,
      etag: res.headers?.get?.('etag') || '',
      data
    };
  }

  async getHealth(options = {}) {
    const { data } = await this._request('/health', {
      method: 'GET',
      token: options.token
    });
    return data;
  }

  async getMetrics(options = {}) {
    const { data } = await this._request('/metrics', {
      method: 'GET',
      token: options.token
    });
    return data;
  }

  async queryPathAnalysis(payload, options = {}) {
    const { data } = await this._request('/api/v1/analysis/path/query', {
      method: 'POST',
      json: true,
      body: JSON.stringify(payload || {}),
      token: options.token
    });
    return data;
  }

  async analyzeFaultSpread(payload, options = {}) {
    const { data } = await this._request('/api/v1/fault/spread/analyze', {
      method: 'POST',
      json: true,
      body: JSON.stringify(payload || {}),
      token: options.token
    });
    return data;
  }
}
