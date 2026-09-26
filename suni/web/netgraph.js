/* netgraph.js — the network SUNI sits in front of.
 *
 * Nodes are what she can reach: the machine, the models and services, the
 * skills, and every folder and file she has indexed. One level is on screen at
 * a time; the server decides what that level contains (see suni/graph.py).
 *
 * Two moods, because this thing is on screen for hours:
 *
 *   AMBIENT — grey, dim, slow. It is behind her head and it is not asking for
 *             attention. Readable as depth and activity, not as information.
 *   FOCUS   — the viewer took hold of it, or SUNI highlighted something. It
 *             takes the colour of her current state, brightens, labels itself,
 *             and the head steps aside to the corner.
 *
 * It owns its own camera so the viewer can orbit and zoom without disturbing
 * the head, which keeps its own. Drawn before the head with depth writes on, so
 * the head occludes what is behind it rather than being pasted over it.
 *
 * Picking is done on the CPU by projecting node positions to the screen. A
 * picking framebuffer would be more exact, but this is a field of round points
 * a few dozen strong — the nearest projected point within a finger's width is
 * the one the viewer meant, and it costs one loop instead of a second pass.
 */
(function (global) {
  'use strict';

  const VS = `
    attribute vec3 a_pos;
    attribute float a_size;
    attribute float a_shade;      /* 0 dim .. 1 bright, before mood */
    uniform mat4 u_mvp;
    uniform float u_scale;        /* pixels per unit of a_size, for this canvas */
    varying float v_shade;
    varying float v_depth;
    void main(){
      vec4 clip = u_mvp * vec4(a_pos, 1.0);
      gl_Position = clip;
      /* Nearer points are larger, as they would be. clip.w is the camera
         distance, so dividing keeps perspective honest for points too. */
      gl_PointSize = max(2.0, u_scale * a_size / max(clip.w, 0.001));
      v_shade = a_shade;
      v_depth = clamp(clip.w / 24.0, 0.0, 1.0);
    }`;

  const FS = `
    precision mediump float;
    uniform vec3  u_tint;
    uniform float u_focus;        /* 0 ambient (grey) .. 1 focus (tinted) */
    uniform float u_alpha;
    varying float v_shade;
    varying float v_depth;
    void main(){
      /* Round, soft-edged point. Square points read as pixels, not as things. */
      vec2 d = gl_PointCoord - vec2(0.5);
      float r = length(d) * 2.0;
      float a = smoothstep(1.0, 0.35, r);
      if (a <= 0.003) discard;
      float core = smoothstep(0.9, 0.0, r);
      vec3 grey = vec3(0.62, 0.66, 0.72);
      vec3 col  = mix(grey, u_tint, u_focus);
      col = mix(col * 0.55, col, v_shade);
      col += core * 0.35 * mix(vec3(1.0), u_tint, u_focus);
      /* Distance fade, so the far side of the field recedes instead of
         crowding the near side. */
      float fade = mix(1.0, 0.25, v_depth);
      gl_FragColor = vec4(col, a * u_alpha * fade);
    }`;

  const LVS = `
    attribute vec3 a_pos;
    attribute float a_shade;
    uniform mat4 u_mvp;
    varying float v_shade;
    varying float v_depth;
    void main(){
      vec4 clip = u_mvp * vec4(a_pos, 1.0);
      gl_Position = clip;
      v_shade = a_shade;
      v_depth = clamp(clip.w / 24.0, 0.0, 1.0);
    }`;

  const LFS = `
    precision mediump float;
    uniform vec3  u_tint;
    uniform float u_focus;
    uniform float u_alpha;
    varying float v_shade;
    varying float v_depth;
    void main(){
      vec3 grey = vec3(0.50, 0.54, 0.60);
      vec3 col  = mix(grey, u_tint, u_focus);
      float fade = mix(1.0, 0.18, v_depth);
      gl_FragColor = vec4(col, v_shade * u_alpha * fade);
    }`;

  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      console.warn('[NET] shader:', gl.getShaderInfoLog(s));
      return null;
    }
    return s;
  }

  function program(gl, vs, fs) {
    const v = compile(gl, gl.VERTEX_SHADER, vs), f = compile(gl, gl.FRAGMENT_SHADER, fs);
    if (!v || !f) return null;
    const p = gl.createProgram();
    gl.attachShader(p, v); gl.attachShader(p, f); gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      console.warn('[NET] link:', gl.getProgramInfoLog(p));
      return null;
    }
    return p;
  }

  /* ── maths, kept local so the module stands alone ───────────────────────── */
  function mul(a, b) {
    const o = new Float32Array(16);
    for (let i = 0; i < 4; i++) for (let j = 0; j < 4; j++) {
      let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + j] * b[i * 4 + k];
      o[i * 4 + j] = s;
    }
    return o;
  }
  function ident() {
    const o = new Float32Array(16); o[0] = o[5] = o[10] = o[15] = 1; return o;
  }
  function trans(x, y, z) { const o = ident(); o[12] = x; o[13] = y; o[14] = z; return o; }
  function rotX(a) { const c = Math.cos(a), s = Math.sin(a), o = ident(); o[5] = c; o[6] = s; o[9] = -s; o[10] = c; return o; }
  function rotY(a) { const c = Math.cos(a), s = Math.sin(a), o = ident(); o[0] = c; o[2] = -s; o[8] = s; o[10] = c; return o; }

  /* Points spread evenly over a sphere. A random scatter clumps — the eye reads
     clumping as meaning, and there is none here. */
  function fibSphere(i, n) {
    const off = 2 / n, inc = Math.PI * (3 - Math.sqrt(5));
    const y = i * off - 1 + off / 2;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const phi = i * inc;
    return [Math.cos(phi) * r, y, Math.sin(phi) * r];
  }

  const SIZE_BY_KIND = {
    root: 26, machine: 22, model: 20, tool: 13, skill: 13,
    channel: 15, folder: 16, file: 10,
  };

  class NetGraph {
    constructor(gl) {
      this.gl = gl;
      this.prog = program(gl, VS, FS);
      this.lprog = program(gl, LVS, LFS);
      this.ok = !!(this.prog && this.lprog);
      if (!this.ok) return;

      this.bPos = gl.createBuffer(); this.bSize = gl.createBuffer(); this.bShade = gl.createBuffer();
      this.bLine = gl.createBuffer(); this.bLineShade = gl.createBuffer();

      this.a = {
        pos: gl.getAttribLocation(this.prog, 'a_pos'),
        size: gl.getAttribLocation(this.prog, 'a_size'),
        shade: gl.getAttribLocation(this.prog, 'a_shade'),
      };
      this.u = {
        mvp: gl.getUniformLocation(this.prog, 'u_mvp'),
        scale: gl.getUniformLocation(this.prog, 'u_scale'),
        tint: gl.getUniformLocation(this.prog, 'u_tint'),
        focus: gl.getUniformLocation(this.prog, 'u_focus'),
        alpha: gl.getUniformLocation(this.prog, 'u_alpha'),
      };
      this.la = {
        pos: gl.getAttribLocation(this.lprog, 'a_pos'),
        shade: gl.getAttribLocation(this.lprog, 'a_shade'),
      };
      this.lu = {
        mvp: gl.getUniformLocation(this.lprog, 'u_mvp'),
        tint: gl.getUniformLocation(this.lprog, 'u_tint'),
        focus: gl.getUniformLocation(this.lprog, 'u_focus'),
        alpha: gl.getUniformLocation(this.lprog, 'u_alpha'),
      };

      this.nodes = [];        // {id,label,kind,pos:[x,y,z],home:[...],size,shade}
      this.count = 0;
      this.lineCount = 0;
      this.focus = 0;         // eased 0..1, ambient → focus
      this.wantFocus = false;
      this.highlight = null;  // node id SUNI or the pointer is pointing at
      this.yaw = 0.4; this.pitch = -0.18;
      this.dist = 15; this.distWant = 15;
      this.spin = 0.035;      // ambient drift, radians/second
      this._mvp = ident();
      this._t = 0;
    }

    /* ── data ───────────────────────────────────────────────────────────── */
    setData(graph) {
      if (!this.ok) return;
      const list = (graph && graph.nodes) || [];
      const n = Math.max(1, list.length);
      const R = 5.2 + Math.min(4.5, n * 0.035);
      this.nodes = list.map((nd, i) => {
        const p = fibSphere(i, n);
        // Depth variety: a perfect shell reads as a bubble, not a network.
        const k = R * (0.72 + 0.4 * ((i * 2654435761 % 1000) / 1000));
        const home = [p[0] * k, p[1] * k * 0.8, p[2] * k];
        return {
          id: nd.id, label: nd.label, kind: nd.kind, meta: nd,
          home, pos: home.slice(),
          size: SIZE_BY_KIND[nd.kind] || 12,
          shade: nd.kind === 'file' ? 0.55 : 0.85,
          phase: (i * 1.7) % 6.283,
        };
      });
      this.center = { id: (graph && graph.focus) || 'root', pos: [0, 0, 0] };
      // Pull back far enough that the level fits. Six nodes and a hundred and
      // twenty need very different room, and a level whose edges are off-screen
      // reads as broken rather than as big.
      this.distWant = Math.max(9, Math.min(34, R * 2.6));
      if (this.focus < 0.02) this.dist = this.distWant;   // ambient: no lurch
      this._upload();
    }

    _upload() {
      const gl = this.gl;
      const n = this.nodes.length + 1;                  // + the centre
      const pos = new Float32Array(n * 3), size = new Float32Array(n), shade = new Float32Array(n);
      pos[0] = 0; pos[1] = 0; pos[2] = 0; size[0] = SIZE_BY_KIND.root; shade[0] = 1;
      this.nodes.forEach((nd, i) => {
        pos[(i + 1) * 3] = nd.pos[0]; pos[(i + 1) * 3 + 1] = nd.pos[1]; pos[(i + 1) * 3 + 2] = nd.pos[2];
        size[i + 1] = nd.size; shade[i + 1] = nd.shade;
      });
      this.count = n;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bPos); gl.bufferData(gl.ARRAY_BUFFER, pos, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bSize); gl.bufferData(gl.ARRAY_BUFFER, size, gl.STATIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bShade); gl.bufferData(gl.ARRAY_BUFFER, shade, gl.DYNAMIC_DRAW);

      /* Edges. Every node holds a faint line to the hub — that is the real
         relationship — plus one to its nearest sibling, which is what stops the
         picture reading as a firework. Sibling links carry no meaning of their
         own, so they are drawn dimmer than the spokes: structure for the eye,
         not a claim about the data. */
      this._links = [];
      this.nodes.forEach((nd, i) => this._links.push([null, i, 0.42, 0.10]));
      this.nodes.forEach((nd, i) => {
        let best = -1, bestD = Infinity;
        for (let j = 0; j < this.nodes.length; j++) {
          if (j === i) continue;
          const o = this.nodes[j];
          const dx = o.home[0] - nd.home[0], dy = o.home[1] - nd.home[1], dz = o.home[2] - nd.home[2];
          const d = dx * dx + dy * dy + dz * dz;
          if (d < bestD) { bestD = d; best = j; }
        }
        if (best >= 0 && best > i) this._links.push([i, best, 0.16, 0.16]);
      });
      const lp = new Float32Array(this._links.length * 6), ls = new Float32Array(this._links.length * 2);
      this._links.forEach((lk, i) => {
        const a = lk[0] === null ? [0, 0, 0] : this.nodes[lk[0]].pos;
        const b = this.nodes[lk[1]].pos;
        lp[i * 6] = a[0]; lp[i * 6 + 1] = a[1]; lp[i * 6 + 2] = a[2];
        lp[i * 6 + 3] = b[0]; lp[i * 6 + 4] = b[1]; lp[i * 6 + 5] = b[2];
        ls[i * 2] = lk[2]; ls[i * 2 + 1] = lk[3];
      });
      this.lineCount = this._links.length * 2;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bLine); gl.bufferData(gl.ARRAY_BUFFER, lp, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bLineShade); gl.bufferData(gl.ARRAY_BUFFER, ls, gl.STATIC_DRAW);
    }

    /* ── camera ─────────────────────────────────────────────────────────── */
    orbit(dx, dy) {
      this.yaw += dx * 0.006;
      this.pitch = Math.max(-1.35, Math.min(1.35, this.pitch + dy * 0.006));
      this.wantFocus = true;
    }
    zoom(delta) {
      this.distWant = Math.max(4.5, Math.min(40, this.distWant * (1 + delta * 0.0012)));
      this.wantFocus = true;
    }
    setFocus(on) { this.wantFocus = !!on; }

    update(dt) {
      if (!this.ok) return;
      this._t += dt;
      const want = this.wantFocus ? 1 : 0;
      this.focus += (want - this.focus) * Math.min(1, 3.2 * dt);
      this.dist += (this.distWant - this.dist) * Math.min(1, 5 * dt);
      // Ambient drift only while nobody is holding it: a view that keeps
      // rotating under the cursor is a view you cannot read.
      if (this.focus < 0.02) this.yaw += this.spin * dt;

      // Nodes breathe around their home so the field reads as alive.
      const amp = 0.16 + 0.10 * this.focus;
      let moved = false;
      for (const nd of this.nodes) {
        const s = Math.sin(this._t * 0.55 + nd.phase), c = Math.cos(this._t * 0.4 + nd.phase);
        nd.pos[0] = nd.home[0] + s * amp;
        nd.pos[1] = nd.home[1] + c * amp;
        nd.pos[2] = nd.home[2] + s * c * amp;
        moved = true;
      }
      if (moved) this._reupload();
    }

    _reupload() {
      const gl = this.gl;
      const pos = new Float32Array(this.count * 3);
      this.nodes.forEach((nd, i) => {
        pos[(i + 1) * 3] = nd.pos[0]; pos[(i + 1) * 3 + 1] = nd.pos[1]; pos[(i + 1) * 3 + 2] = nd.pos[2];
      });
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bPos); gl.bufferSubData(gl.ARRAY_BUFFER, 0, pos);
      const links = this._links || [];
      const lp = new Float32Array(links.length * 6);
      links.forEach((lk, i) => {
        const a = lk[0] === null ? [0, 0, 0] : this.nodes[lk[0]].pos;
        const b = this.nodes[lk[1]].pos;
        lp[i * 6] = a[0]; lp[i * 6 + 1] = a[1]; lp[i * 6 + 2] = a[2];
        lp[i * 6 + 3] = b[0]; lp[i * 6 + 4] = b[1]; lp[i * 6 + 5] = b[2];
      });
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bLine); gl.bufferSubData(gl.ARRAY_BUFFER, 0, lp);
    }

    view() {
      return mul(trans(0, 0, -this.dist), mul(rotX(this.pitch), rotY(this.yaw)));
    }

    /* ── drawing ────────────────────────────────────────────────────────── */
    draw(proj, tint, canvasH) {
      if (!this.ok || !this.nodes.length) return;
      const gl = this.gl;
      this._mvp = mul(proj, this.view());

      gl.enable(gl.DEPTH_TEST);
      gl.enable(gl.BLEND);
      // Additive: overlapping points accumulate into brightness the way a
      // constellation does, instead of punching holes in each other.
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      gl.depthMask(false);          // ...so they never occlude one another

      const alpha = 0.30 + 0.62 * this.focus;

      gl.useProgram(this.lprog);
      gl.uniformMatrix4fv(this.lu.mvp, false, this._mvp);
      gl.uniform3f(this.lu.tint, tint[0], tint[1], tint[2]);
      gl.uniform1f(this.lu.focus, this.focus);
      gl.uniform1f(this.lu.alpha, alpha * 0.8);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bLine);
      gl.enableVertexAttribArray(this.la.pos);
      gl.vertexAttribPointer(this.la.pos, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bLineShade);
      gl.enableVertexAttribArray(this.la.shade);
      gl.vertexAttribPointer(this.la.shade, 1, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.LINES, 0, this.lineCount);

      gl.useProgram(this.prog);
      gl.uniformMatrix4fv(this.u.mvp, false, this._mvp);
      // Pixels per unit of a_size at one unit of depth. Sized so a folder
      // node reads as a point of light at the default distance: too large and
      // additive blending turns the whole field into a white sheet, which is
      // exactly what the first render did.
      gl.uniform1f(this.u.scale, (canvasH || 900) * 0.012);
      gl.uniform3f(this.u.tint, tint[0], tint[1], tint[2]);
      gl.uniform1f(this.u.focus, this.focus);
      gl.uniform1f(this.u.alpha, alpha);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bPos);
      gl.enableVertexAttribArray(this.a.pos);
      gl.vertexAttribPointer(this.a.pos, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bSize);
      gl.enableVertexAttribArray(this.a.size);
      gl.vertexAttribPointer(this.a.size, 1, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bShade);
      gl.enableVertexAttribArray(this.a.shade);
      gl.vertexAttribPointer(this.a.shade, 1, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.POINTS, 0, this.count);

      gl.depthMask(true);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    }

    /* ── where a node is on screen, for picking and for labels ──────────── */
    project(pos, w, h) {
      const m = this._mvp;
      const x = m[0] * pos[0] + m[4] * pos[1] + m[8] * pos[2] + m[12];
      const y = m[1] * pos[0] + m[5] * pos[1] + m[9] * pos[2] + m[13];
      const wclip = m[3] * pos[0] + m[7] * pos[1] + m[11] * pos[2] + m[15];
      if (wclip <= 0.0001) return null;                 // behind the camera
      return { x: (x / wclip * 0.5 + 0.5) * w, y: (1 - (y / wclip * 0.5 + 0.5)) * h, w: wclip };
    }

    pick(sx, sy, w, h, radius) {
      if (!this.ok) return null;
      const r = radius || 26;
      let best = null, bestD = r * r;
      for (const nd of this.nodes) {
        const p = this.project(nd.pos, w, h);
        if (!p) continue;
        const dx = p.x - sx, dy = p.y - sy, d = dx * dx + dy * dy;
        // Ties go to the nearer node: two points over each other should hand
        // back the one the viewer thinks they are looking at.
        if (d < bestD || (d < r * r && best && p.w < best._w)) {
          best = nd; best._w = p.w; bestD = d;
        }
      }
      return best;
    }

    screenPos(node, w, h) { return node ? this.project(node.pos, w, h) : null; }
  }

  global.NetGraph = NetGraph;
})(window);
