import { MONITOR_EVENT_KIND, MONITOR_SCHEMA_VERSION } from './types.js';
import { getMonitorThresholds } from './config.js';

function metricLevel(value, threshold) {
  if (value == null || Number.isNaN(value)) {
    return 'unknown';
  }
  if (value > threshold.critical) {
    return 'critical';
  }
  if (value >= threshold.warning) {
    return 'warning';
  }
  return 'normal';
}

function maxLevel(levels) {
  if (levels.includes('critical')) {
    return 'critical';
  }
  if (levels.includes('warning')) {
    return 'warning';
  }
  if (levels.includes('normal')) {
    return 'normal';
  }
  return 'unknown';
}

export function createEmptyMonitorSnapshot() {
  return {
    updatedAt: null,
    health: 'unknown',
    nodeCount: 0,
    linkCount: 0,
    flowCount: 0,
    alarmCount: 0,
    criticalAlarmCount: 0,
    warningAlarmCount: 0,
    topAlarms: [],
    byNode: {},
    byLink: {},
    byFlow: {}
  };
}

function upsertAlarm(snapshot, alarm) {
  const severity = alarm.severity || 'info';
  const normalized = {
    id: alarm.alarm_id || alarm.message_id || `alarm-${Date.now()}`,
    severity,
    title: alarm.title || '未命名告警',
    scopeType: alarm.scope_type || 'unknown',
    scopeUid: alarm.scope_uid || '',
    scopeId: alarm.scope_id || '-',
    detail: alarm.detail || '',
    timestamp: alarm.timestamp || new Date().toISOString()
  };
  const exists = snapshot.topAlarms.some((item) => item.id === normalized.id);
  if (!exists) {
    snapshot.topAlarms.unshift(normalized);
  }
  snapshot.topAlarms = snapshot.topAlarms.slice(0, 20);
}

export function applyMonitorEvent(snapshot, inputEvent, options = {}) {
  const threshold = options.thresholds || getMonitorThresholds();
  if (!inputEvent || typeof inputEvent !== 'object') {
    return snapshot;
  }
  const event = { ...inputEvent };
  if (event.schema_version && event.schema_version !== MONITOR_SCHEMA_VERSION) {
    return snapshot;
  }
  const next = {
    ...snapshot,
    byNode: { ...snapshot.byNode },
    byLink: { ...snapshot.byLink },
    byFlow: { ...snapshot.byFlow },
    topAlarms: [...snapshot.topAlarms]
  };
  next.updatedAt = event.timestamp || new Date().toISOString();

  if (event.kind === MONITOR_EVENT_KIND.NODE_METRIC) {
    if (!event.node_id) {
      return snapshot;
    }
    const levels = [
      metricLevel(event.cpu_ratio, threshold.cpu_ratio),
      metricLevel(event.mem_ratio, threshold.mem_ratio)
    ];
    next.byNode[event.node_id] = {
      nodeId: event.node_id,
      nodeUid: event.node_uid || event.docker_name || event.node_id,
      topoNodeId: event.topo_node_id || event.node_id,
      dockerName: event.docker_name || '',
      dockerIp: event.docker_ip || '',
      cpuRatio: event.cpu_ratio ?? null,
      memRatio: event.mem_ratio ?? null,
      txBps: event.tx_bps ?? null,
      rxBps: event.rx_bps ?? null,
      status: event.status || 'UNKNOWN',
      health: maxLevel(levels)
    };
  }

  if (event.kind === MONITOR_EVENT_KIND.LINK_METRIC) {
    if (!event.link_id) {
      return snapshot;
    }
    const levels = [
      metricLevel(event.loss_rate, threshold.loss_rate),
      metricLevel(event.rtt_ms, threshold.rtt_ms),
      metricLevel(event.jitter_ms, threshold.jitter_ms)
    ];
    next.byLink[event.link_id] = {
      linkId: event.link_id,
      linkUid: event.link_uid || event.link_id,
      srcNodeId: event.src_node_id || '',
      dstNodeId: event.dst_node_id || '',
      srcNodeUid: event.src_node_uid || event.src_node_id || '',
      dstNodeUid: event.dst_node_uid || event.dst_node_id || '',
      state: event.state || 'UNKNOWN',
      lossRate: event.loss_rate ?? null,
      rttMs: event.rtt_ms ?? null,
      jitterMs: event.jitter_ms ?? null,
      health: maxLevel(levels)
    };
  }

  if (event.kind === MONITOR_EVENT_KIND.FLOW) {
    if (!event.flow_id) {
      return snapshot;
    }
    next.byFlow[event.flow_id] = {
      flowId: event.flow_id,
      srcNodeId: event.src_node_id || '',
      dstNodeId: event.dst_node_id || '',
      srcNodeUid: event.src_node_uid || event.src_node_id || '',
      dstNodeUid: event.dst_node_uid || event.dst_node_id || '',
      bps: event.bps ?? null,
      path: Array.isArray(event.path) ? event.path : [],
      priority: event.priority || ''
    };
  }

  if (event.kind === MONITOR_EVENT_KIND.ALARM) {
    upsertAlarm(next, event);
  }

  const nodeList = Object.values(next.byNode);
  const linkList = Object.values(next.byLink);
  const flowList = Object.values(next.byFlow);
  const criticalAlarmCount = next.topAlarms.filter((alarm) => alarm.severity === 'critical').length;
  const warningAlarmCount = next.topAlarms.filter((alarm) => alarm.severity === 'warning').length;

  next.nodeCount = nodeList.length;
  next.linkCount = linkList.length;
  next.flowCount = flowList.length;
  next.alarmCount = next.topAlarms.length;
  next.criticalAlarmCount = criticalAlarmCount;
  next.warningAlarmCount = warningAlarmCount;

  const overallLevels = [
    ...nodeList.map((item) => item.health),
    ...linkList.map((item) => item.health),
    criticalAlarmCount > 0 ? 'critical' : 'normal',
    warningAlarmCount > 0 ? 'warning' : 'normal'
  ];
  next.health = maxLevel(overallLevels);
  return next;
}

export function applyMonitorSnapshot(snapshot, rawSnapshot, options = {}) {
  const threshold = options.thresholds || getMonitorThresholds();
  if (!rawSnapshot || typeof rawSnapshot !== 'object') {
    return snapshot;
  }
  const monitor = rawSnapshot.monitor && typeof rawSnapshot.monitor === 'object'
    ? rawSnapshot.monitor
    : rawSnapshot;

  const next = createEmptyMonitorSnapshot();
  next.updatedAt = monitor.updated_at || new Date().toISOString();

  const nodes = monitor.nodes || {};
  const links = monitor.links || {};
  const alarms = Array.isArray(monitor.alarms) ? monitor.alarms : [];
  const nodeEntries = Array.isArray(nodes) ? nodes : Object.values(nodes);
  const linkEntries = Array.isArray(links) ? links : Object.values(links);

  for (const item of nodeEntries) {
    if (!item || !item.node_id) {
      continue;
    }
    next.byNode[item.node_id] = {
      nodeId: item.node_id,
      nodeUid: item.node_uid || item.docker_name || item.node_id,
      topoNodeId: item.topo_node_id || item.node_id,
      dockerName: item.docker_name || '',
      dockerIp: item.docker_ip || '',
      cpuRatio: item.cpu_ratio ?? null,
      memRatio: item.mem_ratio ?? null,
      txBps: item.tx_bps ?? null,
      rxBps: item.rx_bps ?? null,
      status: item.status || 'UNKNOWN',
      health: maxLevel([
        metricLevel(item.cpu_ratio, threshold.cpu_ratio),
        metricLevel(item.mem_ratio, threshold.mem_ratio)
      ])
    };
  }

  for (const item of linkEntries) {
    if (!item || !item.link_id) {
      continue;
    }
    next.byLink[item.link_id] = {
      linkId: item.link_id,
      linkUid: item.link_uid || item.link_id,
      srcNodeId: item.src_node_id || '',
      dstNodeId: item.dst_node_id || '',
      srcNodeUid: item.src_node_uid || item.src_node_id || '',
      dstNodeUid: item.dst_node_uid || item.dst_node_id || '',
      state: item.state || 'UNKNOWN',
      lossRate: item.loss_rate ?? null,
      rttMs: item.rtt_ms ?? null,
      jitterMs: item.jitter_ms ?? null,
      health: maxLevel([
        metricLevel(item.loss_rate, threshold.loss_rate),
        metricLevel(item.rtt_ms, threshold.rtt_ms),
        metricLevel(item.jitter_ms, threshold.jitter_ms)
      ])
    };
  }

  for (const alarm of alarms) {
    upsertAlarm(next, alarm);
  }

  const nodeList = Object.values(next.byNode);
  const linkList = Object.values(next.byLink);
  next.nodeCount = nodeList.length;
  next.linkCount = linkList.length;
  next.flowCount = Object.keys(next.byFlow).length;
  next.alarmCount = next.topAlarms.length;
  next.criticalAlarmCount = next.topAlarms.filter((item) => item.severity === 'critical').length;
  next.warningAlarmCount = next.topAlarms.filter((item) => item.severity === 'warning').length;
  next.health = maxLevel([
    ...nodeList.map((item) => item.health),
    ...linkList.map((item) => item.health),
    next.criticalAlarmCount > 0 ? 'critical' : 'normal',
    next.warningAlarmCount > 0 ? 'warning' : 'normal'
  ]);
  return next;
}
