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
    const url = joinUrl(this.baseUrl, `/api/v1/ingest/${normalizedKind}`);
    const token = options.token || this.token;
    const res = await this.fetchImpl(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { 'x-api-token': token } : {})
      },
      body: JSON.stringify(payload)
    });
    let data = {};
    try {
      data = await res.json();
    } catch {
      data = {};
    }
    if (!res.ok || data.status === 'error') {
      throw new MonitorApiError(data.message || `HTTP ${res.status}`, {
        code: data.error_code || data.code || MONITOR_ERROR_CODE.INVALID_PAYLOAD,
        status: res.status,
        payload: data
      });
    }
    return data;
  }

  async getSnapshot(options = {}) {
    const params = new URLSearchParams();
    if (options.topologyEpoch != null) {
      params.set('topology_epoch', String(options.topologyEpoch));
    }
    const query = params.toString();
    const path = `/api/v1/monitor/snapshot${query ? `?${query}` : ''}`;
    const url = joinUrl(this.baseUrl, path);
    const token = options.token || this.token;
    const res = await this.fetchImpl(url, {
      method: 'GET',
      headers: {
        ...(token ? { 'x-api-token': token } : {})
      }
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
    return data;
  }
}
