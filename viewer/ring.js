// The power ring: a faceted band with a hexagonal bezel carrying the Sprout Gate sigil.
// Local frame: the bezel faces +Z (so ring.lookAt(target) aims it), the finger hole runs along X.
// Built at unit size; scale the returned group to fit the construct.
import * as THREE from 'three';
import { createHardLight, createGlow } from './hardlight.js';

const v3 = (x, y, z = 0) => new THREE.Vector3(x, y, z);

function polyline(points) {
  const path = new THREE.CurvePath();
  for (let i = 0; i < points.length - 1; i++) path.add(new THREE.LineCurve3(points[i], points[i + 1]));
  return path;
}

function stroke(curve, material, segments = 48, radius = 0.035) {
  return new THREE.Mesh(new THREE.TubeGeometry(curve, segments, radius, 6, false), material);
}

// The Sprout Gate: an open hexagon (gap at the top), a seed-shaped vesica,
// and a stem that pierces the seed and splits into three prongs.
function createSigil(material) {
  const sigil = new THREE.Group();
  const P = (deg, r = 0.85) => v3(Math.cos((deg * Math.PI) / 180) * r, Math.sin((deg * Math.PI) / 180) * r);
  const top = P(90);
  sigil.add(stroke(polyline([
    P(30).lerp(top, 0.5), P(30), P(-30), P(-90), P(-150), P(150), P(150).lerp(top, 0.5),
  ]), material, 96));

  for (const side of [-1, 1]) {
    const pts = [];
    for (let i = 0; i <= 16; i++) {
      const a = -Math.PI / 2 + (Math.PI * i) / 16;
      pts.push(v3(side * 0.28 * Math.cos(a), 0.45 * Math.sin(a)));
    }
    sigil.add(stroke(new THREE.CatmullRomCurve3(pts), material));
  }

  sigil.add(stroke(polyline([v3(0, -0.62), v3(0, 0.72)]), material, 8));
  for (const side of [-1, 1]) {
    sigil.add(stroke(new THREE.CatmullRomCurve3([v3(0, 0.3), v3(side * 0.12, 0.46), v3(side * 0.27, 0.62)]), material, 16));
  }
  return sigil;
}

export function createRing(style) {
  const group = new THREE.Group();

  const band = new THREE.Mesh(
    new THREE.TorusGeometry(1, 0.16, 6, 36),
    createHardLight({ ...style, opacity: 0.55, rim: 1.4, intensity: 1.3 }),
  );
  band.rotation.y = Math.PI / 2; // finger hole along X
  group.add(band);

  const bezel = new THREE.Mesh(
    new THREE.CylinderGeometry(0.62, 0.68, 0.24, 6),
    createHardLight({ ...style, opacity: 0.5, rim: 0.8, intensity: 1.2 }),
  );
  bezel.rotation.x = Math.PI / 2; // face along +Z
  bezel.position.z = 1.12;
  group.add(bezel);

  const sigilMaterial = createGlow(style.color, 2.5);
  const sigil = createSigil(sigilMaterial);
  sigil.position.z = 1.25;
  sigil.scale.setScalar(0.55);
  group.add(sigil);

  // Where the beam and particles leave the ring.
  const muzzle = new THREE.Object3D();
  muzzle.position.z = 1.35;
  group.add(muzzle);

  const baseSigilColor = sigilMaterial.color.clone();
  return {
    group,
    muzzle,
    /** 0 = dark, 1 = normal, >1 = flaring */
    setGlow(g) {
      sigilMaterial.color.copy(baseSigilColor).multiplyScalar(g);
      sigilMaterial.opacity = Math.min(1, g);
      for (const m of [band.material, bezel.material]) m.uniforms.uIntensity.value = 0.5 + 0.8 * Math.min(g, 1.5);
    },
    setTime(t) {
      band.material.uniforms.uTime.value = t;
      bezel.material.uniforms.uTime.value = t;
    },
  };
}
