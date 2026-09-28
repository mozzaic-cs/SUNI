/* archgraph.js — the architecture as panels you can walk around.

   The flat page put eighty-one boxes and four thousand words at one depth and
   let the reader sort it out. This is the same content with a camera in front
   of it: subsystems first, their parts when you open one, the full prose when
   you open a part.

   THE TEXT IS HTML, NOT TEXTURES. That is the whole reason this is built the
   way it is. Panels are positioned by projecting a 3D point to screen and
   placing an absolutely-positioned div there, scaled by distance — so the type
   stays crisp at any zoom, Ctrl+F still finds it, it can be selected and read
   by a screen reader, and four thousand words cost no texture memory at all.
   The same projection already places the name labels over the network field on
   the Face, so this is a proven arrangement in this codebase rather than a
   hopeful one.

   Edges are drawn on an ordinary 2D canvas from the same projected endpoints.
   They connect points that live in 3D and they move with the camera, which is
   all the depth they need; a second WebGL context to draw thirty lines would
   be cost without a reason.
*/
(function (global) {
  'use strict';

  /* ── small maths ──────────────────────────────────────────────────────── */
  function mul(a, b) {
    const o = new Float32Array(16);
    for (let i = 0; i < 4; i++) {
      for (let j = 0; j < 4; j++) {
        let s = 0;
        for (let k = 0; k < 4; k++) s += a[k * 4 + j] * b[i * 4 + k];
        o[i * 4 + j] = s;
      }
    }
    return o;
  }
  function persp(fov, asp, n, f) {
    const t = 1 / Math.tan(fov / 2), o = new Float32Array(16);
    o[0] = t / asp; o[5] = t; o[10] = (f + n) / (n - f); o[11] = -1;
    o[14] = 2 * f * n / (n - f);
    return o;
  }
  function trans(x, y, z) {
    const o = new Float32Array(16);
    o[0] = o[5] = o[10] = o[15] = 1; o[12] = x; o[13] = y; o[14] = z;
    return o;
  }
  function rotX(a) {
    const c = Math.cos(a), s = Math.sin(a), o = new Float32Array(16);
    o[0] = 1; o[5] = c; o[6] = s; o[9] = -s; o[10] = c; o[15] = 1;
    return o;
  }
  function rotY(a) {
    const c = Math.cos(a), s = Math.sin(a), o = new Float32Array(16);
    o[0] = c; o[2] = -s; o[5] = 1; o[8] = s; o[10] = c; o[15] = 1;
    return o;
  }

  const TONE = {
    gold:   '201,162,39',
    purple: '155,89,182',
    teal:   '20,217,140',
    cyan:   '0,212,255',
    red:    '255,69,96',
  };

  class ArchGraph {
    constructor(doc, opts) {
      this.doc = doc || { groups: [], edges: [] };
      this.o = opts || {};
      this.yaw = 0.35; this.pitch = 0.22;
      this.dist = 19; this.distWant = 19;
      this.look = [0, 0, 0]; this.lookWant = [0, 0, 0];
      this.spin = 0.035;
      this.open = null;          // the group being looked inside, or null
      this.hover = null;
      this._t = 0;
      this.layout();
    }

    /* ── where everything sits ────────────────────────────────────────────
       Groups on a disc facing the viewer, spread by golden angle. Opening one
       moves it to the middle and fans its parts around it — a set in a ring, a
       sequence along an arc, because an ordered pipeline that reads as a cloud
       has lost the only thing it was saying. */
    layout() {
      const gs = this.doc.groups || [];
      const R = gs.length > 1 ? 3.6 + gs.length * 1.15 : 0;
      this.nodes = [];
      gs.forEach((g, i) => {
        const t = gs.length > 1 ? Math.sqrt((i + 0.6) / gs.length) : 0;
        const a = i * 2.399963;
        g._home = [Math.cos(a) * R * t, Math.sin(a) * R * t * 0.72, 0];
        g._pos = g._home.slice();
        this.nodes.push({ kind: 'group', ref: g, pos: g._pos });
      });
      this._relayout();
    }

    _relayout() {
      const gs = this.doc.groups || [];
      this.nodes = [];
      for (const g of gs) {
        const opened = this.open === g.id;
        const target = opened ? [0, 4.6, 0] : g._home;
        g._pos = g._pos || target.slice();
        g._target = this.open && !opened
          // The others push outward and back, so they are still there to see
          // and no longer in the way of what was opened.
          ? [g._home[0] * 1.55, g._home[1] * 1.55, -7]
          : target;
        this.nodes.push({ kind: 'group', ref: g, pos: g._pos, dim: !!(this.open && !opened) });
        if (!opened) continue;
        const items = g.items || [];
        const seq = g.kind === 'sequence';
        items.forEach((it, i) => {
          if (!it._pos) it._pos = [0, 0, 0];
          if (seq) {
            /* A shallow arc, left to right: the order is the point. */
            /* Spacing has to exceed a panel's own width in world units or
               the row is a stack of overlapping cards with the order hidden
               inside it — the one thing a sequence exists to show. */
            const u = items.length > 1 ? i / (items.length - 1) : 0.5;
            it._target = [(u - 0.5) * (items.length * 3.9),
                          Math.sin(u * Math.PI) * 1.1 - 2.2,
                          /* A shallow bow only. Curving the row toward the
                             camera makes the middle steps nearer, therefore
                             bigger, therefore overlapping the ones beside
                             them — depth bought at the cost of the order. */
                          Math.sin(u * Math.PI) * 1.2];
          } else {
            const ring = Math.floor(i / 6), k = i % 6;
            const a = k * (Math.PI * 2 / 6) + ring * 0.52;
            const rad = 7.6 + ring * 5.2;
            it._target = [Math.cos(a) * rad, Math.sin(a) * rad * 0.62, -ring * 1.8];
          }
          this.nodes.push({ kind: 'item', ref: it, group: g, pos: it._pos });
        });
      }
    }

    openGroup(id) {
      this.open = id || null;
      this._relayout();
      const g = (this.doc.groups || []).find(x => x.id === id);
      this.lookWant = g ? [0, 1.2, 0] : [0, 0, 0];
      this.distWant = !g ? 19
        : (g.kind === 'sequence' ? 11 + (g.items || []).length * 2.15
                                 : 16 + Math.sqrt((g.items || []).length) * 3.6);
      this.yawWant = 0;
    }

    orbit(dx, dy) {
      this.yaw += dx * 0.005;
      this.pitch = Math.max(-1.1, Math.min(1.1, this.pitch + dy * 0.005));
      this.yawWant = undefined;
    }
    zoom(d) { this.distWant = Math.max(5, Math.min(70, this.distWant * (1 + d * 0.0012))); }

    update(dt) {
      this._t += dt;
      const k = Math.min(1, 3.4 * dt);
      this.dist += (this.distWant - this.dist) * k;
      for (let i = 0; i < 3; i++) this.look[i] += (this.lookWant[i] - this.look[i]) * k;
      if (this.yawWant !== undefined) {
        this.yaw += (this.yawWant - this.yaw) * k;
        if (Math.abs(this.yawWant - this.yaw) < 0.002) this.yawWant = undefined;
      } else if (!this.open && !this.hover) {
        this.yaw += this.spin * dt;      // a slow drift while nobody is holding it
      }
      for (const nd of this.nodes) {
        const tgt = nd.ref._target;
        if (!tgt) continue;
        for (let i = 0; i < 3; i++) nd.pos[i] += (tgt[i] - nd.pos[i]) * Math.min(1, 2.8 * dt);
      }
    }

    view() {
      return mul(trans(0, 0, -this.dist),
                 mul(rotX(this.pitch), mul(rotY(this.yaw),
                     trans(-this.look[0], -this.look[1], -this.look[2]))));
    }

    /* Screen position, in CSS pixels, plus the camera distance — which is what
       scales a panel and decides what covers what. */
    project(pos, w, h) {
      const m = mul(persp(0.9, w / h, 0.1, 200), this.view());
      const x = m[0] * pos[0] + m[4] * pos[1] + m[8] * pos[2] + m[12];
      const y = m[1] * pos[0] + m[5] * pos[1] + m[9] * pos[2] + m[13];
      const wc = m[3] * pos[0] + m[7] * pos[1] + m[11] * pos[2] + m[15];
      if (wc <= 0.05) return null;
      return { x: (x / wc * 0.5 + 0.5) * w, y: (1 - (y / wc * 0.5 + 0.5)) * h, w: wc };
    }

    /* Everything on screen this frame, furthest first so the near ones are
       painted over the far ones. */
    placed(w, h) {
      const out = [];
      for (const nd of this.nodes) {
        const p = this.project(nd.pos, w, h);
        if (!p) continue;
        out.push({ nd, x: p.x, y: p.y, d: p.w, scale: Math.max(0.52, Math.min(1.25, 18 / p.w)) });
      }
      out.sort((a, b) => b.d - a.d);
      return out;
    }

    /* Edges, from the same projected points. Only between groups: an edge from
       a subsystem to one of its own parts says nothing that containment has not
       already said. */
    drawEdges(ctx, w, h, placedList) {
      const at = new Map();
      for (const p of placedList) if (p.nd.kind === 'group') at.set(p.nd.ref.id, p);
      ctx.clearRect(0, 0, w, h);
      ctx.lineWidth = 1;
      for (const e of (this.doc.edges || [])) {
        const a = at.get(e.from), b = at.get(e.to);
        if (!a || !b) continue;
        const tone = TONE[(a.nd.ref.tone)] || TONE.cyan;
        const near = Math.min(a.d, b.d);
        const alpha = Math.max(0.05, Math.min(0.5, 14 / near)) * (this.open ? 0.35 : 1);
        ctx.strokeStyle = `rgba(${tone},${alpha.toFixed(3)})`;
        ctx.setLineDash([5, 9]);
        ctx.lineDashOffset = -(this._t * 26) % 14;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        /* Bowed, so two edges between the same pair are distinguishable and a
           straight run of them does not read as one line. */
        const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        const nx = -(b.y - a.y), ny = (b.x - a.x);
        const len = Math.hypot(nx, ny) || 1;
        ctx.quadraticCurveTo(mx + nx / len * 26, my + ny / len * 26, b.x, b.y);
        ctx.stroke();
      }
      ctx.setLineDash([]);
    }

    static tone(name) { return TONE[name] || TONE.cyan; }
  }

  global.ArchGraph = ArchGraph;
})(window);
