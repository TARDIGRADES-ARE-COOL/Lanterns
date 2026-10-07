import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { buildConstruct } from '/viewer/scene.js';
import { createAnimator } from '/viewer/animate.js';
import { createRing } from '/viewer/ring.js';
import { createForge } from '/viewer/forge.js';

const $ = (id) => document.getElementById(id);
const showError = (msg) => {
  $('error').textContent = msg;
  $('error').style.display = 'flex';
};

// --- renderer, scene, camera ---
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
document.body.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x020604);
const camera = new THREE.PerspectiveCamera(40, innerWidth / innerHeight, 0.05, 500);
camera.position.set(6, 4, 9);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.06;

// Faint holographic floor grid.
const grid = new THREE.PolarGridHelper(8, 16, 8, 64, 0x0d6b28, 0x06331a);
grid.material.transparent = true;
grid.material.opacity = 0.35;
grid.material.depthWrite = false;
scene.add(grid);

// --- post-processing: bloom does most of the "hard-light" glow ---
const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(scene, camera));
composer.addPass(new UnrealBloomPass(new THREE.Vector2(innerWidth, innerHeight), 0.85, 0.35, 0.55));
composer.addPass(new OutputPass());

let forge = null;
const pixelScale = () => (renderer.domElement.height / 2) / Math.tan((camera.fov * Math.PI) / 360);

addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
  composer.setSize(innerWidth, innerHeight);
  forge?.setPixelScale(pixelScale());
});

function frame(box) {
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const dist = (sphere.radius / Math.sin((camera.fov * Math.PI) / 360)) * 1.05;
  const dir = new THREE.Vector3(0.55, 0.35, 0.85).normalize();
  controls.target.copy(sphere.center);
  camera.position.copy(sphere.center).addScaledVector(dir, dist);
  camera.near = dist / 100;
  camera.far = dist * 20;
  camera.updateProjectionMatrix();
}

// --- load the spec: ?spec=/samples/x.json, otherwise the latest construct ---
async function load() {
  const specUrl = new URLSearchParams(location.search).get('spec') ?? '/api/latest';
  const res = await fetch(specUrl);
  if (!res.ok) throw new Error(`Could not load ${specUrl} (${res.status})`);
  const spec = await res.json();

  const construct = buildConstruct(spec);
  scene.add(construct.root);
  construct.root.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(construct.root);
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const size = sphere.radius;
  construct.center = sphere.center.clone();
  construct.shared.uBounds.value.set(box.min.y, Math.max(0.01, box.max.y - box.min.y));
  construct.shared.uCell.value = 10 / size; // ~10 assembly blocks across the construct's radius
  grid.scale.setScalar(Math.max(1, size / 6));

  // The ring hovers off to the front-left of the construct, facing it.
  const ringPosition = spec.forge?.ringPosition
    ? new THREE.Vector3(...spec.forge.ringPosition)
    : sphere.center.clone().add(new THREE.Vector3(-0.95 * size, 0.2 * size, 0.85 * size));
  const ring = createRing(construct.style);
  scene.add(ring.group);

  frame(box.clone().expandByPoint(ringPosition));

  forge = createForge({
    scene,
    camera,
    construct,
    ring,
    animator: createAnimator(spec, construct.parts),
    ringPosition,
    size,
    duration: spec.forge?.duration ?? 6,
  });
  forge.setPixelScale(pixelScale());
  forge.reset(clock.getElapsedTime());
  if (params.has('instant')) forge.skip(clock.getElapsedTime());

  document.title = `${spec.title} · Green Lantern`;
  $('title').textContent = spec.title;
  $('meta').textContent = `${spec.parts.length} parts · ${(spec.animations ?? []).length} animations`;
}

const clock = new THREE.Clock();
const params = new URLSearchParams(location.search);
// Debug: ?at=4.5 simulates the cinematic up to 4.5 s and freezes there (for screenshots).
const frozenAt = params.has('at') ? parseFloat(params.get('at')) : null;
addEventListener('error', (e) => showError(e.message));

load()
  .then(() => {
    if (frozenAt == null) return;
    forge.reset(0);
    for (let t = 0; t < frozenAt; t += 1 / 60) forge.update(t, 1 / 60);
  })
  .catch((e) => showError(e.message));

const replay = () => forge?.reset(clock.getElapsedTime());
const skip = () => forge?.skip(clock.getElapsedTime());
$('replay').addEventListener('click', replay);
addEventListener('keydown', (e) => {
  if (e.key === 'r' || e.key === 'R') replay();
  if (e.key === ' ') skip();
});

const STATUS = { intro: 'RING ONLINE', forging: 'FORGING', pulse: 'CONSTRUCT COMPLETE', idle: 'HOLDING' };
let last = 0;
renderer.setAnimationLoop(() => {
  const now = clock.getElapsedTime();
  const dt = Math.min(0.05, now - last);
  last = now;
  if (forge && frozenAt == null) {
    const { phase, progress } = forge.update(now, dt);
    $('status').textContent = STATUS[phase] + (progress != null ? ` · ${Math.round(progress * 100)}%` : '');
  }
  controls.update();
  composer.render();
});
