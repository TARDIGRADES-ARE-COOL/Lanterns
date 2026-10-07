// Turns a scene spec into a Three.js hierarchy.
// Each part becomes a "joint" (position/rotation, carries children) plus an optional
// mesh child (offset + scale), so scale stretches only that part, not its children.
import * as THREE from 'three';
import { createHardLight, createEdges, createSharedUniforms } from './hardlight.js';

const DEG = Math.PI / 180;
// Flat faces hit grazing angles across their whole surface, so they get a weaker rim
// than curved shapes (whose rim only lights the silhouette).
const RIM_BY_SHAPE = { box: 0.35, extrude: 0.35, tube: 0.6 };
export const DEFAULT_STYLE = { color: '#19ff4a', opacity: 0.32, rim: 1.2, edgeGlow: 1.0 };

function geometryFor(shape) {
  const seg = shape.segments;
  switch (shape.type) {
    case 'box':
      return new THREE.BoxGeometry(...shape.size);
    case 'sphere':
      return new THREE.SphereGeometry(shape.radius, seg ?? 24, Math.max(6, Math.round((seg ?? 24) * 0.66)));
    case 'cylinder':
      return new THREE.CylinderGeometry(shape.radiusTop, shape.radiusBottom, shape.height, seg ?? 24);
    case 'cone':
      return new THREE.ConeGeometry(shape.radius, shape.height, seg ?? 24);
    case 'torus':
      return new THREE.TorusGeometry(shape.radius, shape.tube, 12, 48, (shape.arc ?? 360) * DEG);
    case 'tube': {
      const curve = new THREE.CatmullRomCurve3(shape.points.map((p) => new THREE.Vector3(...p)), !!shape.closed);
      return new THREE.TubeGeometry(curve, Math.max(16, shape.points.length * 10), shape.radius, 10, !!shape.closed);
    }
    case 'extrude': {
      const s = new THREE.Shape(shape.outline.map(([x, y]) => new THREE.Vector2(x, y)));
      const bevel = shape.bevel ?? 0;
      const geo = new THREE.ExtrudeGeometry(s, {
        depth: shape.depth,
        bevelEnabled: bevel > 0,
        bevelThickness: bevel,
        bevelSize: bevel,
        bevelSegments: 1,
      });
      geo.translate(0, 0, -shape.depth / 2); // centre the slab on its joint
      return geo;
    }
    case 'group':
      return null;
    default:
      throw new Error(`Unknown shape type: ${shape.type}`);
  }
}

export function buildConstruct(spec, shared = createSharedUniforms()) {
  const style = { ...DEFAULT_STYLE, ...(spec.style ?? {}) };
  const root = new THREE.Group();
  const parts = new Map(); // id -> { spec, joint, mesh, material, edges }

  for (const p of spec.parts) {
    const joint = new THREE.Group();
    joint.name = p.id;
    joint.position.set(...p.position);
    if (p.rotation) joint.rotation.set(p.rotation[0] * DEG, p.rotation[1] * DEG, p.rotation[2] * DEG);

    let mesh = null;
    let material = null;
    let edges = null;
    const geometry = geometryFor(p.shape);
    if (geometry) {
      const intensity = p.intensity ?? 1;
      material = createHardLight({ ...style, rim: style.rim * (RIM_BY_SHAPE[p.shape.type] ?? 1), intensity }, shared);
      mesh = new THREE.Mesh(geometry, material);
      edges = createEdges(geometry, { ...style, intensity });
      mesh.add(edges);
      if (p.offset) mesh.position.set(...p.offset);
      if (p.scale) mesh.scale.set(...p.scale);
      joint.add(mesh);
    }
    parts.set(p.id, { spec: p, joint, mesh, material, edges });
  }

  for (const { spec: p, joint } of parts.values()) {
    const parent = p.parent ? parts.get(p.parent)?.joint : root;
    (parent ?? root).add(joint);
  }

  return { root, parts, style, shared };
}
