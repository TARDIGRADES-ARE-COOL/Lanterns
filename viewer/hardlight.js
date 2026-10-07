// Hard-light material: cel-shaded bands + stepped Fresnel rim, semi-transparent.
// Bright edge lines are separate LineSegments so bloom picks them up as glowing outlines.
//
// Forging effects live in the same shader:
//   uBuild  0..1  blocky dissolve-in (world-space cells), with a white-hot assembly front
//   uPulse        height (0..1) of the completion pulse travelling up the construct; <0 = off
//   uIdle   0..1  idle shimmer: fine scanlines plus a slow sweeping glow band
import * as THREE from 'three';

const vertexShader = /* glsl */ `
  varying vec3 vNormal;
  varying vec3 vViewDir;
  varying vec3 vWorldPos;
  void main() {
    vec4 world = modelMatrix * vec4(position, 1.0);
    vWorldPos = world.xyz;
    vNormal = normalize(mat3(modelMatrix) * normal);
    vViewDir = cameraPosition - world.xyz;
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;

const fragmentShader = /* glsl */ `
  uniform vec3 uColor;
  uniform float uOpacity;
  uniform float uRim;
  uniform float uIntensity;
  uniform float uTime;
  uniform float uBuild;
  uniform float uPulse;
  uniform float uIdle;
  uniform float uCell;
  uniform vec2 uBounds; // (minY, height) of the whole construct
  varying vec3 vNormal;
  varying vec3 vViewDir;
  varying vec3 vWorldPos;

  float hash(vec3 p) { return fract(sin(dot(p, vec3(12.9898, 78.233, 37.719))) * 43758.5453); }

  void main() {
    // Assemble out of hard-light "blocks": each world-space cell switches on at its own moment.
    float n = hash(floor(vWorldPos * uCell) + 0.37);
    if (n >= uBuild) discard;
    float front = uBuild < 1.0 ? smoothstep(uBuild - 0.25, uBuild, n) : 0.0;

    vec3 nrm = normalize(vNormal);
    if (!gl_FrontFacing) nrm = -nrm;
    vec3 v = normalize(vViewDir);

    // Cel shading: quantise a key light into three flat bands.
    float ndl = dot(nrm, normalize(vec3(0.45, 0.8, 0.4)));
    float band = ndl > 0.55 ? 1.0 : (ndl > 0.0 ? 0.62 : 0.38);

    // Fresnel rim, stepped for a toon edge rather than a soft gradient.
    float fres = pow(1.0 - abs(dot(nrm, v)), 2.0);
    float rim = smoothstep(0.4, 0.55, fres) * 0.55 + smoothstep(0.68, 0.78, fres) * 0.6;

    float h = clamp((vWorldPos.y - uBounds.x) / uBounds.y, 0.0, 1.0);
    float pulse = uPulse < -0.5 ? 0.0 : exp(-pow((h - uPulse) * 7.0, 2.0));
    float sweepPos = fract(uTime * 0.09) * 1.6 - 0.3;
    float sweep = uIdle * exp(-pow((h - sweepPos) * 9.0, 2.0));
    float lines = uIdle * 0.09 * sin(h * 140.0 - uTime * 4.0);

    vec3 hot = mix(uColor, vec3(1.0), 0.2);
    vec3 col = uColor * band * 0.42 + hot * rim * uRim * 0.55;
    col *= 1.0 + lines;
    col += hot * (front * 1.6 + pulse * 1.4 + sweep * 0.35);
    float alpha = clamp(uOpacity * band * 1.1 + rim * 0.45 + front * 0.5 + pulse * 0.4, 0.0, 1.0);
    gl_FragColor = vec4(col * uIntensity, alpha);
  }
`;

// Uniforms every part shares (time, pulse, idle...). Pass the same object to every material.
export function createSharedUniforms() {
  return {
    uTime: { value: 0 },
    uPulse: { value: -1 },
    uIdle: { value: 0 },
    uCell: { value: 4 },
    uBounds: { value: new THREE.Vector2(0, 1) },
  };
}

export function createHardLight({ color, opacity = 0.32, rim = 1.2, intensity = 1 }, shared = createSharedUniforms()) {
  return new THREE.ShaderMaterial({
    vertexShader,
    fragmentShader,
    uniforms: {
      ...shared, // same {value} objects, so updating shared updates every part
      uColor: { value: new THREE.Color(color) },
      uOpacity: { value: opacity },
      uRim: { value: rim },
      uIntensity: { value: intensity },
      uBuild: { value: 1 },
    },
    transparent: true,
    depthWrite: false,
    side: THREE.FrontSide,
  });
}

export function createEdges(geometry, { color, edgeGlow = 1, intensity = 1 }) {
  const edges = new THREE.EdgesGeometry(geometry, 20);
  // Push the colour above 1.0 (HDR) toward white so bloom makes the outlines glow.
  const c = new THREE.Color(color).lerp(new THREE.Color(1, 1, 1), 0.2).multiplyScalar(1.3 * edgeGlow * intensity);
  const material = new THREE.LineBasicMaterial({
    color: c,
    transparent: true,
    opacity: 0.8,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  return new THREE.LineSegments(edges, material);
}

// Glowing additive material for energy effects (sigil, sparks). HDR colour so it blooms.
export function createGlow(color, strength = 2.5) {
  return new THREE.MeshBasicMaterial({
    color: new THREE.Color(color).lerp(new THREE.Color(1, 1, 1), 0.3).multiplyScalar(strength),
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
}
