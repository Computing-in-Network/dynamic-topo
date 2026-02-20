import { useEffect, useMemo, useRef, useState } from 'react';
import { Cartesian2, Cartesian3, Color, Math as CesiumMath, VerticalOrigin } from 'cesium';
import { CameraFlyTo, Entity, PolylineGraphics, Viewer } from 'resium';

const WS_URL = import.meta.env.VITE_TOPO_WS_URL || 'ws://localhost:8765';
const TRAIL_LEN = 120;

const typeColor = {
  leo: Color.fromCssColorString('#ffb703'),
  aircraft: Color.fromCssColorString('#2a9d8f'),
  ship: Color.fromCssColorString('#00b4d8')
};

function toCartesian(node) {
  return Cartesian3.fromDegrees(node.lon, node.lat, node.alt_m);
}

export function App() {
  const [frame, setFrame] = useState(null);
  const [connected, setConnected] = useState(false);
  const trailsRef = useRef(new Map());

  useEffect(() => {
    const ws = new WebSocket(WS_URL);
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onerror = () => setConnected(false);
    ws.onmessage = (evt) => {
      const payload = JSON.parse(evt.data);
      setFrame(payload);
      const map = trailsRef.current;
      for (const node of payload.nodes) {
        const list = map.get(node.id) || [];
        list.push(toCartesian(node));
        if (list.length > TRAIL_LEN) {
          list.shift();
        }
        map.set(node.id, list);
      }
    };
    return () => ws.close();
  }, []);

  const nodeMap = useMemo(() => {
    if (!frame) {
      return new Map();
    }
    return new Map(frame.nodes.map((n) => [n.id, n]));
  }, [frame]);

  const linkLines = useMemo(() => {
    if (!frame) {
      return [];
    }
    const out = [];
    for (const edge of frame.links) {
      const a = nodeMap.get(edge.a);
      const b = nodeMap.get(edge.b);
      if (!a || !b) {
        continue;
      }
      out.push({
        id: `${edge.a}-${edge.b}`,
        positions: [toCartesian(a), toCartesian(b)]
      });
    }
    return out;
  }, [frame, nodeMap]);

  return (
    <div className="app-shell">
      <div className="hud">
        <h1>Dynamic Topology</h1>
        <p>Status: {connected ? 'connected' : 'disconnected'}</p>
        <p>WS: {WS_URL}</p>
        <p>t: {frame ? frame.sim_time_s.toFixed(1) : '-'} s</p>
        <p>nodes: {frame ? frame.nodes.length : 0}</p>
        <p>links: {frame ? frame.metrics.edge_count : 0}</p>
        <p>avg degree: {frame ? frame.metrics.avg_degree.toFixed(2) : '-'}</p>
        <p>tick: {frame ? frame.elapsed_ms.toFixed(2) : '-'} ms</p>
      </div>

      <Viewer
        full
        animation={false}
        timeline={false}
        shouldAnimate
        selectionIndicator={false}
        baseLayerPicker={false}
        geocoder={false}
        sceneModePicker={false}
        homeButton={false}
        navigationHelpButton={false}
      >
        <CameraFlyTo
          duration={0}
          destination={Cartesian3.fromDegrees(110, 25, 22_000_000)}
          orientation={{
            heading: 0,
            pitch: CesiumMath.toRadians(-65),
            roll: 0
          }}
        />

        {frame &&
          frame.nodes.map((node) => {
            const trail = trailsRef.current.get(node.id) || [];
            if (trail.length < 2) {
              return null;
            }
            return (
              <Entity key={node.id} name={node.id} position={toCartesian(node)}>
                <PolylineGraphics
                  positions={trail}
                  width={1.5}
                  material={(typeColor[node.type] || Color.WHITE).withAlpha(0.45)}
                />
              </Entity>
            );
          })}

        {frame &&
          frame.nodes.map((node) => (
            <Entity
              key={`${node.id}-point`}
              name={node.id}
              position={toCartesian(node)}
              point={{
                pixelSize: node.type === 'leo' ? 7 : 5,
                color: typeColor[node.type] || Color.WHITE,
                outlineColor: Color.BLACK,
                outlineWidth: 1
              }}
              label={{
                text: node.id,
                show: node.type === 'leo',
                scale: 0.45,
                fillColor: Color.WHITE,
                showBackground: true,
                backgroundColor: Color.BLACK.withAlpha(0.55),
                verticalOrigin: VerticalOrigin.BOTTOM,
                pixelOffset: new Cartesian2(0, -12)
              }}
            />
          ))}

        {linkLines.map((line) => (
          <Entity
            key={line.id}
            polyline={{
              positions: line.positions,
              width: 1,
              material: Color.CYAN.withAlpha(0.24)
            }}
          />
        ))}
      </Viewer>
    </div>
  );
}
