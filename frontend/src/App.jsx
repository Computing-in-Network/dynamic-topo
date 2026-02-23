import { useEffect, useRef, useState } from 'react';
import {
  Cartesian2,
  Cartesian3,
  Color,
  EllipsoidTerrainProvider,
  HorizontalOrigin,
  LabelStyle,
  OpenStreetMapImageryProvider,
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
const SELECTED_LINK_COLOR = Color.fromCssColorString('#f94144').withAlpha(0.95);
const STALE_WARN_MS = 2500;
const STALE_ERROR_MS = 5000;
const INGEST_FPS_WARN = 0.7;
const FRAME_QUEUE_MAX = 600;
const SPEED_OPTIONS = [0.5, 1, 2];
const ORBIT_UPDATE_INTERVAL_TICKS = 4;

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
  const linkVisualStateRef = useRef(new Map());
  const pickHandlerRef = useRef(null);
  const nodeStateRef = useRef(new Map());
  const linkStateRef = useRef(new Map());
  const nodeVisibilityRef = useRef(new Map());
  const lastFrameAtRef = useRef(0);
  const frameTimestampsRef = useRef([]);
  const frameQueueRef = useRef([]);
  const orbitCacheRef = useRef(new Map());

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
    ws.onopen = () => {
      setConnected(true);
      lastFrameAtRef.current = Date.now();
      frameTimestampsRef.current = [];
      frameQueueRef.current = [];
      setQueueDepth(0);
    };
    ws.onclose = () => setConnected(false);
    ws.onerror = () => setConnected(false);
    ws.onmessage = (evt) => {
      const now = Date.now();
      lastFrameAtRef.current = now;
      frameTimestampsRef.current.push(now);
      const payload = JSON.parse(evt.data);
      frameQueueRef.current.push(payload);
      if (frameQueueRef.current.length > FRAME_QUEUE_MAX) {
        frameQueueRef.current.shift();
      }
      setQueueDepth(frameQueueRef.current.length);
    };
    return () => ws.close();
  }, []);

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
        nodeEntity.billboard.scale = selectedNode ? 1.35 : 1.0;
        nodeEntity.billboard.color = selectedNode ? SELECTED_NODE_COLOR : color;
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
    for (const edge of frame.links) {
      const a = nodeMap.get(edge.a);
      const b = nodeMap.get(edge.b);
      if (!a || !b) {
        continue;
      }
      degreeCount.set(edge.a, (degreeCount.get(edge.a) || 0) + 1);
      degreeCount.set(edge.b, (degreeCount.get(edge.b) || 0) + 1);
      const linkId = `${edge.a}-${edge.b}`;
      activeLinks.add(linkId);
      const pa = nodePositionMap.get(a.id);
      const pb = nodePositionMap.get(b.id);
      if (!pa || !pb) {
        continue;
      }
      const positions = [pa, pb];
      const selectedLink = selected?.kind === 'link' && selected.id === linkId;

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
            material: style.color
          }
        });
        linkEntitiesRef.current.set(linkId, lineEntity);
        linkVisualStateRef.current.set(linkId, {
          width: style.width,
          selected: selectedLink,
          kind: linkKind
        });
      } else {
        lineEntity.polyline.positions = positions;
      }
      const visual = linkVisualStateRef.current.get(linkId);
      const expectedWidth = selectedLink ? style.width + 1.6 : style.width;
      const widthChanged = !visual || visual.width !== expectedWidth || visual.selected !== selectedLink;
      if (widthChanged) {
        lineEntity.polyline.width = expectedWidth;
      }
      const materialChanged = !visual || visual.selected !== selectedLink || visual.kind !== linkKind;
      if (materialChanged) {
        lineEntity.polyline.material = selectedLink ? SELECTED_LINK_COLOR : style.color;
      }
      lineEntity.show = visible;
      linkVisualStateRef.current.set(linkId, {
        width: expectedWidth,
        selected: selectedLink,
        kind: linkKind
      });
    }

    for (const [id, ent] of linkEntitiesRef.current) {
      if (!activeLinks.has(id)) {
        entities.remove(ent);
        linkEntitiesRef.current.delete(id);
        linkVisualStateRef.current.delete(id);
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
  }, [frame, layerPrefs, selected]);

  const selectedNode = selected?.kind === 'node' ? nodeStateRef.current.get(selected.id) : null;
  const selectedLink = selected?.kind === 'link' ? linkStateRef.current.get(selected.id) : null;
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
        <p>t: {frame ? frame.sim_time_s.toFixed(1) : '-'} s</p>
        <p>nodes: {frame ? frame.nodes.length : 0}</p>
        <p>links: {frame ? frame.metrics.edge_count : 0}</p>
        <p>avg degree: {frame ? frame.metrics.avg_degree.toFixed(2) : '-'}</p>
        <p>mobile connected: {frame ? `${frame.metrics.mobile_connected_count ?? 0}/${(frame.nodes.filter((n) => n.type !== 'leo').length || 1)}` : '-'}</p>
        <p>mobile ratio: {frame ? `${((frame.metrics.mobile_connected_ratio ?? 0) * 100).toFixed(1)}%` : '-'}</p>
        <p>tick: {frame ? frame.elapsed_ms.toFixed(2) : '-'} ms</p>
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
        <div className="legend">
          <div className="legend-item"><span className="swatch orbit" />satellite orbit</div>
          <div className="legend-item"><span className="swatch sat-sat" />satellite-satellite link</div>
          <div className="legend-item"><span className="swatch sat-mobile" />satellite-air/ship link</div>
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
          </div>
        ) : null}
      </aside>
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
    </div>
  );
}
