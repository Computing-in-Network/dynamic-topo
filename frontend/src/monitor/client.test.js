import test from 'node:test';
import assert from 'node:assert/strict';
import { MonitorApiClient, MonitorApiError } from './client.js';

test('getSnapshot sends topology_epoch query', async () => {
  let capturedUrl = '';
  let capturedHeaders = {};
  const client = new MonitorApiClient({
    baseUrl: 'http://collector',
    fetchImpl: async (url, options = {}) => {
      capturedUrl = url;
      capturedHeaders = options.headers || {};
      return {
        ok: true,
        status: 200,
        headers: { get: () => 'W/"1708848000-10"' },
        async json() {
          return { monitor: {} };
        }
      };
    }
  });
  const res = await client.getSnapshot({ topologyEpoch: 1708848000, etag: 'W/"1708848000-9"' });
  assert.equal(capturedUrl, 'http://collector/api/v1/monitor/snapshot?topology_epoch=1708848000');
  assert.equal(capturedHeaders['If-None-Match'], 'W/"1708848000-9"');
  assert.equal(res.notModified, false);
  assert.equal(res.etag, 'W/"1708848000-10"');
});

test('getSnapshot maps backend error_code', async () => {
  const client = new MonitorApiClient({
    baseUrl: 'http://collector',
    fetchImpl: async () => ({
      ok: false,
      status: 401,
      async json() {
        return {
          status: 'error',
          error_code: 'UNAUTHORIZED',
          error_message: 'bad token'
        };
      }
    })
  });
  await assert.rejects(
    () => client.getSnapshot(),
    (err) => {
      assert.ok(err instanceof MonitorApiError);
      assert.equal(err.code, 'UNAUTHORIZED');
      assert.equal(err.status, 401);
      return true;
    }
  );
});

test('getSnapshot handles 304 not modified', async () => {
  const client = new MonitorApiClient({
    baseUrl: 'http://collector',
    fetchImpl: async () => ({
      ok: false,
      status: 304,
      headers: { get: () => 'W/"1708848000-11"' },
      async json() {
        return {};
      }
    })
  });
  const res = await client.getSnapshot({ topologyEpoch: 1708848000, etag: 'W/"1708848000-10"' });
  assert.equal(res.notModified, true);
  assert.equal(res.status, 304);
  assert.equal(res.etag, 'W/"1708848000-11"');
});

test('queryPathAnalysis posts payload', async () => {
  let capturedPath = '';
  let capturedBody = '';
  const client = new MonitorApiClient({
    baseUrl: 'http://collector',
    fetchImpl: async (url, options = {}) => {
      capturedPath = url;
      capturedBody = options.body;
      return {
        ok: true,
        status: 200,
        async json() {
          return { status: 'ok', result: { paths: [] } };
        }
      };
    }
  });
  await client.queryPathAnalysis({ src: 'A', dst: 'B' });
  assert.equal(capturedPath, 'http://collector/api/v1/analysis/path/query');
  assert.equal(capturedBody, JSON.stringify({ src: 'A', dst: 'B' }));
});

test('analyzeFaultSpread posts payload', async () => {
  let capturedPath = '';
  let capturedBody = '';
  const client = new MonitorApiClient({
    baseUrl: 'http://collector',
    fetchImpl: async (url, options = {}) => {
      capturedPath = url;
      capturedBody = options.body;
      return {
        ok: true,
        status: 200,
        async json() {
          return { status: 'ok', result: { impacted_nodes: [] } };
        }
      };
    }
  });
  await client.analyzeFaultSpread({ alarm_nodes: ['A'] });
  assert.equal(capturedPath, 'http://collector/api/v1/fault/spread/analyze');
  assert.equal(capturedBody, JSON.stringify({ alarm_nodes: ['A'] }));
});
