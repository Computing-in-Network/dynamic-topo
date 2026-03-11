import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Cartesian2,
  Cartesian3,
  Color,
  EllipsoidTerrainProvider,
  HorizontalOrigin,
  LabelStyle,
  OpenStreetMapImageryProvider,
  PolylineDashMaterialProperty,
  ScreenSpaceEventHandler,
  ScreenSpaceEventType,
  TileMapServiceImageryProvider,
  VerticalOrigin,
  defined,
  Viewer
} from 'cesium';

const defaultWsUrl = (() => {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${window.location.hostname}:8765`;
})();
const WS_URL = import.meta.env.VITE_TOPO_WS_URL || defaultWsUrl;
const defaultMonitorBaseUrl = `${window.location.protocol}//${window.location.hostname}:9010`;
const MONITOR_BASE_URL = (import.meta.env.VITE_MONITOR_BASE_URL || defaultMonitorBaseUrl).replace(/\/$/, '');
const MONITOR_TOPOLOGY_EPOCH = import.meta.env.VITE_MONITOR_TOPOLOGY_EPOCH || '';
const MONITOR_POLL_MS = (() => {
  const parsed = Number(import.meta.env.VITE_MONITOR_POLL_MS || 2000);
  if (!Number.isFinite(parsed) || parsed < 1000) {
    return 2000;
  }
  return Math.floor(parsed);
})();
const LAYER_PREFS_KEY = 'topo_layer_prefs_v1';
const TRAIL_LEN_BY_TYPE = {
  leo: 180,
  aircraft: 260,
  ship: 300
};
const SAT_ORBIT_SAMPLES = 72;

const typeColor = {
  leo: Color.fromCssColorString('#ffb703'),
  aircraft: Color.fromCssColorString('#2a9d8f'),
  ship: Color.fromCssColorString('#00b4d8')
};
const orbitColor = {
  polar: Color.fromCssColorString('#7dff00').withAlpha(0.42),
  inclined: Color.fromCssColorString('#4df8b5').withAlpha(0.36)
};
const linkStyle = {
  sat_sat: {
    color: Color.fromCssColorString('#ff4fd8').withAlpha(0.86),
    width: 2.0
  },
  sat_mobile: {
    color: Color.fromCssColorString('#ff7f11').withAlpha(0.9),
    width: 2.2
  },
  other: {
    color: Color.fromCssColorString('#8da9c4').withAlpha(0.18),
    width: 1.0
  }
};
const defaultLayerPrefs = {
  nodeLeo: true,
  nodeAircraft: true,
  nodeShip: true,
  linkSatSat: true,
  linkSatMobile: true,
  linkOther: true,
  showTrails: true,
  showLabels: true,
  showOrbits: true
};
const SELECTED_NODE_COLOR = Color.fromCssColorString('#fff176');
const DAMAGED_NODE_COLOR = Color.fromCssColorString('#ff595e');
const SELECTED_LINK_COLOR = Color.fromCssColorString('#f94144').withAlpha(0.95);
const FAULT_LINK_COLOR = Color.fromCssColorString('#ff3b30').withAlpha(0.95);
const ROUTE_LINK_COLOR = Color.fromCssColorString('#80ffdb').withAlpha(0.96);
const ROUTE_NODE_COLOR = Color.fromCssColorString('#72ddf7');
const ROUTE_SRC_COLOR = Color.fromCssColorString('#c3f73a');
const ROUTE_DST_COLOR = Color.fromCssColorString('#ffd166');
const STALE_WARN_MS = 2500;
const STALE_ERROR_MS = 5000;
const INGEST_FPS_WARN = 0.7;
const FRAME_QUEUE_MAX = 600;
const SPEED_OPTIONS = [0.5, 1, 2];
const ORBIT_UPDATE_INTERVAL_TICKS = 4;
const ROUTE_SNAPSHOT_POLL_MS = 5000;

function svgDataUri(svg) {
  return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`;
}

function buildNodeIcon(type) {
  if (type === 'leo') {
    return svgDataUri(`
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
        <defs>
          <linearGradient id="g1" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#9eff4a"/>
            <stop offset="100%" stop-color="#4edfff"/>
          </linearGradient>
        </defs>
        <circle cx="32" cy="32" r="28" fill="#0f2133" stroke="#6ee7ff" stroke-width="2"/>
        <rect x="25" y="25" width="14" height="14" rx="2" fill="url(#g1)" stroke="#dff7ff" stroke-width="1.5"/>
        <rect x="7" y="28" width="16" height="8" rx="1.5" fill="#5c8dbf" stroke="#cbe7ff" stroke-width="1"/>
        <rect x="41" y="28" width="16" height="8" rx="1.5" fill="#5c8dbf" stroke="#cbe7ff" stroke-width="1"/>
        <line x1="32" y1="39" x2="32" y2="49" stroke="#fef08a" stroke-width="2"/>
        <circle cx="32" cy="50" r="2" fill="#fde047"/>
      </svg>
    `);
  }
  if (type === 'aircraft') {
    return svgDataUri(`
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
        <circle cx="32" cy="32" r="28" fill="#102a32" stroke="#59e1c1" stroke-width="2"/>
        <path d="M32 11 L37 25 L53 30 L53 34 L37 39 L32 53 L27 39 L11 34 L11 30 L27 25 Z"
              fill="#e7f7ff" stroke="#7ed7ff" stroke-width="1.5"/>
        <rect x="29" y="14" width="6" height="35" rx="2" fill="#8ec5ff" opacity="0.45"/>
      </svg>
    `);
  }
  return svgDataUri(`
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
      <circle cx="32" cy="32" r="28" fill="#0b2433" stroke="#60d9ff" stroke-width="2"/>
      <path d="M14 39 H50 L44 47 H20 Z" fill="#f1f5f9" stroke="#bde7ff" stroke-width="1.5"/>
      <rect x="24" y="28" width="16" height="10" rx="1.5" fill="#88b5e6" stroke="#d6efff" stroke-width="1"/>
      <rect x="29" y="23" width="6" height="5" rx="1" fill="#88b5e6"/>
      <line x1="14" y1="50" x2="50" y2="50" stroke="#4cc9f0" stroke-width="2" opacity="0.7"/>
    </svg>
  `);
}

const nodeIcon = {
  leo: buildNodeIcon('leo'),
  aircraft: buildNodeIcon('aircraft'),
  ship: buildNodeIcon('ship')
};

function toCartesian(node) {
  return Cartesian3.fromDegrees(node.lon, node.lat, node.alt_m);
}

function buildSatelliteOrbitPolyline(node) {
  if (node.type !== 'leo' || node.vx == null || node.vy == null || node.vz == null) {
    return null;
  }
  const r = new Cartesian3(node.x, node.y, node.z);
  const v = new Cartesian3(node.vx, node.vy, node.vz);
  const n = Cartesian3.cross(r, v, new Cartesian3());
  if (Cartesian3.magnitudeSquared(n) < 1e-6) {
    return null;
  }
  const u = Cartesian3.normalize(r, new Cartesian3());
  const w = Cartesian3.normalize(Cartesian3.cross(n, u, new Cartesian3()), new Cartesian3());
  const radius = Cartesian3.magnitude(r);
  const points = [];
  for (let i = 0; i <= SAT_ORBIT_SAMPLES; i += 1) {
    const theta = (2.0 * Math.PI * i) / SAT_ORBIT_SAMPLES;
    const pu = Cartesian3.multiplyByScalar(u, Math.cos(theta) * radius, new Cartesian3());
    const pw = Cartesian3.multiplyByScalar(w, Math.sin(theta) * radius, new Cartesian3());
    points.push(Cartesian3.add(pu, pw, new Cartesian3()));
  }
  return points;
}

function resolveLinkKind(a, b) {
  const aSat = a.type === 'leo';
  const bSat = b.type === 'leo';
  if (aSat && bSat) {
    return 'sat_sat';
  }
  if ((aSat && !bSat) || (!aSat && bSat)) {
    return 'sat_mobile';
  }
  return 'other';
}

function resolveLinkStyle(a, b) {
  return linkStyle[resolveLinkKind(a, b)];
}

function isNodeVisible(node, layerPrefs) {
  if (node.type === 'leo') {
    return layerPrefs.nodeLeo;
  }
  if (node.type === 'aircraft') {
    return layerPrefs.nodeAircraft;
  }
  return layerPrefs.nodeShip;
}

function isLinkVisible(linkKind, layerPrefs) {
  if (linkKind === 'sat_sat') {
    return layerPrefs.linkSatSat;
  }
  if (linkKind === 'sat_mobile') {
    return layerPrefs.linkSatMobile;
  }
  return layerPrefs.linkOther;
}

function edgeKey(a, b) {
  return a < b ? `${a}|${b}` : `${b}|${a}`;
}

function labelForNode(node) {
  if (!node) {
    return '';
  }
  return node.name || node.id || node.node_id || '';
}

function compareNodeLabels(a, b) {
  return labelForNode(a).localeCompare(labelForNode(b), undefined, {
    numeric: true,
    sensitivity: 'base'
  });
}

function buildRouteAnalysis(routeSnapshot, srcId, dstId) {
  const empty = {
    status: 'idle',
    source: null,
    target: null,
    firstHop: null,
    pathIds: [],
    pathNodes: [],
    pathEdgeKeys: [],
    componentSize: 0,
    hopCount: 0,
    snapshotUpdatedAt: null,
    snapshotFrameIndex: null,
    snapshotEdgeCount: null
  };
  if (!routeSnapshot || !srcId || !dstId) {
    return empty;
  }
  const nodes = routeSnapshot.nodes && typeof routeSnapshot.nodes === 'object' ? routeSnapshot.nodes : {};
  const routes = routeSnapshot.routes && typeof routeSnapshot.routes === 'object' ? routeSnapshot.routes : {};
  const componentSizes =
    routeSnapshot.component_sizes && typeof routeSnapshot.component_sizes === 'object'
      ? routeSnapshot.component_sizes
      : {};
  const source = nodes[srcId] || null;
  const target = nodes[dstId] || null;
  const base = {
    ...empty,
    snapshotUpdatedAt: routeSnapshot.updated_at || null,
    snapshotFrameIndex: routeSnapshot.frame_index ?? null,
    snapshotEdgeCount: routeSnapshot.edge_count ?? null
  };
  if (!source || !target) {
    return {
      ...base,
      status: 'missing',
      source,
      target
    };
  }
  if (srcId === dstId) {
    return {
      ...base,
      status: 'same-node',
      source,
      target,
      pathIds: [srcId],
      pathNodes: [source],
      componentSize: 1
    };
  }

  const maxHops = Math.max(1, Number(routeSnapshot.node_count) || Object.keys(nodes).length || 300);
  const pathIds = [srcId];
  const seen = new Set(pathIds);
  let current = srcId;
  let firstHop = null;
  for (let step = 0; step < maxHops; step += 1) {
    if (current === dstId) {
      break;
    }
    const currentRoutes = routes[current];
    const hop = currentRoutes && typeof currentRoutes === 'object' ? currentRoutes[dstId] : null;
    if (!hop || typeof hop.next_hop_node !== 'string' || !hop.next_hop_node) {
      return {
        ...base,
        status: 'unreachable',
        source,
        target,
        componentSize: Number(componentSizes[srcId]) || 0
      };
    }
    const nextId = hop.next_hop_node;
    if (!firstHop) {
      firstHop = nodes[nextId] || { node_id: nextId, id: nextId };
    }
    if (seen.has(nextId)) {
      return {
        ...base,
        status: 'loop',
        source,
        target,
        firstHop,
        pathIds,
        pathNodes: pathIds.map((id) => nodes[id] || { node_id: id, id }),
        componentSize: Number(componentSizes[srcId]) || 0
      };
    }
    pathIds.push(nextId);
    seen.add(nextId);
    current = nextId;
  }

  if (current !== dstId) {
    return {
      ...base,
      status: 'unreachable',
      source,
      target,
      firstHop,
      componentSize: Number(componentSizes[srcId]) || 0
    };
  }
  const pathNodes = pathIds.map((id) => nodes[id] || { node_id: id, id });
  const pathEdgeKeys = pathIds.slice(1).map((id, idx) => edgeKey(pathIds[idx], id));
  return {
    ...base,
    status: 'ok',
    source,
    target,
    firstHop: firstHop || pathNodes[1] || null,
    pathIds,
    pathNodes,
    pathEdgeKeys,
    componentSize: Number(componentSizes[srcId]) || 0,
    hopCount: Math.max(0, pathIds.length - 1)
  };
}

function formatPercentFromRatio(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return '-';
  }
  return `${(value * 100).toFixed(1)}%`;
}

function formatFixed(value, digits = 2) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return '-';
  }
  return value.toFixed(digits);
}

function formatBps(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return '-';
  }
  const abs = Math.abs(value);
  if (abs < 1_000) {
    return `${value.toFixed(0)} bps`;
  }
  if (abs < 1_000_000) {
    return `${(value / 1_000).toFixed(1)} Kbps`;
  }
  if (abs < 1_000_000_000) {
    return `${(value / 1_000_000).toFixed(1)} Mbps`;
  }
  return `${(value / 1_000_000_000).toFixed(2)} Gbps`;
}

function formatTimestamp(value) {
  if (typeof value !== 'string' || !value) {
    return '-';
  }
  const ts = Date.parse(value);
  if (!Number.isFinite(ts)) {
    return value;
  }
  return new Date(ts).toLocaleString();
}

function resolveNodeMetric(nodesMap, nodeId) {
  if (!nodesMap || !nodeId) {
    return null;
  }
  if (nodesMap[nodeId]) {
    return nodesMap[nodeId];
  }
  for (const payload of Object.values(nodesMap)) {
    if (!payload || typeof payload !== 'object') {
      continue;
    }
    if (payload.node_uid === nodeId || payload.node_id === nodeId || payload.docker_name === nodeId) {
      return payload;
    }
  }
  return null;
}

function resolveLinkMetric(linksMap, link) {
  if (!linksMap || !link?.a?.id || !link?.b?.id) {
    return null;
  }
  const targetKey = edgeKey(link.a.id, link.b.id);
  for (const payload of Object.values(linksMap)) {
    if (!payload || typeof payload !== 'object') {
      continue;
    }
    const src = payload.src_node_id || payload.src_node_uid;
    const dst = payload.dst_node_id || payload.dst_node_uid;
    if (typeof src === 'string' && typeof dst === 'string' && edgeKey(src, dst) === targetKey) {
      return payload;
    }
    if (payload.link_id === link.id || payload.link_uid === link.id) {
      return payload;
    }
  }
  return null;
}

export function App() {
  const [frame, setFrame] = useState(null);
  const [connected, setConnected] = useState(false);
  const [hoverInfo, setHoverInfo] = useState(null);
  const [selected, setSelected] = useState(null);
  const [runtimeHealth, setRuntimeHealth] = useState({
    stalenessMs: 0,
    ingestFps: 0
  });
  const [playback, setPlayback] = useState({
    paused: false,
    speed: 1
  });
  const [queueDepth, setQueueDepth] = useState(0);
  const [faults, setFaults] = useState([]);
  const [controlStatus, setControlStatus] = useState('');
  const [monitorSnapshot, setMonitorSnapshot] = useState(null);
  const [monitorStatus, setMonitorStatus] = useState({
    connected: false,
    error: '',
    fetchedAt: null
  });
  const [routeSnapshot, setRouteSnapshot] = useState(null);
  const [routeSnapshotStatus, setRouteSnapshotStatus] = useState({
    available: false,
    error: '',
    fetchedAt: null
  });
  const [routeQuery, setRouteQuery] = useState({
    srcId: '',
    dstId: ''
  });
  const [layerPrefs, setLayerPrefs] = useState(() => {
    try {
      const raw = window.localStorage.getItem(LAYER_PREFS_KEY);
      if (!raw) {
        return defaultLayerPrefs;
      }
      return { ...defaultLayerPrefs, ...JSON.parse(raw) };
    } catch {
      return defaultLayerPrefs;
    }
  });

  const containerRef = useRef(null);
  const viewerRef = useRef(null);
  const nodeEntitiesRef = useRef(new Map());
  const trailEntitiesRef = useRef(new Map());
  const trailPointsRef = useRef(new Map());
  const orbitEntitiesRef = useRef(new Map());
  const linkEntitiesRef = useRef(new Map());
  const faultLinkEntitiesRef = useRef(new Map());
  const linkVisualStateRef = useRef(new Map());
  const pickHandlerRef = useRef(null);
  const nodeStateRef = useRef(new Map());
  const linkStateRef = useRef(new Map());
  const nodeVisibilityRef = useRef(new Map());
  const lastFrameAtRef = useRef(0);
  const frameTimestampsRef = useRef([]);
  const frameQueueRef = useRef([]);
  const orbitCacheRef = useRef(new Map());
  const wsRef = useRef(null);

  useEffect(() => {
    window.localStorage.setItem(LAYER_PREFS_KEY, JSON.stringify(layerPrefs));
  }, [layerPrefs]);

  function toggleLayer(key) {
    setLayerPrefs((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  function resetLayerPrefs() {
    setLayerPrefs(defaultLayerPrefs);
  }

  function shiftFrameFromQueue() {
    if (frameQueueRef.current.length === 0) {
      setQueueDepth(0);
      return false;
    }
    const nextFrame = frameQueueRef.current.shift();
    setQueueDepth(frameQueueRef.current.length);
    if (nextFrame) {
      setFrame(nextFrame);
      return true;
    }
    return false;
  }

  function stepOnce() {
    setPlayback((prev) => ({ ...prev, paused: true }));
    shiftFrameFromQueue();
  }

  function sendControl(action, extra = {}) {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      if (action === 'route_snapshot') {
        setRouteSnapshotStatus((prev) => ({
          ...prev,
          available: false,
          error: '控制通道未连接'
        }));
        return;
      }
      setControlStatus('控制通道未连接');
      return;
    }
    const request_id = `req-${Date.now()}`;
    ws.send(JSON.stringify({ action, request_id, ...extra }));
  }

  function focusFaultTarget(fault) {
    const viewer = viewerRef.current;
    if (!viewer) {
      return;
    }
    if (fault.fault_type === 'DAMAGED') {
      const nodeId = fault.target?.node_id;
      if (!nodeId) {
        return;
      }
      const node = nodeStateRef.current.get(nodeId);
      if (!node) {
        return;
      }
      setSelected({ kind: 'node', id: nodeId });
      viewer.camera.flyTo({
        destination: Cartesian3.fromDegrees(node.lon, node.lat, node.alt_m + 1_200_000),
        duration: 0.8
      });
      return;
    }
    const a = fault.target?.a;
    const b = fault.target?.b;
    if (!a || !b) {
      return;
    }
    const aNode = nodeStateRef.current.get(a);
    const bNode = nodeStateRef.current.get(b);
    if (!aNode || !bNode) {
      return;
    }
    const linkIdAB = `${a}-${b}`;
    const linkIdBA = `${b}-${a}`;
    const linkIdFault = `fault-${edgeKey(a, b)}`;
    const linkId = linkStateRef.current.has(linkIdAB)
      ? linkIdAB
      : (linkStateRef.current.has(linkIdBA) ? linkIdBA : linkIdFault);
    if (linkStateRef.current.has(linkId)) {
      setSelected({ kind: 'link', id: linkId });
    }
    viewer.camera.flyTo({
      destination: Cartesian3.fromDegrees(
        (aNode.lon + bNode.lon) / 2.0,
        (aNode.lat + bNode.lat) / 2.0,
        Math.max(aNode.alt_m, bNode.alt_m) + 1_200_000
      ),
      duration: 0.8
    });
  }

  useEffect(() => {
    if (!containerRef.current || viewerRef.current) {
      return;
    }

    const viewer = new Viewer(containerRef.current, {
      imageryProvider: false,
      terrainProvider: new EllipsoidTerrainProvider(),
      animation: false,
      timeline: false,
      baseLayerPicker: false,
      geocoder: false,
      sceneModePicker: false,
      homeButton: false,
      navigationHelpButton: false,
      selectionIndicator: false,
      infoBox: false
    });

    viewer.scene.globe.baseColor = Color.fromCssColorString('#123b58');
    const localTextureUrl = `${(window.CESIUM_BASE_URL || '/cesium').replace(/\/$/, '')}/Assets/Textures/NaturalEarthII`;
    TileMapServiceImageryProvider.fromUrl(localTextureUrl)
      .then((provider) => {
        viewer.imageryLayers.addImageryProvider(provider);
      })
      .catch(() => {
        viewer.imageryLayers.addImageryProvider(
          new OpenStreetMapImageryProvider({ url: 'https://tile.openstreetmap.org/' })
        );
      });
    viewer.camera.flyTo({
      destination: Cartesian3.fromDegrees(110, 25, 22_000_000),
      duration: 0
    });
    const pickHandler = new ScreenSpaceEventHandler(viewer.scene.canvas);
    pickHandler.setInputAction((movement) => {
      const picked = viewer.scene.pick(movement.endPosition);
      if (!defined(picked) || !picked?.id?.id || typeof picked.id.id !== 'string') {
        setHoverInfo(null);
        return;
      }
      if (!picked.id.id.startsWith('node-')) {
        setHoverInfo(null);
        return;
      }
      const nodeId = picked.id.id.slice(5);
      if (!nodeVisibilityRef.current.get(nodeId)) {
        setHoverInfo(null);
        return;
      }
      const node = nodeStateRef.current.get(nodeId);
      if (!node) {
        setHoverInfo(null);
        return;
      }
      setHoverInfo({
        x: movement.endPosition.x,
        y: movement.endPosition.y,
        node
      });
    }, ScreenSpaceEventType.MOUSE_MOVE);
    pickHandler.setInputAction((movement) => {
      const picked = viewer.scene.pick(movement.position);
      if (!defined(picked) || !picked?.id?.id || typeof picked.id.id !== 'string') {
        setSelected(null);
        return;
      }
      const pickedId = picked.id.id;
      if (pickedId.startsWith('node-')) {
        const nodeId = pickedId.slice(5);
        if (!nodeVisibilityRef.current.get(nodeId)) {
          setSelected(null);
          return;
        }
        setSelected({ kind: 'node', id: nodeId });
        return;
      }
      if (pickedId.startsWith('link-')) {
        const linkId = pickedId.slice(5);
        setSelected({ kind: 'link', id: linkId });
        return;
      }
      setSelected(null);
    }, ScreenSpaceEventType.LEFT_CLICK);

    viewerRef.current = viewer;
    pickHandlerRef.current = pickHandler;

    return () => {
      pickHandler.destroy();
      viewer.destroy();
      pickHandlerRef.current = null;
      viewerRef.current = null;
    };
  }, []);

  useEffect(() => {
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;
    ws.onopen = () => {
      setConnected(true);
      lastFrameAtRef.current = Date.now();
      frameTimestampsRef.current = [];
      frameQueueRef.current = [];
      setQueueDepth(0);
      setControlStatus('');
      ws.send(JSON.stringify({ action: 'list_faults', request_id: `req-${Date.now()}` }));
    };
    ws.onclose = () => {
      setConnected(false);
      setRouteSnapshotStatus((prev) => ({
        ...prev,
        available: false,
        error: prev.fetchedAt ? 'WebSocket 已断开，展示的是最后一次已获取快照' : 'WebSocket 已断开'
      }));
      wsRef.current = null;
    };
    ws.onerror = () => {
      setConnected(false);
      setRouteSnapshotStatus((prev) => ({
        ...prev,
        available: false,
        error: prev.fetchedAt ? 'WebSocket 异常，展示的是最后一次已获取快照' : 'WebSocket 异常'
      }));
      wsRef.current = null;
    };
    ws.onmessage = (evt) => {
      const payload = JSON.parse(evt.data);
      if (payload && payload.type === 'control_ack') {
        if (payload.action === 'route_snapshot') {
          if (payload.ok && payload.route_snapshot) {
            setRouteSnapshot(payload.route_snapshot);
            setRouteSnapshotStatus({
              available: true,
              error: '',
              fetchedAt: new Date().toISOString()
            });
          } else {
            setRouteSnapshotStatus((prev) => ({
              available: false,
              error: payload.error || 'route snapshot unavailable',
              fetchedAt: prev.fetchedAt
            }));
          }
          return;
        }
        if (Array.isArray(payload.faults)) {
          setFaults(payload.faults);
        }
        if (payload.ok) {
          setControlStatus(payload.deduplicated ? '已存在相同故障，已去重' : '控制操作成功');
        } else {
          setControlStatus(payload.error || '控制操作失败');
        }
        return;
      }
      const now = Date.now();
      lastFrameAtRef.current = now;
      frameTimestampsRef.current.push(now);
      frameQueueRef.current.push(payload);
      if (frameQueueRef.current.length > FRAME_QUEUE_MAX) {
        frameQueueRef.current.shift();
      }
      setQueueDepth(frameQueueRef.current.length);
    };
    return () => {
      wsRef.current = null;
      ws.close();
    };
  }, []);

  useEffect(() => {
    if (!connected) {
      return undefined;
    }
    sendControl('route_snapshot');
    const timer = window.setInterval(() => {
      sendControl('route_snapshot');
    }, ROUTE_SNAPSHOT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [connected]);

  useEffect(() => {
    let stopped = false;
    let timer = null;
    const query = new URLSearchParams();
    if (MONITOR_TOPOLOGY_EPOCH) {
      query.set('topology_epoch', MONITOR_TOPOLOGY_EPOCH);
    }
    const queryStr = query.toString();
    const snapshotUrl = `${MONITOR_BASE_URL}/api/v1/monitor/snapshot${queryStr ? `?${queryStr}` : ''}`;

    async function pullSnapshot() {
      try {
        const resp = await fetch(snapshotUrl, {
          method: 'GET',
          headers: {
            Accept: 'application/json'
          }
        });
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        const payload = await resp.json();
        if (stopped) {
          return;
        }
        setMonitorSnapshot(payload);
        setMonitorStatus({
          connected: true,
          error: '',
          fetchedAt: new Date().toISOString()
        });
      } catch (err) {
        if (stopped) {
          return;
        }
        setMonitorStatus((prev) => ({
          connected: false,
          error: String(err?.message || err || 'fetch failed'),
          fetchedAt: prev.fetchedAt
        }));
      }
    }

    pullSnapshot();
    timer = window.setInterval(pullSnapshot, MONITOR_POLL_MS);

    return () => {
      stopped = true;
      if (timer) {
        window.clearInterval(timer);
      }
    };
  }, []);

  const routeNodeOptions = useMemo(() => {
    if (!frame?.nodes) {
      return [];
    }
    return [...frame.nodes]
      .filter((node) => node.type === 'leo')
      .sort(compareNodeLabels);
  }, [frame]);

  useEffect(() => {
    if (routeNodeOptions.length === 0) {
      return;
    }
    setRouteQuery((prev) => {
      const knownIds = new Set(routeNodeOptions.map((node) => node.id));
      const srcId = knownIds.has(prev.srcId) ? prev.srcId : routeNodeOptions[0].id;
      let dstId = knownIds.has(prev.dstId) ? prev.dstId : '';
      if (!dstId || dstId === srcId) {
        dstId = routeNodeOptions.find((node) => node.id !== srcId)?.id || srcId;
      }
      if (srcId === prev.srcId && dstId === prev.dstId) {
        return prev;
      }
      return { srcId, dstId };
    });
  }, [routeNodeOptions]);

  const routeAnalysis = useMemo(
    () => buildRouteAnalysis(routeSnapshot, routeQuery.srcId, routeQuery.dstId),
    [routeSnapshot, routeQuery]
  );
  const routePathNodeIds = useMemo(
    () => new Set(routeAnalysis.pathIds),
    [routeAnalysis.pathIds]
  );
  const routePathEdgeIds = useMemo(
    () => new Set(routeAnalysis.pathEdgeKeys),
    [routeAnalysis.pathEdgeKeys]
  );

  const damagedNodeIds = new Set(
    faults
      .filter((f) => f.fault_type === 'DAMAGED' && f.target?.node_id)
      .map((f) => f.target.node_id)
  );

  useEffect(() => {
    if (playback.paused) {
      return undefined;
    }
    const intervalMs = Math.max(80, Math.floor(1000 / playback.speed));
    const timer = window.setInterval(() => {
      shiftFrameFromQueue();
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [playback.paused, playback.speed]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const now = Date.now();
      const stamps = frameTimestampsRef.current.filter((t) => now - t <= 10_000);
      frameTimestampsRef.current = stamps;
      let ingestFps = 0;
      if (stamps.length >= 2) {
        const spanMs = Math.max(1, stamps[stamps.length - 1] - stamps[0]);
        ingestFps = ((stamps.length - 1) * 1000) / spanMs;
      }
      const stalenessMs = connected ? Math.max(0, now - (lastFrameAtRef.current || now)) : 0;
      setRuntimeHealth({ stalenessMs, ingestFps });
    }, 500);
    return () => window.clearInterval(timer);
  }, [connected]);

  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !frame) {
      return;
    }

    const entities = viewer.entities;
    const activeNodeIds = new Set();
    const nodePositionMap = new Map();
    const frameTick = Math.floor(frame.sim_time_s ?? 0);

    for (const node of frame.nodes) {
      activeNodeIds.add(node.id);
      const position = toCartesian(node);
      nodePositionMap.set(node.id, position);
      const color = typeColor[node.type] || Color.WHITE;
      const nodeVisible = isNodeVisible(node, layerPrefs);
      nodeVisibilityRef.current.set(node.id, nodeVisible);

      const selectedNode = selected?.kind === 'node' && selected.id === node.id;
      const isDamagedNode = damagedNodeIds.has(node.id);
      const isRouteSource = routeQuery.srcId === node.id;
      const isRouteTarget = routeQuery.dstId === node.id;
      const isRouteNode = routePathNodeIds.has(node.id);
      let nodeEntity = nodeEntitiesRef.current.get(node.id);
      const labelText = node.name || node.id;
      const labelScale = node.type === 'leo' ? 0.45 : 0.35;
      if (!nodeEntity) {
        nodeEntity = entities.add({
          id: `node-${node.id}`,
          name: labelText,
          position,
          billboard: {
            image: nodeIcon[node.type] || nodeIcon.leo,
            width: node.type === 'leo' ? 26 : 24,
            height: node.type === 'leo' ? 26 : 24,
            verticalOrigin: VerticalOrigin.CENTER,
            horizontalOrigin: HorizontalOrigin.CENTER,
            color,
            scale: 1.0
          },
          label: {
            text: labelText,
            show: true,
            scale: labelScale,
            fillColor: Color.WHITE,
            showBackground: true,
            backgroundColor: Color.BLACK.withAlpha(0.55),
            style: LabelStyle.FILL,
            verticalOrigin: VerticalOrigin.BOTTOM,
            pixelOffset: new Cartesian2(0, -12)
          }
        });
        nodeEntitiesRef.current.set(node.id, nodeEntity);
      } else {
        nodeEntity.position = position;
      }
      nodeEntity.show = nodeVisible;
      if (nodeEntity.billboard) {
        nodeEntity.billboard.scale = selectedNode
          ? 1.35
          : (isRouteSource || isRouteTarget ? 1.26 : (isRouteNode ? 1.14 : 1.0));
        nodeEntity.billboard.color = isDamagedNode
          ? DAMAGED_NODE_COLOR
          : (
            selectedNode
              ? SELECTED_NODE_COLOR
              : (isRouteSource ? ROUTE_SRC_COLOR : (isRouteTarget ? ROUTE_DST_COLOR : (isRouteNode ? ROUTE_NODE_COLOR : color)))
          );
      }
      if (nodeEntity.label) {
        nodeEntity.label.show = nodeVisible && layerPrefs.showLabels;
      }

      const trail = trailPointsRef.current.get(node.id) || [];
      trail.push(position);
      const trailLen = TRAIL_LEN_BY_TYPE[node.type] || 180;
      if (trail.length > trailLen) {
        trail.shift();
      }
      trailPointsRef.current.set(node.id, trail);

      let trailEntity = trailEntitiesRef.current.get(node.id);
      if (!trailEntity) {
        trailEntity = entities.add({
          id: `trail-${node.id}`,
          polyline: {
            positions: trail,
            width: node.type === 'leo' ? 1.4 : 2.1,
            material: color.withAlpha(0.45)
          }
        });
        trailEntitiesRef.current.set(node.id, trailEntity);
      } else {
        trailEntity.polyline.positions = trail;
      }
      trailEntity.show = nodeVisible && layerPrefs.showTrails;

      if (node.type === 'leo') {
        let orbitPositions = null;
        const orbitCache = orbitCacheRef.current.get(node.id);
        const shouldRecompute =
          !orbitCache ||
          orbitCache.orbitClass !== node.orbit_class ||
          frameTick - orbitCache.tick >= ORBIT_UPDATE_INTERVAL_TICKS;
        if (shouldRecompute) {
          orbitPositions = buildSatelliteOrbitPolyline(node);
          if (orbitPositions) {
            orbitCacheRef.current.set(node.id, {
              orbitClass: node.orbit_class,
              tick: frameTick,
              positions: orbitPositions
            });
          }
        } else {
          orbitPositions = orbitCache.positions;
        }
        if (orbitPositions) {
          let orbitEntity = orbitEntitiesRef.current.get(node.id);
          if (!orbitEntity) {
            orbitEntity = entities.add({
              id: `orbit-${node.id}`,
              polyline: {
                positions: orbitPositions,
                width: 1,
                material: orbitColor[node.orbit_class] || Color.WHITE.withAlpha(0.15)
              }
            });
            orbitEntitiesRef.current.set(node.id, orbitEntity);
          } else {
            orbitEntity.polyline.positions = orbitPositions;
          }
          orbitEntity.show = nodeVisible && layerPrefs.showOrbits;
        }
      }
    }

    for (const [id, ent] of nodeEntitiesRef.current) {
      if (!activeNodeIds.has(id)) {
        entities.remove(ent);
        nodeEntitiesRef.current.delete(id);
      }
    }
    for (const [id, ent] of trailEntitiesRef.current) {
      if (!activeNodeIds.has(id)) {
        entities.remove(ent);
        trailEntitiesRef.current.delete(id);
        trailPointsRef.current.delete(id);
      }
    }
    for (const [id, ent] of orbitEntitiesRef.current) {
      if (!activeNodeIds.has(id)) {
        entities.remove(ent);
        orbitEntitiesRef.current.delete(id);
        orbitCacheRef.current.delete(id);
      }
    }

    const activeLinks = new Set();
    const linkState = new Map();
    const degreeCount = new Map();
    const nodeMap = new Map(frame.nodes.map((n) => [n.id, n]));
    const faultLinkKeys = new Set(
      faults
        .filter((f) => f.fault_type === 'INTERRUPTED' && f.target?.a && f.target?.b)
        .map((f) => edgeKey(f.target.a, f.target.b))
    );
    for (const edge of frame.links) {
      const a = nodeMap.get(edge.a);
      const b = nodeMap.get(edge.b);
      if (!a || !b) {
        continue;
      }
      degreeCount.set(edge.a, (degreeCount.get(edge.a) || 0) + 1);
      degreeCount.set(edge.b, (degreeCount.get(edge.b) || 0) + 1);
      const linkId = `${edge.a}-${edge.b}`;
      const linkFaulted = faultLinkKeys.has(edgeKey(edge.a, edge.b));
      activeLinks.add(linkId);
      const pa = nodePositionMap.get(a.id);
      const pb = nodePositionMap.get(b.id);
      if (!pa || !pb) {
        continue;
      }
      const positions = [pa, pb];
      const selectedLink = selected?.kind === 'link' && selected.id === linkId;
      const isRouteLink = routePathEdgeIds.has(edgeKey(edge.a, edge.b));

      let lineEntity = linkEntitiesRef.current.get(linkId);
      const style = resolveLinkStyle(a, b);
      const linkKind = resolveLinkKind(a, b);
      const visible = isLinkVisible(linkKind, layerPrefs) && isNodeVisible(a, layerPrefs) && isNodeVisible(b, layerPrefs);
      linkState.set(linkId, {
        id: linkId,
        kind: linkKind,
        a,
        b
      });
      if (!lineEntity) {
        lineEntity = entities.add({
          id: `link-${linkId}`,
          polyline: {
            positions,
            width: style.width,
            material: linkFaulted
              ? new PolylineDashMaterialProperty({
                  color: FAULT_LINK_COLOR,
                  dashLength: 12
                })
              : style.color
          }
        });
        linkEntitiesRef.current.set(linkId, lineEntity);
        linkVisualStateRef.current.set(linkId, {
          width: style.width,
          selected: selectedLink,
          kind: linkKind,
          fault: linkFaulted,
          route: isRouteLink
        });
      } else {
        lineEntity.polyline.positions = positions;
      }
      const visual = linkVisualStateRef.current.get(linkId);
      const baseWidth = isRouteLink
        ? Math.max(style.width + 1.5, 3.4)
        : (linkFaulted ? Math.max(style.width, 2.8) : style.width);
      const expectedWidth = selectedLink ? baseWidth + 1.6 : baseWidth;
      const widthChanged = !visual || visual.width !== expectedWidth || visual.selected !== selectedLink;
      if (widthChanged) {
        lineEntity.polyline.width = expectedWidth;
      }
      const materialChanged =
        !visual ||
        visual.selected !== selectedLink ||
        visual.kind !== linkKind ||
        visual.fault !== linkFaulted ||
        visual.route !== isRouteLink;
      if (materialChanged) {
        if (selectedLink) {
          lineEntity.polyline.material = SELECTED_LINK_COLOR;
        } else if (linkFaulted) {
          lineEntity.polyline.material = new PolylineDashMaterialProperty({
            color: FAULT_LINK_COLOR,
            dashLength: 12
          });
        } else if (isRouteLink) {
          lineEntity.polyline.material = ROUTE_LINK_COLOR;
        } else {
          lineEntity.polyline.material = style.color;
        }
      }
      lineEntity.show = visible;
      linkVisualStateRef.current.set(linkId, {
        width: expectedWidth,
        selected: selectedLink,
        kind: linkKind,
        fault: linkFaulted,
        route: isRouteLink
      });
    }

    for (const [id, ent] of linkEntitiesRef.current) {
      if (!activeLinks.has(id)) {
        entities.remove(ent);
        linkEntitiesRef.current.delete(id);
        linkVisualStateRef.current.delete(id);
      }
    }

    const activeFaultLinks = new Set();
    for (const fault of faults) {
      if (fault.fault_type !== 'INTERRUPTED') {
        continue;
      }
      const aId = fault.target?.a;
      const bId = fault.target?.b;
      if (!aId || !bId) {
        continue;
      }
      const a = nodeMap.get(aId);
      const b = nodeMap.get(bId);
      if (!a || !b) {
        continue;
      }
      const normalAB = `${aId}-${bId}`;
      const normalBA = `${bId}-${aId}`;
      if (activeLinks.has(normalAB) || activeLinks.has(normalBA)) {
        continue;
      }
      const pa = nodePositionMap.get(aId);
      const pb = nodePositionMap.get(bId);
      if (!pa || !pb) {
        continue;
      }
      const faultId = `fault-${edgeKey(aId, bId)}`;
      activeFaultLinks.add(faultId);
      const selectedFault = selected?.kind === 'link' && selected.id === faultId;
      const linkKind = resolveLinkKind(a, b);
      const visible = isLinkVisible(linkKind, layerPrefs) && isNodeVisible(a, layerPrefs) && isNodeVisible(b, layerPrefs);
      let faultEntity = faultLinkEntitiesRef.current.get(faultId);
      if (!faultEntity) {
        faultEntity = entities.add({
          id: `link-${faultId}`,
          polyline: {
            positions: [pa, pb],
            width: 2.8,
            material: new PolylineDashMaterialProperty({
              color: FAULT_LINK_COLOR,
              dashLength: 12
            })
          }
        });
        faultLinkEntitiesRef.current.set(faultId, faultEntity);
      } else {
        faultEntity.polyline.positions = [pa, pb];
      }
      faultEntity.polyline.width = selectedFault ? 4.2 : 2.8;
      faultEntity.polyline.material = selectedFault
        ? SELECTED_LINK_COLOR
        : new PolylineDashMaterialProperty({
            color: FAULT_LINK_COLOR,
            dashLength: 12
          });
      faultEntity.show = visible;
      linkState.set(faultId, {
        id: faultId,
        kind: linkKind,
        a,
        b
      });
    }
    for (const [id, ent] of faultLinkEntitiesRef.current) {
      if (!activeFaultLinks.has(id)) {
        entities.remove(ent);
        faultLinkEntitiesRef.current.delete(id);
      }
    }

    const nodeState = new Map();
    for (const node of frame.nodes) {
      const degree = degreeCount.get(node.id) || 0;
      nodeState.set(node.id, {
        ...node,
        degree,
        has_link: degree > 0
      });
    }
    nodeStateRef.current = nodeState;
    linkStateRef.current = linkState;

    if (selected?.kind === 'node') {
      const node = nodeState.get(selected.id);
      if (!node || !isNodeVisible(node, layerPrefs)) {
        setSelected(null);
      }
    }
    if (selected?.kind === 'link') {
      const link = linkState.get(selected.id);
      if (!link || !isNodeVisible(link.a, layerPrefs) || !isNodeVisible(link.b, layerPrefs) || !isLinkVisible(link.kind, layerPrefs)) {
        setSelected(null);
      }
    }
  }, [frame, layerPrefs, selected, faults, routePathEdgeIds, routePathNodeIds, routeQuery]);

  const selectedNode = selected?.kind === 'node' ? nodeStateRef.current.get(selected.id) : null;
  const selectedLink = selected?.kind === 'link' ? linkStateRef.current.get(selected.id) : null;
  const monitorData = monitorSnapshot?.monitor || null;
  const monitorNodes = monitorData?.nodes || null;
  const monitorLinks = monitorData?.links || null;
  const monitorNodeCount = monitorNodes ? Object.keys(monitorNodes).length : 0;
  const monitorLinkCount = monitorLinks ? Object.keys(monitorLinks).length : 0;
  const selectedNodeMetric = useMemo(
    () => resolveNodeMetric(monitorNodes, selectedNode?.id),
    [monitorNodes, selectedNode]
  );
  const selectedLinkMetric = useMemo(
    () => resolveLinkMetric(monitorLinks, selectedLink),
    [monitorLinks, selectedLink]
  );
  const alerts = [];
  if (!connected) {
    alerts.push({ level: 'error', text: 'WebSocket 已断开' });
  }
  if (connected && runtimeHealth.stalenessMs >= STALE_ERROR_MS) {
    alerts.push({ level: 'error', text: `数据延迟过高：${(runtimeHealth.stalenessMs / 1000).toFixed(1)}s` });
  } else if (connected && runtimeHealth.stalenessMs >= STALE_WARN_MS) {
    alerts.push({ level: 'warn', text: `数据延迟偏高：${(runtimeHealth.stalenessMs / 1000).toFixed(1)}s` });
  }
  if (connected && runtimeHealth.ingestFps > 0 && runtimeHealth.ingestFps < INGEST_FPS_WARN) {
    alerts.push({ level: 'warn', text: `帧率偏低：${runtimeHealth.ingestFps.toFixed(2)} fps` });
  }

  return (
    <div className="app-shell">
      <div className="hud">
        <h1>Dynamic Topology - Deploy Check 2026-02-20</h1>
        <p>Status: {connected ? 'connected' : 'disconnected'}</p>
        <div className="status-badges">
          <span className={`badge ${connected ? 'ok' : 'error'}`}>{connected ? '连接正常' : '连接中断'}</span>
          <span className={`badge ${runtimeHealth.stalenessMs >= STALE_ERROR_MS ? 'error' : runtimeHealth.stalenessMs >= STALE_WARN_MS ? 'warn' : 'ok'}`}>
            延迟 {runtimeHealth.stalenessMs}ms
          </span>
          <span className={`badge ${runtimeHealth.ingestFps > 0 && runtimeHealth.ingestFps < INGEST_FPS_WARN ? 'warn' : 'ok'}`}>
            帧率 {runtimeHealth.ingestFps.toFixed(2)}fps
          </span>
          <span className={`badge ${monitorStatus.connected ? 'ok' : 'warn'}`}>
            监控 {monitorStatus.connected ? '已接入' : '未连接'}
          </span>
        </div>
        <div className="time-controls">
          <button type="button" onClick={() => setPlayback((p) => ({ ...p, paused: !p.paused }))}>
            {playback.paused ? '继续' : '暂停'}
          </button>
          <button type="button" onClick={stepOnce}>单步</button>
          {SPEED_OPTIONS.map((sp) => (
            <button
              type="button"
              key={`speed-${sp}`}
              className={playback.speed === sp ? 'active' : ''}
              onClick={() => setPlayback((p) => ({ ...p, speed: sp }))}
            >
              {sp}x
            </button>
          ))}
          <span className="queue-chip">缓冲 {queueDepth}</span>
        </div>
        <p>WS: {WS_URL}</p>
        <p>Monitor: {MONITOR_BASE_URL}</p>
        <p>monitor nodes: {monitorNodeCount}</p>
        <p>monitor links: {monitorLinkCount}</p>
        <p>monitor epoch: {monitorData?.topology_epoch || '-'}</p>
        <p>monitor updated: {formatTimestamp(monitorData?.updated_at || monitorStatus.fetchedAt)}</p>
        {!monitorStatus.connected && monitorStatus.error ? (
          <p>monitor error: {monitorStatus.error}</p>
        ) : null}
        <p>t: {frame ? frame.sim_time_s.toFixed(1) : '-'} s</p>
        <p>nodes: {frame ? frame.nodes.length : 0}</p>
        <p>links: {frame ? frame.metrics.edge_count : 0}</p>
        <p>avg degree: {frame ? frame.metrics.avg_degree.toFixed(2) : '-'}</p>
        <p>mobile connected: {frame ? `${frame.metrics.mobile_connected_count ?? 0}/${(frame.nodes.filter((n) => n.type !== 'leo').length || 1)}` : '-'}</p>
        <p>mobile ratio: {frame ? `${((frame.metrics.mobile_connected_ratio ?? 0) * 100).toFixed(1)}%` : '-'}</p>
        <p>I(QoE-Imbalance): {frame ? (frame.metrics.qoe_imbalance ?? 0).toFixed(4) : '-'}</p>
        <p>fault nodes: {frame ? frame.metrics.fault_node_count ?? 0 : 0}</p>
        <p>fault links: {frame ? frame.metrics.fault_link_count ?? 0 : 0}</p>
        <p>tick: {frame ? frame.elapsed_ms.toFixed(2) : '-'} ms</p>
        <p>control: {controlStatus || '-'}</p>
        <div className="alert-box">
          {alerts.length === 0 ? (
            <div className="alert-row ok">当前无告警</div>
          ) : (
            alerts.map((a, idx) => (
              <div key={`${a.level}-${idx}`} className={`alert-row ${a.level}`}>
                {a.text}
              </div>
            ))
          )}
        </div>
        <div className="route-panel">
          <div className="layer-header">
            <span>静态路由演示</span>
            <button
              type="button"
              onClick={() => setRouteQuery((prev) => ({ srcId: prev.dstId, dstId: prev.srcId }))}
            >
              交换
            </button>
          </div>
          <div className="route-hint">
            面板读取的是路由控制器最近一次成功下发的只读快照，不再使用前端本地重算结果。
          </div>
          <div className="route-grid">
            <label>
              <span>起点</span>
              <select
                value={routeQuery.srcId}
                onChange={(evt) => setRouteQuery((prev) => ({ ...prev, srcId: evt.target.value }))}
              >
                {routeNodeOptions.map((node) => (
                  <option key={`src-${node.id}`} value={node.id}>
                    {labelForNode(node)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>终点</span>
              <select
                value={routeQuery.dstId}
                onChange={(evt) => setRouteQuery((prev) => ({ ...prev, dstId: evt.target.value }))}
              >
                {routeNodeOptions.map((node) => (
                  <option key={`dst-${node.id}`} value={node.id}>
                    {labelForNode(node)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="route-meta">
            <span className={`badge ${routeSnapshotStatus.available ? 'ok' : 'warn'}`}>
              快照 {routeSnapshotStatus.available ? '已加载' : '未加载'}
            </span>
            <span className={`badge ${
              routeAnalysis.status === 'ok' || routeAnalysis.status === 'same-node'
                ? 'ok'
                : (routeAnalysis.status === 'unreachable' || routeAnalysis.status === 'loop' ? 'error' : 'warn')
            }`}>
              {routeAnalysis.status === 'ok'
                ? '当前可达'
                : routeAnalysis.status === 'same-node'
                  ? '同一节点'
                  : routeAnalysis.status === 'unreachable'
                    ? '当前不可达'
                    : routeAnalysis.status === 'loop'
                      ? '路由异常'
                    : '等待路径'}
            </span>
            <span className="badge ok">连通分量 {routeAnalysis.componentSize}</span>
            <span className="badge ok">跳数 {routeAnalysis.hopCount}</span>
          </div>
          <div className="route-summary">
            <div>快照时间: {formatTimestamp(routeAnalysis.snapshotUpdatedAt || routeSnapshotStatus.fetchedAt)}</div>
            <div>已下发帧: {routeAnalysis.snapshotFrameIndex ?? '-'}</div>
            <div>已下发边数: {routeAnalysis.snapshotEdgeCount ?? '-'}</div>
          </div>
          {!routeSnapshotStatus.available && routeSnapshotStatus.error ? (
            <div className="route-empty">{routeSnapshotStatus.error}</div>
          ) : null}
          {routeAnalysis.status === 'unreachable' ? (
            <div className="route-empty">
              当前两点不在同一连通分量，控制器不会为这对节点下发到达终点的静态路由。
            </div>
          ) : null}
          {routeAnalysis.status === 'loop' ? (
            <div className="route-empty">
              最近一次已下发快照里检测到路径环路，这说明控制器状态异常，需要先检查 `push_static_routes.py` 日志。
            </div>
          ) : null}
          {(routeAnalysis.status === 'ok' || routeAnalysis.status === 'same-node') ? (
            <>
              <div className="route-summary">
                <div>起点: {labelForNode(routeAnalysis.source)}</div>
                <div>终点: {labelForNode(routeAnalysis.target)}</div>
                <div>第一跳: {routeAnalysis.firstHop ? labelForNode(routeAnalysis.firstHop) : '-'}</div>
              </div>
              <div className="route-path-strip">
                {routeAnalysis.pathNodes.map((node, idx) => (
                  <span
                    key={`route-hop-${node.id}`}
                    className={`route-pill ${
                      idx === 0
                        ? 'src'
                        : (idx === routeAnalysis.pathNodes.length - 1 ? 'dst' : 'mid')
                    }`}
                  >
                    {idx > 0 ? <span className="route-arrow">→</span> : null}
                    {labelForNode(node)}
                  </span>
                ))}
              </div>
            </>
          ) : null}
        </div>
        <div className="legend">
          <div className="legend-item"><span className="swatch orbit" />satellite orbit</div>
          <div className="legend-item"><span className="swatch sat-sat" />satellite-satellite link</div>
          <div className="legend-item"><span className="swatch sat-mobile" />satellite-air/ship link</div>
          <div className="legend-item"><span className="swatch route" />static route highlight</div>
        </div>
        <div className="layer-panel">
          <div className="layer-header">
            <span>图层控制</span>
            <button type="button" onClick={resetLayerPrefs}>重置</button>
          </div>
          <div className="layer-grid">
            <label><input type="checkbox" checked={layerPrefs.nodeLeo} onChange={() => toggleLayer('nodeLeo')} /> 卫星</label>
            <label><input type="checkbox" checked={layerPrefs.nodeAircraft} onChange={() => toggleLayer('nodeAircraft')} /> 飞机</label>
            <label><input type="checkbox" checked={layerPrefs.nodeShip} onChange={() => toggleLayer('nodeShip')} /> 舰船</label>
            <label><input type="checkbox" checked={layerPrefs.linkSatSat} onChange={() => toggleLayer('linkSatSat')} /> 星间链路</label>
            <label><input type="checkbox" checked={layerPrefs.linkSatMobile} onChange={() => toggleLayer('linkSatMobile')} /> 星地/空链路</label>
            <label><input type="checkbox" checked={layerPrefs.linkOther} onChange={() => toggleLayer('linkOther')} /> 非卫星链路</label>
            <label><input type="checkbox" checked={layerPrefs.showTrails} onChange={() => toggleLayer('showTrails')} /> 轨迹</label>
            <label><input type="checkbox" checked={layerPrefs.showOrbits} onChange={() => toggleLayer('showOrbits')} /> 轨道环</label>
            <label><input type="checkbox" checked={layerPrefs.showLabels} onChange={() => toggleLayer('showLabels')} /> 标签</label>
          </div>
        </div>
        <div className="fault-panel">
          <div className="layer-header">
            <span>故障面板</span>
            <button type="button" onClick={() => sendControl('list_faults')}>刷新</button>
          </div>
          <div className="fault-list">
            {faults.length === 0 ? (
              <div className="fault-empty">当前无故障注入</div>
            ) : (
              faults.map((fault) => (
                <div key={fault.fault_id} className="fault-row">
                  <div className="fault-row-title">{fault.fault_type}</div>
                  <div className="fault-row-target">
                    {fault.fault_type === 'DAMAGED'
                      ? `node=${fault.target?.node_id || '-'}`
                      : `a=${fault.target?.a || '-'}, b=${fault.target?.b || '-'}`}
                  </div>
                  <div className="fault-row-actions">
                    <button type="button" onClick={() => focusFaultTarget(fault)}>定位</button>
                    <button type="button" onClick={() => sendControl('clear_fault', { fault_id: fault.fault_id })}>解除</button>
                  </div>
                </div>
              ))
            )}
          </div>
          <div className="fault-row-actions fault-footer-actions">
            <button type="button" onClick={() => sendControl('clear_all_faults')}>解除全部故障</button>
          </div>
        </div>
      </div>
      {hoverInfo ? (
        <div
          className="node-tooltip"
          style={{
            left: `${Math.min(hoverInfo.x + 14, window.innerWidth - 260)}px`,
            top: `${Math.min(hoverInfo.y + 14, window.innerHeight - 180)}px`
          }}
        >
          <div className="title">{hoverInfo.node.name}</div>
          <div>id: {hoverInfo.node.id}</div>
          <div>type: {hoverInfo.node.category}</div>
          <div>orbit: {hoverInfo.node.orbit_class || '-'}</div>
          <div>links: {hoverInfo.node.has_link ? `yes (degree ${hoverInfo.node.degree})` : 'no'}</div>
          <div>alt: {hoverInfo.node.alt_m.toFixed(0)} m</div>
        </div>
      ) : null}
      <aside className={`detail-panel ${selected ? 'show' : ''}`}>
        <div className="detail-header">
          <strong>详情侧栏</strong>
          <button type="button" onClick={() => setSelected(null)}>关闭</button>
        </div>
        {!selectedNode && !selectedLink ? (
          <div className="detail-empty">点击节点或链路查看详情</div>
        ) : null}
        {selectedNode ? (
          <div className="detail-block">
            <div className="detail-title">节点 {selectedNode.name}</div>
            <div>id: {selectedNode.id}</div>
            <div>类别: {selectedNode.category}</div>
            <div>轨道: {selectedNode.orbit_class || '-'}</div>
            <div>纬度: {selectedNode.lat.toFixed(3)}</div>
            <div>经度: {selectedNode.lon.toFixed(3)}</div>
            <div>高度: {selectedNode.alt_m.toFixed(0)} m</div>
            <div>连通: {selectedNode.has_link ? `是（度 ${selectedNode.degree}）` : '否'}</div>
            <div className="detail-subtitle">监控指标</div>
            {selectedNodeMetric ? (
              <div className="metrics-grid">
                <div>状态: {selectedNodeMetric.status || '-'}</div>
                <div>CPU 利用率: {formatPercentFromRatio(selectedNodeMetric.cpu_ratio)}</div>
                <div>内存利用率: {formatPercentFromRatio(selectedNodeMetric.mem_ratio)}</div>
                <div>发送速率: {formatBps(selectedNodeMetric.tx_bps)}</div>
                <div>接收速率: {formatBps(selectedNodeMetric.rx_bps)}</div>
                <div>连接数: {formatFixed(selectedNodeMetric.conn_count, 0)}</div>
                <div>更新时间: {formatTimestamp(selectedNodeMetric.timestamp)}</div>
              </div>
            ) : (
              <div className="metric-empty">暂无节点监控数据</div>
            )}
            <div className="detail-actions">
              <button type="button" onClick={() => sendControl('inject_node_fault', { node_id: selectedNode.id })}>
                注入节点故障
              </button>
              {selectedNode.type === 'leo' ? (
                <>
                  <button type="button" onClick={() => setRouteQuery((prev) => ({ ...prev, srcId: selectedNode.id }))}>
                    设为路由起点
                  </button>
                  <button type="button" onClick={() => setRouteQuery((prev) => ({ ...prev, dstId: selectedNode.id }))}>
                    设为路由终点
                  </button>
                </>
              ) : null}
            </div>
          </div>
        ) : null}
        {selectedLink ? (
          <div className="detail-block">
            <div className="detail-title">链路 {selectedLink.id}</div>
            <div>类型: {selectedLink.kind}</div>
            <div>A: {selectedLink.a.name} ({selectedLink.a.id})</div>
            <div>B: {selectedLink.b.name} ({selectedLink.b.id})</div>
            <div>A 类别: {selectedLink.a.category}</div>
            <div>B 类别: {selectedLink.b.category}</div>
            <div>A 高度: {selectedLink.a.alt_m.toFixed(0)} m</div>
            <div>B 高度: {selectedLink.b.alt_m.toFixed(0)} m</div>
            <div className="detail-subtitle">监控指标</div>
            {selectedLinkMetric ? (
              <div className="metrics-grid">
                <div>状态: {selectedLinkMetric.state || '-'}</div>
                <div>时延 RTT: {formatFixed(selectedLinkMetric.rtt_ms)} ms</div>
                <div>抖动: {formatFixed(selectedLinkMetric.jitter_ms)} ms</div>
                <div>丢包率: {formatPercentFromRatio(selectedLinkMetric.loss_rate)}</div>
                <div>信噪比: {formatFixed(selectedLinkMetric.snr_db)} dB</div>
                <div>误码率: {formatFixed(selectedLinkMetric.ber, 6)}</div>
                <div>更新时间: {formatTimestamp(selectedLinkMetric.timestamp)}</div>
              </div>
            ) : (
              <div className="metric-empty">暂无链路监控数据</div>
            )}
            <div className="detail-actions">
              <button
                type="button"
                onClick={() => sendControl('inject_link_fault', { a: selectedLink.a.id, b: selectedLink.b.id })}
              >
                注入链路故障
              </button>
            </div>
          </div>
        ) : null}
      </aside>
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
    </div>
  );
}
