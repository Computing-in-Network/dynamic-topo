import test from 'node:test';
import assert from 'node:assert/strict';
import { MonitorApiClient, MonitorApiError } from './client.js';

test('getSnapshot sends topology_epoch query', async () => {
  let capturedUrl = '';
  const client = new MonitorApiClient({
    baseUrl: 'http://collector',
    fetchImpl: async (url) => {
      capturedUrl = url;
      return {
        ok: true,
        status: 200,
        async json() {
          return { monitor: {} };
        }
      };
    }
  });
  await client.getSnapshot({ topologyEpoch: 1708848000 });
  assert.equal(capturedUrl, 'http://collector/api/v1/monitor/snapshot?topology_epoch=1708848000');
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

