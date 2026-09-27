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
    attribute vec3 a_color;       /* the category's colour */
    attribute float a_icon;       /* cell in the icon atlas */
    uniform mat4 u_mvp;
    uniform float u_scale;        /* pixels per unit of a_size, for this canvas */
    varying float v_shade;
    varying float v_depth;
    varying vec3 v_color;
    varying float v_icon;
    varying float v_px;
    void main(){
      vec4 clip = u_mvp * vec4(a_pos, 1.0);
      gl_Position = clip;
      /* Nearer points are larger, as they would be. clip.w is the camera
         distance, so dividing keeps perspective honest for points too. */
      gl_PointSize = max(2.0, u_scale * a_size / max(clip.w, 0.001));
      v_px = gl_PointSize;
      v_shade = a_shade;
      v_color = a_color;
      v_icon = a_icon;
      v_depth = clamp(clip.w / 24.0, 0.0, 1.0);
    }`;

  const FS = `
    precision mediump float;
    uniform vec3      u_tint;
    uniform float     u_focus;    /* 0 ambient (grey) .. 1 focus (in colour) */
    uniform float     u_alpha;
    uniform sampler2D u_icons;
    uniform float     u_cols;     /* atlas is u_cols x u_cols cells */
    varying float v_shade;
    varying float v_depth;
    varying vec3  v_color;
    varying float v_icon;
    varying float v_px;
    void main(){
      /* Round, soft-edged halo. Square points read as pixels, not as things. */
      vec2 d = gl_PointCoord - vec2(0.5);
      float r = length(d) * 2.0;
      float halo = smoothstep(1.0, 0.35, r);
      if (halo <= 0.003) discard;

      /* Ambient keeps the categories present but almost drained — enough that
         the eye can tell a folder from a file without the palette shouting for
         attention behind her head. Focus brings them fully up. */
      vec3 grey = vec3(0.62, 0.66, 0.72);
      vec3 cat  = mix(v_color, u_tint, 0.15);
      vec3 col  = mix(mix(grey, cat, 0.35), cat, u_focus);
      col = mix(col * 0.6, col, v_shade);

      /* The glyph, read from the atlas cell this node was given. Drawn inside
         the halo so the point keeps its round silhouette at any size. */
      vec2 cell = vec2(mod(v_icon, u_cols), floor(v_icon / u_cols));
      vec2 uv = (cell + clamp((gl_PointCoord - 0.5) * 1.5 + 0.5, 0.0, 1.0)) / u_cols;
      /* A glyph drawn into ten pixels is mush, and mush at a hundred points is
         a smear. Below that the node stays an honest coloured dot; zooming in
         brings the icon up. */
      float room = smoothstep(13.0, 26.0, v_px) * u_focus;
      float glyph = texture2D(u_icons, uv).a * room;

      float fade = mix(1.0, 0.25, v_depth);
      float a = max(halo * (0.62 - 0.25 * room), glyph);
      col += glyph * 0.5;
      gl_FragColor = vec4(col, a * u_alpha * fade);
    }`;

  const LVS = `
    attribute vec3 a_pos;
    attribute float a_shade;
    attribute float a_t;          /* 0 at the hub end, 1 at the node end */
    uniform mat4 u_mvp;
    varying float v_shade;
    varying float v_depth;
    varying float v_t;
    void main(){
      vec4 clip = u_mvp * vec4(a_pos, 1.0);
      gl_Position = clip;
      v_shade = a_shade;
      v_t = a_t;
      v_depth = clamp(clip.w / 24.0, 0.0, 1.0);
    }`;

  const LFS = `
    precision mediump float;
    uniform vec3  u_tint;
    uniform float u_focus;
    uniform float u_alpha;
    uniform float u_time;
    uniform float u_flow;         /* how much traffic to suggest, 0..1 */
    varying float v_shade;
    varying float v_depth;
    varying float v_t;
    void main(){
      vec3 grey = vec3(0.50, 0.54, 0.60);
      vec3 col  = mix(grey, u_tint, u_focus);
      float fade = mix(1.0, 0.18, v_depth);
      /* A short bright run travelling from the hub outward. Deliberately one
         short segment rather than a dashed line: dashes read as a border, a
         single moving run reads as something being carried. */
      float d = fract(v_t * 1.35 - u_time * 0.34);
      float pulse = smoothstep(0.0, 0.05, d) * (1.0 - smoothstep(0.05, 0.22, d));
      float a = v_shade + pulse * u_flow * (0.35 + 0.65 * u_focus);
      col += pulse * u_flow * 0.5 * mix(vec3(0.8), u_tint, u_focus);
      gl_FragColor = vec4(col, a * u_alpha * fade);
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

  /* One colour per category, and a glyph drawn into an atlas, so a point says
     what it is without being hovered. Chosen to stay apart from each other on a
     dark background AND to survive the ambient view, where everything is mixed
     most of the way to grey: a palette that only works at full saturation reads
     as noise for the hours this sits behind her head.

     The brand marks are drawn HERE, in code, deliberately. Shipping vendor logo
     files in an MIT repository is a trademark question nobody needs. Anyone who
     wants the official artwork can drop SVGs into suni/web/icons/ and they are
     used in preference. */
  const KIND_STYLE = {
    root:    { color: [0.85, 0.93, 1.00], icon: "hub",     label: "SUNI" },
    machine: { color: [0.49, 0.83, 1.00], icon: "chip",    label: "machine / host" },
    model:   { color: [1.00, 0.71, 0.33], icon: "model",   label: "model" },
    tool:    { color: [0.61, 0.86, 0.42], icon: "tool",    label: "tool" },
    skill:   { color: [0.82, 0.55, 1.00], icon: "skill",   label: "skill / agent" },
    channel: { color: [1.00, 0.54, 0.69], icon: "channel", label: "channel" },
    folder:  { color: [1.00, 0.84, 0.42], icon: "folder",  label: "folder" },
    file:    { color: [0.56, 0.64, 0.72], icon: "file",    label: "file" },
  };

  const BRANDS = [
    { test: /ollama/i,           icon: "ollama", color: [0.93, 0.93, 0.93] },
    { test: /claude|anthropic/i, icon: "claude", color: [0.85, 0.47, 0.26] },
    { test: /gpt|openai|codex/i, icon: "openai", color: [0.45, 0.85, 0.70] },
    { test: /gemini/i,           icon: "spark",  color: [0.45, 0.68, 1.00] },
    { test: /qwen/i,             icon: "model",  color: [0.62, 0.45, 1.00] },
    { test: /llama|mistral/i,    icon: "model",  color: [0.98, 0.62, 0.35] },
  ];

  const ICON_ORDER = ["hub", "chip", "model", "tool", "skill", "channel",
                      "folder", "file", "ollama", "claude", "openai", "spark"];
  const ATLAS_COLS = 4, ATLAS_CELL = 128;

  function _drawIcon(ctx, name, s) {
    /* White on transparent; the shader tints it. */
    const c = s / 2, u = s / 100;
    ctx.save();
    ctx.translate(c, c);
    ctx.strokeStyle = "#fff"; ctx.fillStyle = "#fff";
    ctx.lineWidth = 7 * u; ctx.lineCap = "round"; ctx.lineJoin = "round";
    const R = 30 * u;
    const P = (i) => (i ? "lineTo" : "moveTo");
    switch (name) {
      case "hub":
        ctx.beginPath(); ctx.arc(0, 0, R * 0.5, 0, 6.283); ctx.fill();
        for (let i = 0; i < 6; i++) {
          const a = i * 1.047;
          ctx.beginPath();
          ctx.moveTo(Math.cos(a) * R * 0.72, Math.sin(a) * R * 0.72);
          ctx.lineTo(Math.cos(a) * R * 1.28, Math.sin(a) * R * 1.28);
          ctx.stroke();
        }
        break;
      case "chip":
        ctx.strokeRect(-R * 0.72, -R * 0.72, R * 1.44, R * 1.44);
        ctx.strokeRect(-R * 0.28, -R * 0.28, R * 0.56, R * 0.56);
        for (let i = -1; i <= 1; i++) {
          ctx.beginPath(); ctx.moveTo(i * R * 0.5, -R * 1.2); ctx.lineTo(i * R * 0.5, -R * 0.72); ctx.stroke();
          ctx.beginPath(); ctx.moveTo(i * R * 0.5, R * 0.72); ctx.lineTo(i * R * 0.5, R * 1.2); ctx.stroke();
        }
        break;
      case "model":
        for (let i = -1; i <= 1; i++) {
          ctx.beginPath();
          ctx.moveTo(-R, i * R * 0.58); ctx.lineTo(0, i * R * 0.58 - R * 0.4);
          ctx.lineTo(R, i * R * 0.58); ctx.lineTo(0, i * R * 0.58 + R * 0.4);
          ctx.closePath(); ctx.stroke();
        }
        break;
      case "tool":
        ctx.beginPath(); ctx.arc(-R * 0.45, -R * 0.45, R * 0.42, 0.6, 5.2); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(-R * 0.2, -R * 0.2); ctx.lineTo(R * 0.85, R * 0.85); ctx.stroke();
        break;
      case "skill":
        ctx.beginPath();
        for (let i = 0; i < 10; i++) {
          const rr = i % 2 ? R * 0.46 : R, a = -1.571 + i * 0.628;
          ctx[P(i)](Math.cos(a) * rr, Math.sin(a) * rr);
        }
        ctx.closePath(); ctx.fill();
        break;
      case "channel":
        ctx.beginPath();
        ctx.moveTo(-R, -R * 0.7); ctx.lineTo(R, -R * 0.7); ctx.lineTo(R, R * 0.32);
        ctx.lineTo(-R * 0.25, R * 0.32); ctx.lineTo(-R * 0.6, R); ctx.lineTo(-R * 0.6, R * 0.32);
        ctx.lineTo(-R, R * 0.32); ctx.closePath(); ctx.stroke();
        break;
      case "folder":
        ctx.beginPath();
        ctx.moveTo(-R, R * 0.7); ctx.lineTo(-R, -R * 0.55); ctx.lineTo(-R * 0.15, -R * 0.55);
        ctx.lineTo(R * 0.05, -R * 0.2); ctx.lineTo(R, -R * 0.2); ctx.lineTo(R, R * 0.7);
        ctx.closePath(); ctx.stroke();
        break;
      case "file":
        ctx.beginPath();
        ctx.moveTo(-R * 0.68, -R); ctx.lineTo(R * 0.24, -R); ctx.lineTo(R * 0.68, -R * 0.5);
        ctx.lineTo(R * 0.68, R); ctx.lineTo(-R * 0.68, R); ctx.closePath(); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(R * 0.24, -R); ctx.lineTo(R * 0.24, -R * 0.5);
        ctx.lineTo(R * 0.68, -R * 0.5); ctx.stroke();
        break;
      case "ollama":
        ctx.beginPath(); ctx.moveTo(-R * 0.45, -R * 0.3); ctx.lineTo(-R * 0.58, -R);
        ctx.lineTo(-R * 0.18, -R * 0.52); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(R * 0.45, -R * 0.3); ctx.lineTo(R * 0.58, -R);
        ctx.lineTo(R * 0.18, -R * 0.52); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(-R * 0.5, -R * 0.22);
        ctx.quadraticCurveTo(0, -R * 0.58, R * 0.5, -R * 0.22);
        ctx.lineTo(R * 0.34, R * 0.5);
        ctx.quadraticCurveTo(0, R * 0.88, -R * 0.34, R * 0.5);
        ctx.closePath(); ctx.stroke();
        break;
      case "claude":
        for (let i = 0; i < 8; i++) {
          const a = i * 0.785;
          ctx.beginPath();
          ctx.moveTo(Math.cos(a) * R * 0.22, Math.sin(a) * R * 0.22);
          ctx.lineTo(Math.cos(a) * R, Math.sin(a) * R);
          ctx.stroke();
        }
        break;
      case "openai":
        ctx.beginPath();
        for (let i = 0; i < 6; i++) {
          const a = i * 1.047;
          ctx[P(i)](Math.cos(a) * R, Math.sin(a) * R);
        }
        ctx.closePath(); ctx.stroke();
        ctx.beginPath(); ctx.arc(0, 0, R * 0.34, 0, 6.283); ctx.stroke();
        break;
      case "spark":
        ctx.beginPath();
        ctx.moveTo(0, -R); ctx.quadraticCurveTo(R * 0.2, -R * 0.2, R, 0);
        ctx.quadraticCurveTo(R * 0.2, R * 0.2, 0, R);
        ctx.quadraticCurveTo(-R * 0.2, R * 0.2, -R, 0);
        ctx.quadraticCurveTo(-R * 0.2, -R * 0.2, 0, -R);
        ctx.closePath(); ctx.stroke();
        break;
      default:
        ctx.beginPath(); ctx.arc(0, 0, R * 0.5, 0, 6.283); ctx.stroke();
    }
    ctx.restore();
  }

  function _buildAtlas() {
    const size = ATLAS_COLS * ATLAS_CELL;
    const cv = document.createElement("canvas");
    cv.width = cv.height = size;
    const ctx = cv.getContext("2d");
    ICON_ORDER.forEach((name, i) => {
      ctx.save();
      ctx.translate((i % ATLAS_COLS) * ATLAS_CELL,
                    Math.floor(i / ATLAS_COLS) * ATLAS_CELL);
      _drawIcon(ctx, name, ATLAS_CELL);
      ctx.restore();
    });
    return cv;
  }

  function styleFor(node) {
    const base = KIND_STYLE[node.kind] || KIND_STYLE.file;
    const text = (node.label || "") + " " + (node.id || "");
    if (node.kind === "model" || node.kind === "machine" || node.kind === "skill") {
      for (const b of BRANDS) {
        if (b.test.test(text)) return { color: b.color, icon: b.icon };
      }
    }
    return { color: base.color, icon: base.icon };
  }

  /* How big a node draws. Weight is what it holds: files under a folder, tools
     in a registry, tokens in a budget — whatever the level's count means. Taken
     as a logarithm because these counts span four orders of magnitude here
     (a folder of 2 files and one of 7,637), and a linear scale would make one
     node a disc and the rest full stops. */
  function sizeFor(node) {
    const base = SIZE_BY_KIND[node.kind] || 12;
    const n = Number(node.count || node.more || 0);
    if (!n || n < 2) return base;
    return base * (1 + Math.min(1.25, Math.log10(n) * 0.42));
  }

  class NetGraph {
    constructor(gl) {
      this.gl = gl;
      this.prog = program(gl, VS, FS);
      this.lprog = program(gl, LVS, LFS);
      this.ok = !!(this.prog && this.lprog);
      if (!this.ok) return;

      this.bPos = gl.createBuffer(); this.bSize = gl.createBuffer(); this.bShade = gl.createBuffer();
      this.bColor = gl.createBuffer(); this.bIcon = gl.createBuffer();
      this.bLine = gl.createBuffer(); this.bLineShade = gl.createBuffer();
      this.bLineT = gl.createBuffer();

      this.a = {
        pos: gl.getAttribLocation(this.prog, 'a_pos'),
        size: gl.getAttribLocation(this.prog, 'a_size'),
        shade: gl.getAttribLocation(this.prog, 'a_shade'),
        color: gl.getAttribLocation(this.prog, 'a_color'),
        icon: gl.getAttribLocation(this.prog, 'a_icon'),
      };
      this.u = {
        mvp: gl.getUniformLocation(this.prog, 'u_mvp'),
        scale: gl.getUniformLocation(this.prog, 'u_scale'),
        tint: gl.getUniformLocation(this.prog, 'u_tint'),
        focus: gl.getUniformLocation(this.prog, 'u_focus'),
        alpha: gl.getUniformLocation(this.prog, 'u_alpha'),
        icons: gl.getUniformLocation(this.prog, 'u_icons'),
        cols: gl.getUniformLocation(this.prog, 'u_cols'),
      };
      this.tex = this._makeAtlas();
      this.la = {
        pos: gl.getAttribLocation(this.lprog, 'a_pos'),
        shade: gl.getAttribLocation(this.lprog, 'a_shade'),
        t: gl.getAttribLocation(this.lprog, 'a_t'),
      };
      this.lu = {
        mvp: gl.getUniformLocation(this.lprog, 'u_mvp'),
        tint: gl.getUniformLocation(this.lprog, 'u_tint'),
        focus: gl.getUniformLocation(this.lprog, 'u_focus'),
        alpha: gl.getUniformLocation(this.lprog, 'u_alpha'),
        time: gl.getUniformLocation(this.lprog, 'u_time'),
        flow: gl.getUniformLocation(this.lprog, 'u_flow'),
      };
      this.flow = 0.35;       // ambient traffic; the page raises it when busy

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

    _makeAtlas() {
      const gl = this.gl;
      const tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      /* One transparent pixel until the glyphs are drawn, so a slow canvas
         never leaves the first frames sampling nothing. */
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE,
                    new Uint8Array([255, 255, 255, 0]));
      try {
        const cv = _buildAtlas();
        gl.bindTexture(gl.TEXTURE_2D, tex);
        gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, cv);
        gl.generateMipmap(gl.TEXTURE_2D);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      } catch (e) {
        console.warn('[NET] icon atlas:', e);
      }
      return tex;
    }

    /* What each category looks like, for the legend on the page. */
    static legend() {
      return Object.keys(KIND_STYLE)
        .filter(k => k !== 'root')
        .map(k => ({ kind: k, label: KIND_STYLE[k].label, icon: KIND_STYLE[k].icon,
                     color: KIND_STYLE[k].color }));
    }

    static iconDataURL(name, size) {
      const cell = size || 22;
      const cv = document.createElement('canvas');
      cv.width = cv.height = cell;
      _drawIcon(cv.getContext('2d'), name, cell);
      return cv.toDataURL();
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
        const st = styleFor(nd);
        return {
          id: nd.id, label: nd.label, kind: nd.kind, meta: nd,
          home, pos: home.slice(),
          size: sizeFor(nd),
          color: st.color,
          icon: Math.max(0, ICON_ORDER.indexOf(st.icon)),
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
      const color = new Float32Array(n * 3), icon = new Float32Array(n);
      pos[0] = 0; pos[1] = 0; pos[2] = 0; size[0] = SIZE_BY_KIND.root; shade[0] = 1;
      color[0] = KIND_STYLE.root.color[0]; color[1] = KIND_STYLE.root.color[1];
      color[2] = KIND_STYLE.root.color[2];
      icon[0] = Math.max(0, ICON_ORDER.indexOf(KIND_STYLE.root.icon));
      this.nodes.forEach((nd, i) => {
        pos[(i + 1) * 3] = nd.pos[0]; pos[(i + 1) * 3 + 1] = nd.pos[1]; pos[(i + 1) * 3 + 2] = nd.pos[2];
        size[i + 1] = nd.size; shade[i + 1] = nd.shade;
        color[(i + 1) * 3] = nd.color[0]; color[(i + 1) * 3 + 1] = nd.color[1];
        color[(i + 1) * 3 + 2] = nd.color[2];
        icon[i + 1] = nd.icon;
      });
      this.count = n;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bPos); gl.bufferData(gl.ARRAY_BUFFER, pos, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bSize); gl.bufferData(gl.ARRAY_BUFFER, size, gl.STATIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bShade); gl.bufferData(gl.ARRAY_BUFFER, shade, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bColor); gl.bufferData(gl.ARRAY_BUFFER, color, gl.STATIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bIcon); gl.bufferData(gl.ARRAY_BUFFER, icon, gl.STATIC_DRAW);

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
      const lt = new Float32Array(this._links.length * 2);
      for (let i = 0; i < this._links.length; i++) { lt[i * 2] = 0; lt[i * 2 + 1] = 1; }
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bLineT); gl.bufferData(gl.ARRAY_BUFFER, lt, gl.STATIC_DRAW);
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
      gl.uniform1f(this.lu.time, this._t);
      gl.uniform1f(this.lu.flow, this.flow);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bLine);
      gl.enableVertexAttribArray(this.la.pos);
      gl.vertexAttribPointer(this.la.pos, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bLineShade);
      gl.enableVertexAttribArray(this.la.shade);
      gl.vertexAttribPointer(this.la.shade, 1, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bLineT);
      gl.enableVertexAttribArray(this.la.t);
      gl.vertexAttribPointer(this.la.t, 1, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.LINES, 0, this.lineCount);

      gl.useProgram(this.prog);
      gl.uniformMatrix4fv(this.u.mvp, false, this._mvp);
      // Pixels per unit of a_size at one unit of depth. Sized so a folder
      // node reads as a point of light at the default distance: too large and
      // additive blending turns the whole field into a white sheet, which is
      // exactly what the first render did.
      // Bigger in focus, where the field is the thing being looked at rather
      // than the backdrop — and big enough there for a glyph to survive.
      gl.uniform1f(this.u.scale, (canvasH || 900) * (0.013 + 0.012 * this.focus));
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
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bColor);
      gl.enableVertexAttribArray(this.a.color);
      gl.vertexAttribPointer(this.a.color, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bIcon);
      gl.enableVertexAttribArray(this.a.icon);
      gl.vertexAttribPointer(this.a.icon, 1, gl.FLOAT, false, 0, 0);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.tex);
      gl.uniform1i(this.u.icons, 0);
      gl.uniform1f(this.u.cols, ATLAS_COLS);
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
