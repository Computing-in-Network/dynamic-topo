import test from 'node:test';
import assert from 'node:assert/strict';
import { applyMonitorEvent, applyMonitorSnapshot, createEmptyMonitorSnapshot } from './adapter.js';
import { MONITOR_EVENT_KIND, MONITOR_SCHEMA_VERSION } from './types.js';

test('applyMonitorEvent uses injected thresholds', () => {
  const snapshot = createEmptyMonitorSnapshot();
  const next = applyMonitorEvent(snapshot, {
    kind: MONITOR_EVENT_KIND.NODE_METRIC,
    schema_version: MONITOR_SCHEMA_VERSION,
    node_id: 'N-001',
    cpu_ratio: 0.82,
    mem_ratio: 0.5
  }, {
    thresholds: {
      cpu_ratio: { warning: 0.8, critical: 0.95 },
      mem_ratio: { warning: 0.75, critical: 0.9 },
      loss_rate: { warning: 0.01, critical: 0.05 },
      rtt_ms: { warning: 80, critical: 150 },
      jitter_ms: { warning: 20, critical: 50 }
    }
  });
  assert.equal(next.byNode['N-001'].health, 'warning');
});

test('applyMonitorSnapshot parses nodes links and alarms', () => {
  const next = applyMonitorSnapshot(createEmptyMonitorSnapshot(), {
    monitor: {
      nodes: {
        'N-001': {
          node_id: 'N-001',
          cpu_ratio: 0.2,
          mem_ratio: 0.3
        }
      },
      links: {
        'N-001->N-002': {
          link_id: 'N-001->N-002',
          src_node_id: 'N-001',
          dst_node_id: 'N-002',
          loss_rate: 0.001,
          rtt_ms: 10,
          jitter_ms: 1
        }
      },
      alarms: [
        {
          alarm_id: 'A-1',
          severity: 'warning',
          scope_type: 'link',
          scope_id: 'N-001->N-002',
          timestamp: '2026-02-25T00:00:00Z'
        }
      ],
      updated_at: '2026-02-25T00:00:00Z'
    }
  });
  assert.equal(next.nodeCount, 1);
  assert.equal(next.linkCount, 1);
  assert.equal(next.alarmCount, 1);
  assert.equal(next.warningAlarmCount, 1);
});

