// Plays the spec's keyframe animations on part joints.
// Keyframe values are offsets from the rest pose (added for position/rotation, multiplied
// for scale). `weight` blends from rest (0) to full motion (1) so idle starts smoothly.
const DEG = Math.PI / 180;

const EASE = {
  linear: (s) => s,
  sine: (s) => 0.5 - 0.5 * Math.cos(Math.PI * s),
  quad: (s) => (s < 0.5 ? 2 * s * s : 1 - 2 * (1 - s) * (1 - s)),
  cubic: (s) => (s < 0.5 ? 4 * s * s * s : 1 - 4 * (1 - s) ** 3),
};

function sample(track, time) {
  let u = (time / track.duration + (track.phase ?? 0)) % 1;
  if (track.loop === 'pingpong') u = u < 0.5 ? u * 2 : 2 - u * 2; // there and back in one duration
  const k = track.keyframes;
  if (u <= k[0].t) return k[0].value;
  if (u >= k[k.length - 1].t) return k[k.length - 1].value;
  for (let i = 0; i < k.length - 1; i++) {
    if (u <= k[i + 1].t) {
      const s = (u - k[i].t) / (k[i + 1].t - k[i].t);
      const e = (EASE[track.easing] ?? EASE.sine)(s);
      return k[i].value.map((v, j) => v + (k[i + 1].value[j] - v) * e);
    }
  }
  return k[k.length - 1].value;
}

export function createAnimator(spec, parts) {
  const rest = new Map();
  for (const { joint } of parts.values()) {
    rest.set(joint, { p: joint.position.clone(), r: joint.rotation.clone(), s: joint.scale.clone() });
  }
  const tracks = (spec.animations ?? [])
    .map((a) => ({ ...a, joint: parts.get(a.part)?.joint }))
    .filter((t) => t.joint);

  return {
    reset() {
      for (const [j, r] of rest) {
        j.position.copy(r.p);
        j.rotation.copy(r.r);
        j.scale.copy(r.s);
      }
    },
    update(time, weight = 1) {
      for (const tr of tracks) {
        const v = sample(tr, time);
        const r = rest.get(tr.joint);
        const j = tr.joint;
        if (tr.property === 'position') {
          j.position.set(r.p.x + v[0] * weight, r.p.y + v[1] * weight, r.p.z + v[2] * weight);
        } else if (tr.property === 'rotation') {
          j.rotation.set(r.r.x + v[0] * DEG * weight, r.r.y + v[1] * DEG * weight, r.r.z + v[2] * DEG * weight);
        } else {
          j.scale.set(r.s.x * (1 + (v[0] - 1) * weight), r.s.y * (1 + (v[1] - 1) * weight), r.s.z * (1 + (v[2] - 1) * weight));
        }
      }
    },
  };
}
