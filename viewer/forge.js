// The forging cinematic: the ring arrives, fires a beam, energy particles stream out and the
// construct assembles group by group (build.order), a pulse runs through it, then it idles.
import * as THREE from 'three';

// ---------------------------------------------------------------- beam
const beamVertex = /* glsl */ `
  varying vec2 vUv;
  varying vec3 vNormal;
  varying vec3 vViewDir;
  void main() {
    vUv = uv;
    vec4 world = modelMatrix * vec4(position, 1.0);
    vNormal = normalize(mat3(modelMatrix) * normal);
    vViewDir = cameraPosition - world.xyz;
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;
const beamFragment = /* glsl */ `
  uniform vec3 uColor;
  uniform float uTime;
  uniform float uIntensity;
  varying vec2 vUv;
  varying vec3 vNormal;
  varying vec3 vViewDir;
  void main() {
    float facing = abs(dot(normalize(vNormal), normalize(vViewDir)));
    float core = pow(facing, 3.0);
    float flow = 0.65 + 0.35 * sin(vUv.y * 60.0 - uTime * 40.0);
    vec3 col = mix(uColor, vec3(1.0), core * 0.6) * (0.4 + core * 1.8) * flow;
    gl_FragColor = vec4(col * uIntensity, facing * uIntensity);
  }
`;

class Beam {
  constructor(color) {
    this.material = new THREE.ShaderMaterial({
      vertexShader: beamVertex,
      fragmentShader: beamFragment,
      uniforms: { uColor: { value: new THREE.Color(color) }, uTime: { value: 0 }, uIntensity: { value: 0 } },
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const geo = new THREE.CylinderGeometry(1, 1, 1, 12, 1, true);
    geo.translate(0, 0.5, 0); // base at the origin, grows along +Y
    this.mesh = new THREE.Mesh(geo, this.material);
    this.mesh.visible = false;
    this.width = 0.05;
  }
  set(from, to, intensity, time) {
    const dir = new THREE.Vector3().subVectors(to, from);
    const len = dir.length();
    this.mesh.visible = intensity > 0.01 && len > 1e-4;
    if (!this.mesh.visible) return;
    this.mesh.position.copy(from);
    this.mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir.normalize());
    const w = this.width * (0.85 + 0.15 * Math.sin(time * 37));
    this.mesh.scale.set(w, len, w);
    this.material.uniforms.uIntensity.value = intensity;
    this.material.uniforms.uTime.value = time;
  }
}

// ---------------------------------------------------------------- particles
const particleVertex = /* glsl */ `
  attribute float aAlpha;
  uniform float uSize;
  uniform float uScale;
  varying float vAlpha;
  void main() {
    vAlpha = aAlpha;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = uSize * uScale / -mv.z;
    gl_Position = projectionMatrix * mv;
  }
`;
const particleFragment = /* glsl */ `
  uniform vec3 uColor;
  varying float vAlpha;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float a = smoothstep(0.5, 0.0, d);
    vec3 col = mix(uColor, vec3(1.0), a * a * 0.7) * 2.2;
    gl_FragColor = vec4(col * a * vAlpha, a * vAlpha);
  }
`;

class Particles {
  constructor(color, count = 3000) {
    this.count = count;
    this.next = 0;
    this.pos = new Float32Array(count * 3);
    this.alpha = new Float32Array(count);
    this.state = Array.from({ length: count }, () => ({
      t: 1, speed: 1, p0: new THREE.Vector3(), p1: new THREE.Vector3(), p2: new THREE.Vector3(),
    }));
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(this.pos, 3));
    geo.setAttribute('aAlpha', new THREE.BufferAttribute(this.alpha, 1));
    this.material = new THREE.ShaderMaterial({
      vertexShader: particleVertex,
      fragmentShader: particleFragment,
      uniforms: { uColor: { value: new THREE.Color(color) }, uSize: { value: 0.05 }, uScale: { value: 500 } },
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    this.points = new THREE.Points(geo, this.material);
    this.points.frustumCulled = false;
  }
  /** Launch one particle from `from` to `to` along a curved path. */
  emit(from, to, travel) {
    const s = this.state[this.next];
    this.next = (this.next + 1) % this.count;
    s.t = 0;
    s.speed = 1 / travel;
    s.p0.copy(from);
    s.p2.copy(to);
    const span = from.distanceTo(to);
    s.p1.lerpVectors(from, to, 0.5).add(new THREE.Vector3().randomDirection().multiplyScalar(span * 0.3));
  }
  update(dt) {
    const a = new THREE.Vector3();
    const b = new THREE.Vector3();
    for (let i = 0; i < this.count; i++) {
      const s = this.state[i];
      if (s.t >= 1) {
        this.alpha[i] = 0;
        continue;
      }
      s.t = Math.min(1, s.t + dt * s.speed);
      const t = s.t * s.t * (3 - 2 * s.t); // ease in-out along the curve
      a.lerpVectors(s.p0, s.p1, t);
      b.lerpVectors(s.p1, s.p2, t);
      a.lerp(b, t);
      this.pos[i * 3] = a.x;
      this.pos[i * 3 + 1] = a.y;
      this.pos[i * 3 + 2] = a.z;
      this.alpha[i] = Math.min(1, s.t * 6) * (s.t > 0.9 ? (1 - s.t) * 10 : 1);
    }
    this.points.geometry.attributes.position.needsUpdate = true;
    this.points.geometry.attributes.aAlpha.needsUpdate = true;
  }
  clear() {
    for (const s of this.state) s.t = 1;
    this.alpha.fill(0);
  }
}

// ---------------------------------------------------------------- sequence
const INTRO = 1.6; // ring arrives and the sigil ignites
const PULSE = 1.6; // completion pulse
const smooth = (x) => { x = Math.min(1, Math.max(0, x)); return x * x * (3 - 2 * x); };

export function createForge({ scene, camera, construct, ring, animator, ringPosition, size, duration }) {
  const { parts, shared, style } = construct;
  const beam = new Beam(style.color);
  beam.width = size * 0.012;
  const particles = new Particles(style.color);
  particles.material.uniforms.uSize.value = size * 0.02;
  scene.add(beam.mesh, particles.points);

  // Build groups in build.order; each remembers how to sample points on its surfaces.
  construct.root.updateMatrixWorld(true);
  const byOrder = new Map();
  for (const p of parts.values()) {
    if (!p.mesh) continue;
    const order = p.spec.build?.order ?? 0;
    if (!byOrder.has(order)) byOrder.set(order, []);
    byOrder.get(order).push(p);
  }
  const groups = [...byOrder.keys()].sort((a, b) => a - b).map((order) => {
    const members = byOrder.get(order);
    const box = new THREE.Box3();
    for (const p of members) box.expandByObject(p.mesh);
    return { members, center: box.getCenter(new THREE.Vector3()) };
  });
  const slot = Math.max(0.4, duration / groups.length);
  const buildTime = slot * groups.length;

  const tmp = new THREE.Vector3();
  function randomSurfacePoint(group) {
    const p = group.members[Math.floor(Math.random() * group.members.length)];
    const pos = p.mesh.geometry.attributes.position;
    return tmp.fromBufferAttribute(pos, Math.floor(Math.random() * pos.count)).applyMatrix4(p.mesh.matrixWorld).clone();
  }

  const ringHome = ringPosition.clone();
  const aim = new THREE.Vector3().copy(groups[0]?.center ?? ringHome);
  const muzzle = new THREE.Vector3();
  let start = 0;
  let skipped = false;
  let phase = 'intro';
  let emitDebt = 0;

  function setBuild(p, b) {
    p.material.uniforms.uBuild.value = b;
    p.edges.material.opacity = 0.8 * smooth(b * 3); // outlines appear first, then the solid fills in
  }

  function reset(now) {
    start = now;
    skipped = false;
    phase = 'intro';
    particles.clear();
    animator.reset();
    for (const g of groups) for (const p of g.members) setBuild(p, 0);
    shared.uPulse.value = -1;
    shared.uIdle.value = 0;
    aim.copy(groups[0]?.center ?? ringHome);
  }

  function skip(now) {
    reset(now);
    start = now - (INTRO + buildTime + PULSE);
    skipped = true;
  }

  function update(now, dt) {
    const t = now - start;
    const tBuild = t - INTRO;
    const tPulse = tBuild - buildTime;
    const tIdle = tPulse - PULSE;
    phase = t < INTRO ? 'intro' : tBuild < buildTime ? 'forging' : tPulse < PULSE ? 'pulse' : 'idle';

    // Ring: swoop in from above with a spin, then hover.
    const arrive = smooth(t / INTRO);
    ring.group.position.copy(ringHome).add(new THREE.Vector3(0, (1 - arrive) * size * 0.6, 0));
    ring.group.position.y += Math.sin(now * 1.3) * size * 0.01;
    ring.group.scale.setScalar(size * 0.12 * (0.3 + 0.7 * arrive));
    let glow = 0.2 + arrive * 0.8 + Math.max(0, 1 - Math.abs(t - INTRO + 0.15) * 5) * 1.5; // ignition flash

    let beamIntensity = 0;
    if (phase === 'forging') {
      const gi = Math.min(groups.length - 1, Math.floor(tBuild / slot));
      const s = (tBuild - gi * slot) / slot;
      for (let i = 0; i < groups.length; i++) {
        const b = i < gi ? 1 : i === gi ? smooth((s - 0.1) / 0.85) : 0;
        for (const p of groups[i].members) setBuild(p, b);
      }
      aim.lerp(groups[gi].center, Math.min(1, dt * 6));
      beamIntensity = 0.8 + 0.2 * Math.sin(now * 23);
      glow = 1.3 + 0.3 * Math.sin(now * 17);

      // Stream particles from the ring onto the surfaces being built.
      ring.muzzle.getWorldPosition(muzzle);
      emitDebt += dt * 700;
      while (emitDebt >= 1) {
        emitDebt -= 1;
        particles.emit(muzzle, randomSurfacePoint(groups[gi]), slot * (0.35 + Math.random() * 0.3));
      }
    } else if (phase !== 'intro') {
      for (const g of groups) for (const p of g.members) setBuild(p, 1);
    }

    if (phase === 'pulse') {
      shared.uPulse.value = -0.15 + 1.4 * (tPulse / PULSE);
      beamIntensity = Math.max(0, 1 - tPulse * 4);
      glow = 1.2 - 0.4 * smooth(tPulse / PULSE);
    }
    if (phase === 'idle') {
      shared.uPulse.value = -1;
      shared.uIdle.value = smooth(tIdle / 1.5);
      animator.update(tIdle, smooth(tIdle / 1.2));
      glow = 0.75 + 0.1 * Math.sin(now * 2);
      aim.lerp(construct.center, Math.min(1, dt * 2));
    }

    // During the intro the sigil faces you as it ignites; then the ring turns to aim at
    // whatever it's building.
    const target = new THREE.Object3D();
    target.position.copy(ring.group.position);
    target.lookAt(phase === 'intro' ? camera.position : aim);
    ring.group.quaternion.slerp(target.quaternion, Math.min(1, dt * (phase === 'intro' ? 12 : 5)));
    ring.group.updateMatrixWorld(true);
    ring.setGlow(glow);
    ring.setTime(now);

    ring.muzzle.getWorldPosition(muzzle);
    beam.set(muzzle, aim, skipped ? 0 : beamIntensity, now);
    particles.update(dt);
    shared.uTime.value = now;
    return { phase, progress: phase === 'forging' ? tBuild / buildTime : null };
  }

  return { reset, skip, update, setPixelScale: (s) => (particles.material.uniforms.uScale.value = s) };
}
