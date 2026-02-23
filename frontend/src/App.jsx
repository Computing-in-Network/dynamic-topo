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
  const pickHandlerRef = useRef(null);
  const nodeStateRef = useRef(new Map());
  const nodeVisibilityRef = useRef(new Map());

  useEffect(() => {
    window.localStorage.setItem(LAYER_PREFS_KEY, JSON.stringify(layerPrefs));
  }, [layerPrefs]);

  function toggleLayer(key) {
    setLayerPrefs((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  function resetLayerPrefs() {
    setLayerPrefs(defaultLayerPrefs);
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
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onerror = () => setConnected(false);
    ws.onmessage = (evt) => {
      const payload = JSON.parse(evt.data);
      setFrame(payload);
    };
    return () => ws.close();
  }, []);

  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !frame) {
      return;
    }

    const entities = viewer.entities;
    const activeNodeIds = new Set();

    for (const node of frame.nodes) {
      activeNodeIds.add(node.id);
      const position = toCartesian(node);
      const color = typeColor[node.type] || Color.WHITE;
      const nodeVisible = isNodeVisible(node, layerPrefs);
      nodeVisibilityRef.current.set(node.id, nodeVisible);

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
        const orbitPositions = buildSatelliteOrbitPolyline(node);
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
      }
    }

    const activeLinks = new Set();
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
      const positions = [toCartesian(a), toCartesian(b)];

      let lineEntity = linkEntitiesRef.current.get(linkId);
      const style = resolveLinkStyle(a, b);
      const linkKind = resolveLinkKind(a, b);
      const visible = isLinkVisible(linkKind, layerPrefs) && isNodeVisible(a, layerPrefs) && isNodeVisible(b, layerPrefs);
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
      } else {
        lineEntity.polyline.positions = positions;
        lineEntity.polyline.width = style.width;
        lineEntity.polyline.material = style.color;
      }
      lineEntity.show = visible;
    }

    for (const [id, ent] of linkEntitiesRef.current) {
      if (!activeLinks.has(id)) {
        entities.remove(ent);
        linkEntitiesRef.current.delete(id);
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
  }, [frame, layerPrefs]);

  return (
    <div className="app-shell">
      <div className="hud">
        <h1>Dynamic Topology - Deploy Check 2026-02-20</h1>
        <p>Status: {connected ? 'connected' : 'disconnected'}</p>
        <p>WS: {WS_URL}</p>
        <p>t: {frame ? frame.sim_time_s.toFixed(1) : '-'} s</p>
        <p>nodes: {frame ? frame.nodes.length : 0}</p>
        <p>links: {frame ? frame.metrics.edge_count : 0}</p>
        <p>avg degree: {frame ? frame.metrics.avg_degree.toFixed(2) : '-'}</p>
        <p>mobile connected: {frame ? `${frame.metrics.mobile_connected_count ?? 0}/${(frame.nodes.filter((n) => n.type !== 'leo').length || 1)}` : '-'}</p>
        <p>mobile ratio: {frame ? `${((frame.metrics.mobile_connected_ratio ?? 0) * 100).toFixed(1)}%` : '-'}</p>
        <p>tick: {frame ? frame.elapsed_ms.toFixed(2) : '-'} ms</p>
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
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
    </div>
  );
}
