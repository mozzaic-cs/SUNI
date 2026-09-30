/* netgraph.js — the network SUNI sits in front of.
 *
 * Nodes are what she can reach: the machine, the models and services, the
 * skills, and every folder and file she has indexed. One level is on screen at
 * a time; the server decides what that level contains (see suni/graph.py).
 *
 * Two moods, because this thing is on screen for hours:
 *
 *   AMBIENT — dim and slow, but in colour. It is behind her head and it is not asking for
 *             attention. Readable as depth and activity, not as reading matter.
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
    uniform float u_cols;         /* atlas is u_cols x u_cols cells */
    varying vec2  v_cell;
    varying float v_shade;
    varying float v_depth;
    varying vec3 v_color;
    varying float v_px;
    void main(){
      vec4 clip = u_mvp * vec4(a_pos, 1.0);
      gl_Position = clip;
      /* Nearer points are larger, as they would be. clip.w is the camera
         distance, so dividing keeps perspective honest for points too. */
      /* HALO_SPRITE wider than the ball needs, so there is room around it
         for the glow to fall off inside the same point. This is the whole
         bloom budget: no second pass, no render target, no extra VRAM on a
         card that is already evicting the language model to make room. */
      float px = max(2.0, u_scale * a_size / max(clip.w, 0.001));
      gl_PointSize = px * 1.55;
      v_px = px;                 /* the BALL's size — what the glyph reads */
      v_shade = a_shade;
      v_color = a_color;
      /* Which atlas cell, worked out HERE rather than in the fragment stage.
         mod()/floor() on an index that lands exactly on a row boundary is a
         coin toss at mediump: 4.0/4.0 comes back as 0.99999 and floor() drops
         it a whole row. That is why the star flickered and nothing else did —
         "skill" is the icon whose index is exactly one row in. Vertex-stage
         float is highp, and the varying only ever carries small whole
         numbers, which survive the trip. */
      float idx = floor(a_icon + 0.5);
      v_cell = vec2(floor(mod(idx, u_cols)), floor(idx / u_cols + 0.001));
      v_depth = clamp(clip.w / 24.0, 0.0, 1.0);
    }`;

  const FS = `
    /* highp where the device has it: the icon atlas is sampled per pixel and
       mediump uv over a multi-cell atlas bleeds between neighbours. */
    #ifdef GL_FRAGMENT_PRECISION_HIGH
    precision highp float;
    #else
    precision mediump float;
    #endif
    uniform vec3      u_tint;
    uniform float     u_focus;    /* 0 ambient (grey) .. 1 focus (in colour) */
    uniform float     u_alpha;
    uniform sampler2D u_icons;
    uniform float     u_cols;     /* atlas is u_cols x u_cols cells */
    uniform float     u_time;
    uniform float     u_pass;     /* 0 = the solid ball, 1 = the light around it */
    varying float v_shade;
    varying float v_depth;
    varying vec3  v_color;
    varying vec2  v_cell;
    varying float v_px;
    void main(){
      /* A sphere, not a smudge. The point is shaded as a ball lit from the
         upper left: solid through the middle, falling off at the limb, with a
         hard antialiased edge. Soft additive halos were why the palette looked
         washed out — everything overlapped everything and the colours averaged
         toward white. */
      /* The sprite is 1.55x the ball, so the ball lives in the inner 0.645
         of it and the rest is glow. Coordinates are rescaled to the ball, so
         every lighting term below is unchanged by the sprite growing. */
      vec2 sprite = gl_PointCoord * 2.0 - 1.0;
      sprite.y = -sprite.y;
      float sr = length(sprite);
      vec2 d = sprite / 0.645;
      float r2 = dot(d, d);
      float z = sqrt(max(0.0, 1.0 - min(r2, 1.0)));
      vec3 n = vec3(d, z);
      vec3 lightDir = normalize(vec3(-0.38, 0.52, 0.76));
      float lam = clamp(dot(n, lightDir), 0.0, 1.0);
      float rim = pow(1.0 - z, 2.5);

      /* Category colour, saturated rather than averaged toward the tint. Both
         moods are in colour; ambient is simply darker. */
      vec3 cat = mix(v_color, u_tint, 0.10);
      float luma = dot(cat, vec3(0.299, 0.587, 0.114));
      cat = clamp(mix(vec3(luma), cat, 1.45), 0.0, 1.0);      /* more saturated */
      vec3 col = cat * (0.34 + 0.86 * lam) * (0.72 + 0.28 * u_focus);

      /* ── the same light her head is made of ──────────────────────────────
         Her hologram is a cool fresnel rim, fine static scanlines and a faint
         per-frame flicker (see FS2 in face.html). The field borrowed none of
         it and read as a chart sitting in front of a hologram rather than as
         part of the same object.

         DELIBERATELY NOT A BLOOM PASS. A framebuffer at this machine's
         resolution is tens of megabytes of render target on an 8 GB card that
         is already evicting the language model to make room — measured today.
         All of this is per-fragment arithmetic and costs no memory at all. */

      /* A rim in her own colour, not the node's, so every node is lit by the
         same source she is. Chromatic: the channels read the falloff at
         slightly different radii, which is what makes a rim look projected
         rather than painted. */
      vec3 rimCol = mix(vec3(0.50, 0.85, 1.00), u_tint, 0.55);
      float r0 = pow(1.0 - z, 2.2);
      float r1 = pow(1.0 - z, 2.5);
      float r2c = pow(1.0 - z, 2.9);
      col += rimCol * vec3(r0, r1, r2c) * (0.55 + 0.75 * u_focus);
      col += rim * 0.22 * cat;

      /* Fine static scanlines — the texture of a projection. Shallow, because
         the whole point of this field is that the colours stay legible. */
      float sl = 0.5 + 0.5 * sin(gl_FragCoord.y * 1.5);
      col *= 0.88 + 0.12 * sl;

      /* Per-frame flicker, quantised so it reads as an unsteady projector
         rather than as noise. */
      col *= 0.97 + 0.03 * fract(sin(floor(u_time * 14.0) * 12.9898) * 43758.5453);

      col = mix(col * 0.74, col, v_shade);

      /* The glyph sits on the ball, bright enough to read against it. Below
         about thirteen pixels it would be mush, so it fades in with size. */
      /* 1.82, not the 1.42 this started with. The sprite is 1.55x the ball, so
         at 1.42 the glyph spanned 1.09 x the ball DIAMETER — it overflowed the
         thing it was supposed to be sitting on, which is why the icons read as
         too big for their orbs. At 1.82 it covers 0.85 of the ball and sits
         inside it. */
      vec2 uv = (v_cell + clamp(gl_PointCoord * 1.82 - 0.41, 0.0, 1.0)) / u_cols;
      float room = smoothstep(13.0, 26.0, v_px) * (0.35 + 0.65 * u_focus);
      float glyph = texture2D(u_icons, uv).a * room;
      col = mix(col, mix(vec3(1.0), cat + 0.55, 0.35), glyph * 0.85);

      /* Solid, with the far side of the field receding rather than vanishing. */
      float edge = smoothstep(1.0, 0.88, r2);
      float fade = mix(1.0, 0.55, v_depth);

      /* THE GLOW. Outside the ball the same colour falls off as light rather
         than as surface — which is what a bloom pass would have produced, at
         the cost of a framebuffer this machine cannot spare. Brighter in
         focus, and stronger for the big nodes, so importance reads as radiance
         instead of only as diameter. */
      /* Falls off faster (2.1, was 1.7) and peaks lower (1.30, was 2.10). At
         the old figures a focused node was carrying 2.65x its own colour in
         light, which on a big node washed out both the glyph on it and the
         nodes beside it. The nebula arrived since those numbers were chosen
         and does the work they were overreaching for: depth now comes from
         the wash behind a cluster, so each node needs less of its own. */
      float halo = pow(1.0 - clamp(sr, 0.0, 1.0), 2.1);
      vec3 glow = mix(cat, rimCol, 0.40) * halo
                * (0.42 + 1.30 * u_focus) * (0.55 + 0.45 * v_shade);

      /* TWO PASSES, because light adds and surfaces do not. The ball is
         alpha-blended and writes depth, so it occludes what is behind it; the
         glow is drawn afterwards ADDITIVELY with depth writes off. Done in one
         pass the halo composited at low alpha against a near-black background
         and disappeared — and what little showed punched a depth hole around
         every node. */
      if (u_pass < 0.5) {
        if (edge < 0.004) discard;
        gl_FragColor = vec4(col, edge * u_alpha * fade);
      } else {
        if (halo < 0.004) discard;
        gl_FragColor = vec4(glow * u_alpha * fade, 1.0);
      }
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
      /* Edges take her state colour in both moods — they are the connective
         tissue, not a category, so they should not compete with the nodes. */
      vec3 col = u_tint * (0.55 + 0.45 * u_focus);
      float fade = mix(1.0, 0.18, v_depth);
      /* A bright run travelling from the hub outward, with a tail behind it.
         The first version was too polite to notice at a glance: this one is
         faster, brighter and longer, and the line it runs along is dimmer, so
         the movement is the thing the eye catches rather than the wire. */
      float d = fract(v_t * 1.5 - u_time * 0.55);
      float head = smoothstep(0.0, 0.04, d) * (1.0 - smoothstep(0.04, 0.17, d));
      float tail = (1.0 - smoothstep(0.0, 0.42, d)) * 0.35;
      float pulse = clamp(head + tail, 0.0, 1.0);
      float a = v_shade * 0.8 + pulse * u_flow * (0.7 + 0.5 * u_focus);
      /* The travelling head splits into its channels, like the sweep on her
         face does when she materialises — a mis-converged display rather than
         a clean white dot. */
      vec3 split = vec3(
        smoothstep(0.0, 0.05, fract(v_t * 1.5 - u_time * 0.55 + 0.010)),
        smoothstep(0.0, 0.05, d),
        smoothstep(0.0, 0.05, fract(v_t * 1.5 - u_time * 0.55 - 0.010)));
      col += pulse * u_flow * 0.9 * mix(vec3(0.85), u_tint + 0.25, u_focus)
             * mix(vec3(1.0), split, 0.55 * u_focus);
      /* Scanlines on the wires too, or the nodes look projected and the links
         between them do not. */
      col *= 0.90 + 0.10 * (0.5 + 0.5 * sin(gl_FragCoord.y * 1.5));
      gl_FragColor = vec4(col, a * u_alpha * fade);
    }`;

  /* ── the nebula ───────────────────────────────────────────────────────
     A soft wash of each category's colour behind its own cluster. In the
     reference this is what carries the depth: without it a field of coloured
     dots on black reads as a chart, and with it the dots sit INSIDE something.

     ONE fullscreen triangle summing a handful of gaussians, rather than a blur
     of the scene. A real blur needs a framebuffer, and a render target at this
     machine's resolution is tens of megabytes on a card that is already
     evicting the language model to make room — measured, in Ollama's own log.
     This costs one draw call and no memory.

     Screen-space on purpose: the centres are projected on the CPU, so a
     cluster's glow follows it through every camera move and layout change
     without the shader knowing anything about the scene. */
  // Headroom over the ten categories that exist: an eleventh should join the
  // field, not quietly lose its wash.
  const NEB_MAX = 16;

  /* How far under its node a name is drawn. One definition, used both to place
     the span and to test whether that span lands somewhere it must not. */
  function labelDrop(px) { return px * 0.55 + 4; }

  /* How far a name reaches either side of its node. The span is centred on the
     point, so the text is the thing that collides, not the point. Monospace at
     a known size, so counting characters is exact enough and costs nothing —
     measuring text properly means a canvas context and a layout flush. */
  function textHalfWidth(label, charW) {
    return ((label || '').length * (charW || 6)) / 2;
  }

  const NVS = `
    attribute vec2 a_xy;
    void main(){ gl_Position = vec4(a_xy, 0.999, 1.0); }`;

  const NFS = `
    precision mediump float;
    uniform vec2  u_res;
    uniform int   u_count;
    uniform vec3  u_pos[${NEB_MAX}];    /* x, y in pixels; z = radius */
    uniform vec3  u_col[${NEB_MAX}];
    uniform float u_gain;
    void main(){
      vec2 p = gl_FragCoord.xy;
      vec3 sum = vec3(0.0);
      for (int i = 0; i < ${NEB_MAX}; i++) {
        if (i >= u_count) break;
        vec3 c = u_pos[i];
        float d = length(p - c.xy) / max(c.z, 1.0);
        /* Gaussian rather than a hard falloff: the edge of a wash should not
           be findable, or it reads as a circle drawn behind the nodes. */
        sum += u_col[i] * exp(-d * d * 2.3);
      }
      sum *= u_gain;
      /* Belt as well as braces: however many washes happen to overlap, this
         cannot reach white. Without it the whole field goes flat the moment
         two clusters sit on top of each other. */
      sum = sum / (1.0 + sum);
      /* Scanlines here too, or the background is the one surface in the scene
         that is not part of the projection. */
      sum *= 0.90 + 0.10 * (0.5 + 0.5 * sin(gl_FragCoord.y * 1.5));
      gl_FragColor = vec4(sum, 1.0);
    }`;

  /* ── the floor ────────────────────────────────────────────────────────
     Everything on this page floats in nothing. A plane underneath gives the
     projection somewhere to come FROM: a grid receding to darkness, and the
     pool of light it is being emitted out of.

     SCREEN SPACE, not a quad in the scene, and that is not a shortcut. The head
     and the field are drawn through two different cameras — the head has its
     own mvp and, once it shrinks, its own viewport in the corner — so a plane
     placed in either one lines up with that one and drifts from the other the
     moment she moves. Painted first in screen space it is under both by
     construction, and the perspective is done here instead: for a fragment
     below the horizon, the depth of the floor at that height is 1/(horizon-y),
     which is what makes the lines converge correctly.

     It still parallaxes, because the grid is panned by the field's own yaw —
     a floor that stays put while the network turns above it reads as a
     photograph of a floor.

     One fullscreen triangle, sharing the nebula's buffer. No framebuffer, as
     everywhere else here: this machine cannot spare a render target. */
  const FLOOR_Z = 6.0;          // how far in front of the camera the pool sits

  const FVS = `
    attribute vec2 a_xy;
    void main(){ gl_Position = vec4(a_xy, 0.9995, 1.0); }`;

  const FFS = `
    precision mediump float;
    uniform vec2  u_res;
    uniform vec3  u_tint;
    uniform float u_focus;
    uniform float u_pan;      /* grid offset, driven by the field's yaw */
    uniform float u_emit;     /* where the projector is, 0..1 across the screen */
    uniform float u_gain;

    void main(){
      vec2 uv = gl_FragCoord.xy / u_res;
      const float HORIZON = 0.31;
      /* Her own light, pulled towards the cool blue the rims use, so the floor
         belongs to the same projection as everything standing on it. */
      vec3 tint = mix(u_tint, vec3(0.50, 0.85, 1.00), 0.35);
      vec3 col = vec3(0.0);

      if (uv.y < HORIZON) {
        /* Depth of the floor at this height. Clamped away from the horizon
           itself, where it goes to infinity and mediump gives up. */
        float d = 0.95 / max(HORIZON - uv.y, 0.0016);
        float gx = (uv.x - 0.5) * d * 2.2 + u_pan;
        float gz = d;

        /* Lines kept a roughly constant width on SCREEN by widening them in
           world units as the floor recedes; otherwise the far grid collapses
           into a solid sheet. */
        float lw = clamp(0.018 * d, 0.004, 0.42);
        float lx = smoothstep(0.5 - lw, 0.5, abs(fract(gx) - 0.5));
        float lz = smoothstep(0.5 - lw, 0.5, abs(fract(gz) - 0.5));
        float grid = max(lx, lz);

        /* Gone well before the horizon, so there is no hard line across the
           screen where the floor stops. */
        float far = exp(-d * 0.085);

        /* The pool the whole thing is projected out of. Defined as a point ON
           the floor, so perspective flattens it into an ellipse by itself. */
        float poolX = (u_emit - 0.5) * FLOOR_Z_JS * 2.2;
        float pd = length(vec2(gx - poolX, gz - FLOOR_Z_JS));
        float pool = exp(-pd * pd * 0.20);

        col += tint * grid * far * 0.085;
        col += tint * pool * 0.32;
        /* A brighter ring right at the emitter's lip. */
        col += tint * smoothstep(0.55, 0.0, abs(pd - 1.5)) * 0.10;
      } else {
        /* Above the horizon: the light leaving the pool. Not a volume, just a
           soft column that fades with height — enough that the head looks lit
           from underneath rather than sitting in front of a picture. */
        float up = exp(-abs(uv.x - u_emit) * 7.0)
                 * exp(-(uv.y - HORIZON) * 3.4);
        col += tint * up * 0.16;
      }

      col *= u_gain;
      /* Scanlines, as on every other surface here. */
      col *= 0.90 + 0.10 * (0.5 + 0.5 * sin(gl_FragCoord.y * 1.5));
      gl_FragColor = vec4(col, 1.0);
    }`.replace(/FLOOR_Z_JS/g, FLOOR_Z.toFixed(2));

  /* ── the ring guides ──────────────────────────────────────────────────
     The rings layout has always put one category on each ring, and never drawn
     the rings. Without them the arrangement reads as scattered dots that
     happen to curve; with them it reads as what it is — a category per orbit,
     ordered from the middle out.

     One unit circle in a buffer, scaled per ring by a uniform, so nine rings
     are nine small draws over 96 vertices rather than nine buffers. */
  const RING_SEGMENTS = 96;

  const GVS = `
    attribute vec2 a_unit;
    uniform mat4 u_mvp;
    uniform vec2 u_scale;
    void main(){
      gl_Position = u_mvp * vec4(a_unit.x * u_scale.x, a_unit.y * u_scale.y, 0.0, 1.0);
    }`;

  const GFS = `
    precision mediump float;
    uniform vec3  u_col;
    uniform float u_alpha;
    void main(){
      /* Scanlines, like everything else in this field: a guide that is not
         part of the projection looks stuck on top of it. */
      float sl = 0.86 + 0.14 * (0.5 + 0.5 * sin(gl_FragCoord.y * 1.5));
      gl_FragColor = vec4(u_col * sl, u_alpha);
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
  /* How wide a NODE is in world units — and it is the same at every distance.
     A point is drawn at u_scale·size/w pixels while a world unit spans
     (viewportHeight/2)/(d·tan(fov/2)) pixels; both go as 1/d, so they cancel.
     A layout spaced more tightly than this overlaps at EVERY zoom, and pulling
     the camera back never fixes it. Learned on the architecture panels — the
     same arithmetic governs these. */
  const NODE_W = 1.35;

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
    skill:   { color: [0.82, 0.55, 1.00], icon: "skill",   label: "skill" },
    agent:   { color: [0.45, 1.00, 0.85], icon: "agent",   label: "agent" },
    schedule:{ color: [0.98, 0.78, 0.30], icon: "clock",   label: "scheduled run" },
    channel: { color: [1.00, 0.54, 0.69], icon: "channel", label: "channel" },
    folder:  { color: [1.00, 0.84, 0.42], icon: "folder",  label: "folder" },
    file:    { color: [0.56, 0.64, 0.72], icon: "file",    label: "file" },
    /* The business, not just the machine it runs on. Agents already appear as
       staff; these are the people they work for and the work itself. */
    /* Periwinkle, not the teal it started as: an agent is already a figure in
       mint and a person in cyan beside it was two similar glyphs in two
       similar colours, which is the one pair on this palette a viewer most
       needs to tell apart. */
    user:    { color: [0.62, 0.70, 1.00], icon: "user",    label: "person" },
    project: { color: [1.00, 0.62, 0.48], icon: "project", label: "project" },
    /* The remainder of a level that was too big to draw. Grey on purpose: it
       is not a thing in the network, it is the network saying there is more of
       it. */
    more:    { color: [0.62, 0.68, 0.80], icon: "more",    label: "more" },
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
                      "folder", "file", "ollama", "claude", "openai", "spark",
                      "agent", "clock", "user", "project", "more"];
  /* Seventeen glyphs do not fit in a four-by-four grid, and the one past the
     end does not fail — it WRAPS, and a node quietly wears another kind's
     icon. So the grid grew.

     It must stay a POWER OF TWO on a side. The atlas is mipmapped with
     LINEAR_MIPMAP_LINEAR, and in WebGL 1 generateMipmap on a non-power-of-two
     texture fails: the texture is left incomplete and samples BLACK. Five
     columns of 128 is 640, which is not one — and the result was not "the
     three new icons are wrong", it was every icon on the screen disappearing
     at once. Eight columns of 64 is 512, exactly the size the four-by-four
     grid was, so this costs no memory and leaves room for forty-seven more
     glyphs. */
  const ATLAS_COLS = 8, ATLAS_CELL = 64;

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
      case "agent":
        ctx.beginPath(); ctx.arc(0, -R * 0.45, R * 0.36, 0, 6.283); ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(-R * 0.72, R * 0.95);
        ctx.quadraticCurveTo(-R * 0.72, R * 0.1, 0, R * 0.1);
        ctx.quadraticCurveTo(R * 0.72, R * 0.1, R * 0.72, R * 0.95);
        ctx.stroke();
        break;
      case "user":
        // Head and shoulders. The one glyph here that is a person, so it stays
        // plainly a person rather than anything cleverer.
        ctx.beginPath(); ctx.arc(0, -R * 0.42, R * 0.38, 0, 6.283); ctx.stroke();
        ctx.beginPath();
        ctx.arc(0, R * 0.92, R * 0.72, Math.PI * 1.08, Math.PI * 1.92);
        ctx.stroke();
        break;
      case "project":
        // A board with a line of work on it.
        ctx.beginPath();
        ctx.moveTo(-R * 0.86, -R * 0.7); ctx.lineTo(R * 0.86, -R * 0.7);
        ctx.lineTo(R * 0.86, R * 0.86); ctx.lineTo(-R * 0.86, R * 0.86);
        ctx.closePath(); ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(-R * 0.48, -R * 0.18); ctx.lineTo(R * 0.48, -R * 0.18);
        ctx.moveTo(-R * 0.48, R * 0.3); ctx.lineTo(R * 0.12, R * 0.3);
        ctx.stroke();
        break;
      case "more":
        // An ellipsis in a ring: the universal "there is more of this".
        ctx.beginPath(); ctx.arc(0, 0, R * 0.94, 0, 6.283); ctx.stroke();
        for (const dx of [-R * 0.42, 0, R * 0.42]) {
          ctx.beginPath(); ctx.arc(dx, 0, R * 0.13, 0, 6.283); ctx.fill();
        }
        break;
      case "clock":
        ctx.beginPath(); ctx.arc(0, 0, R * 0.92, 0, 6.283); ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(0, -R * 0.52); ctx.lineTo(0, 0); ctx.lineTo(R * 0.44, R * 0.2);
        ctx.stroke();
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
      /* ── the arrangements ──────────────────────────────────────────────
         Drawn by the same hand as the node glyphs so the picker belongs to
         the field rather than sitting beside it. These are never baked into
         the atlas — ICON_ORDER does not list them — they are only ever asked
         for one at a time by iconDataURL(). */
      case "lay-orbit":
        ctx.save(); ctx.rotate(-0.42);
        ctx.beginPath(); ctx.ellipse(0, 0, R, R * 0.42, 0, 0, 6.283); ctx.stroke();
        ctx.restore();
        ctx.beginPath(); ctx.arc(0, 0, R * 0.28, 0, 6.283); ctx.fill();
        break;
      case "lay-rings":
        for (let i = 3; i >= 1; i--) {
          ctx.beginPath(); ctx.arc(0, 0, R * (i / 3), 0, 6.283); ctx.stroke();
        }
        ctx.beginPath(); ctx.arc(0, 0, R * 0.16, 0, 6.283); ctx.fill();
        break;
      case "lay-circle":
        ctx.beginPath(); ctx.arc(0, 0, R * 0.92, 0, 6.283); ctx.stroke();
        for (let i = 0; i < 3; i++) {
          const a1 = i * 2.1, a2 = a1 + 2.6;
          ctx.beginPath();
          ctx.moveTo(Math.cos(a1) * R * 0.92, Math.sin(a1) * R * 0.92);
          ctx.lineTo(Math.cos(a2) * R * 0.92, Math.sin(a2) * R * 0.92);
          ctx.stroke();
        }
        break;
      case "lay-areas":
        for (const [cx, cy] of [[-0.48, -0.42], [0.5, -0.36], [0.02, 0.52]]) {
          for (const [dx, dy] of [[0, 0], [0.26, 0.2], [-0.24, 0.22]]) {
            ctx.beginPath();
            ctx.arc((cx + dx * 0.9) * R, (cy + dy * 0.9) * R, R * 0.15, 0, 6.283);
            ctx.fill();
          }
        }
        break;
      case "lay-force": {
        const pts = [[0, -0.66], [-0.7, 0.22], [0.7, 0.2], [-0.12, 0.72]];
        ctx.beginPath();
        ctx.moveTo(pts[0][0] * R, pts[0][1] * R);
        ctx.lineTo(pts[1][0] * R, pts[1][1] * R);
        ctx.lineTo(pts[3][0] * R, pts[3][1] * R);
        ctx.lineTo(pts[2][0] * R, pts[2][1] * R);
        ctx.lineTo(pts[0][0] * R, pts[0][1] * R);
        ctx.stroke();
        for (const [x, y] of pts) {
          ctx.beginPath(); ctx.arc(x * R, y * R, R * 0.19, 0, 6.283); ctx.fill();
        }
        break;
      }
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
    if (node.kind === "model" || node.kind === "machine") {
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
      /* Optional: if this one fails to compile the field simply has no wash
         behind it, which is a lesser thing than no field at all. */
      this.nprog = program(gl, NVS, NFS);
      if (this.nprog) {
        this.na = { xy: gl.getAttribLocation(this.nprog, 'a_xy') };
        this.nu = {
          res:   gl.getUniformLocation(this.nprog, 'u_res'),
          count: gl.getUniformLocation(this.nprog, 'u_count'),
          pos:   gl.getUniformLocation(this.nprog, 'u_pos'),
          col:   gl.getUniformLocation(this.nprog, 'u_col'),
          gain:  gl.getUniformLocation(this.nprog, 'u_gain'),
        };
        this.bQuad = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, this.bQuad);
        gl.bufferData(gl.ARRAY_BUFFER,
          new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
      }
      /* Optional in the same way the wash is: no guides is a lesser loss
         than no field. */
      this.gprog = program(gl, GVS, GFS);
      if (this.gprog) {
        this.ga = { unit: gl.getAttribLocation(this.gprog, 'a_unit') };
        this.gu = {
          mvp:   gl.getUniformLocation(this.gprog, 'u_mvp'),
          scale: gl.getUniformLocation(this.gprog, 'u_scale'),
          col:   gl.getUniformLocation(this.gprog, 'u_col'),
          alpha: gl.getUniformLocation(this.gprog, 'u_alpha'),
        };
        const unit = new Float32Array(RING_SEGMENTS * 2);
        for (let i = 0; i < RING_SEGMENTS; i++) {
          const a = (i / RING_SEGMENTS) * Math.PI * 2;
          unit[i * 2] = Math.cos(a); unit[i * 2 + 1] = Math.sin(a);
        }
        this.bRing = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, this.bRing);
        gl.bufferData(gl.ARRAY_BUFFER, unit, gl.STATIC_DRAW);
      }
      this.fprog = program(gl, FVS, FFS);
      if (this.fprog) {
        this.fa = { xy: gl.getAttribLocation(this.fprog, 'a_xy') };
        this.fu = {
          res:   gl.getUniformLocation(this.fprog, 'u_res'),
          tint:  gl.getUniformLocation(this.fprog, 'u_tint'),
          focus: gl.getUniformLocation(this.fprog, 'u_focus'),
          pan:   gl.getUniformLocation(this.fprog, 'u_pan'),
          emit:  gl.getUniformLocation(this.fprog, 'u_emit'),
          gain:  gl.getUniformLocation(this.fprog, 'u_gain'),
        };
      }
      /* Where the projector is, across the screen. The page sets this each
         frame: she is the source, so it follows her into the corner. */
      this.emitter = 0.5;
      this.rings = [];        // {kind, rx, ry, col} while the rings layout is on
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
        time: gl.getUniformLocation(this.prog, 'u_time'),
        pass: gl.getUniformLocation(this.prog, 'u_pass'),
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
      // Which arrangement of the same nodes is on screen, so the page can
      // offer the others without knowing how any of them works.
      this.layout = 'orbit';
      // What the camera is looking AT. It used to be the origin and only the
      // origin, which is fine while the centre is the subject and useless the
      // moment a particular group is. Eased, like dist, so travelling to a
      // cluster is a move the eye can follow.
      this.look = [0, 0, 0];
      this.lookWant = [0, 0, 0];
      this.spot = null;       // category currently singled out, or null
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
          home, radial: home.slice(), target: home.slice(), pos: home.slice(),
          size: sizeFor(nd),
          color: st.color,
          icon: Math.max(0, ICON_ORDER.indexOf(st.icon)),
          shade: nd.kind === 'file' ? 0.55 : 0.85,
          phase: (i * 1.7) % 6.283,
        };
      });
      this.center = {
        id: (graph && graph.focus) || 'root',
        label: (graph && graph.trail && graph.trail.length
                ? graph.trail[graph.trail.length - 1].label : 'SUNI'),
        pos: [0, 0, 0],
      };
      this.spot = null;
      this.look = [0, 0, 0];
      this.lookWant = [0, 0, 0];
      // A new level has to arrive in whatever arrangement is on screen, or
      // opening a folder silently throws the chosen layout away.
      if (this.layout && this.layout !== 'orbit') this._applyLayout();
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

    /* Pull the field apart into groups that sit clear of each other.

       Grouped by category, because that is the question the separated view
       answers: how much of this is documents, how much is tooling, what else is
       there. A category with more members than a cluster can hold legibly is
       split into several — thirty folders in one ball is the crowding this mode
       exists to undo.

       Only the destinations change; update() walks the nodes there, so the
       toggle reads as the field rearranging rather than as a new screen. */
    /* ── layouts ───────────────────────────────────────────────────────
       Five ways of arranging the same nodes. Each writes only `target`, and
       update() walks every node there — so switching is a MOVE, not a new
       screen. That is the whole reason to have more than one: watching a
       category gather itself out of the cloud is a different kind of
       understanding from seeing it already gathered.

       Named in one place so the page can offer them without knowing how any of
       them works. */
    setLayout(name) {
      const known = ['orbit', 'rings', 'circle', 'areas', 'force'];
      this.layout = known.indexOf(name) >= 0 ? name : 'orbit';
      this.clustered = (this.layout === 'areas');
      this._applyLayout();
      // Stand where the arrangement fits. Without this, rings laid out a wide
      // outer ring and left the camera where the last layout had put it, so
      // half the field sat off the edges of the screen.
      this.distWant = (this.layout === 'orbit') ? this.distWant : this._fitDistance();
      this.wantFocus = true;
    }

    static layouts() {
      return [
        { id: 'orbit',  icon: 'lay-orbit',  label: 'orbit',  hint: 'a slow turn, everything at once' },
        { id: 'rings',  icon: 'lay-rings',  label: 'rings',  hint: 'concentric by kind, the subject at the middle' },
        { id: 'circle', icon: 'lay-circle', label: 'circle', hint: 'one ring, every link across the middle' },
        { id: 'areas',  icon: 'lay-areas',  label: 'areas',  hint: 'one cluster per category' },
        { id: 'force',  icon: 'lay-force',  label: 'force',  hint: 'pulled together by what connects' },
      ];
    }

    _applyLayout() {
      switch (this.layout) {
        case 'rings':  return this._ringsLayout();
        case 'circle': return this._circleLayout();
        case 'areas':  return this._clusterLayout();
        case 'force':  return this._forceLayout();
        default:
          // Orbit. Cleared HERE rather than in each layout, because this is
          // the branch a layout is left FOR, and guides left behind draw
          // circles through an arrangement that has no rings in it.
          this.rings = [];
          for (const nd of this.nodes) nd.target = nd.radial.slice();
          this._extentX = this._extentY = 0;
          return;
      }
    }

    /* RINGS — concentric by kind, the subject in the middle. Reads as layers:
       what she IS nearest, what she has merely indexed furthest out. */
    _ringsLayout() {
      const byKind = new Map();
      for (const nd of this.nodes) {
        if (!byKind.has(nd.kind)) byKind.set(nd.kind, []);
        byKind.get(nd.kind).push(nd);
      }
      const ORDER = ['machine', 'model', 'agent', 'schedule', 'tool', 'skill',
                     'channel', 'folder', 'file'];
      const rank = k => { const i = ORDER.indexOf(k); return i < 0 ? 99 : i; };
      const kinds = [...byKind.keys()].sort((a, b) => rank(a) - rank(b));
      let r = 3.0;
      this._extentX = this._extentY = 0;
      /* Recorded here rather than recomputed for the drawing: two places
         deciding where a ring is, is how a guide ends up beside its own
         nodes instead of through them. */
      this.rings = [];
      for (const k of kinds) {
        const members = byKind.get(k);
        // The circumference has to hold them side by side, or a ring is a smear.
        // Rings tighten as they go out: nine kinds at a constant gap pushed
        // the outermost to a radius the camera could only fit by making every
        // node a dot. The gap shrinks, the ring still has to hold its members
        // side by side, and the two together stay compact.
        const need = (members.length * NODE_W * 1.15) / (2 * Math.PI);
        r = Math.max(r + NODE_W * (2.0 - 0.9 * Math.min(1, r / 14)), need);
        members.forEach((nd, i) => {
          const a = (i / members.length) * Math.PI * 2 + r * 0.11;
          nd.target = [Math.cos(a) * r, Math.sin(a) * r * 0.62, Math.sin(i * 1.3) * 0.9];
        });
        this._extentX = Math.max(this._extentX, r + NODE_W);
        this._extentY = Math.max(this._extentY, r * 0.62 + NODE_W);
        this.rings.push({ kind: k, rx: r, ry: r * 0.62,
                          col: (KIND_STYLE[k] || KIND_STYLE.file).color,
                          count: members.length });
      }
    }

    /* CIRCLE — everything on one ring, every link crossing the middle. Shows
       how connected a level is: a dense bundle of chords says it hangs
       together, a sparse one says it does not. */
    _circleLayout() {
      this.rings = [];   // guides belong to the rings layout alone
      const sorted = this.nodes.slice().sort((a, b) =>
        String(a.kind).localeCompare(String(b.kind))
        || String(a.label).localeCompare(String(b.label)));
      const n = Math.max(1, sorted.length);
      const r = Math.max(4.2, (n * NODE_W * 1.2) / (2 * Math.PI));
      sorted.forEach((nd, i) => {
        const a = (i / n) * Math.PI * 2;
        nd.target = [Math.cos(a) * r, Math.sin(a) * r * 0.66, 0];
      });
      this._extentX = r + NODE_W;
      this._extentY = r * 0.66 + NODE_W;
    }

    /* FORCE — repulsion with link attraction, seeded from the radial spread.
       A FIXED number of iterations, run once: a field that never settles is a
       field you cannot read, and one that jiggles for ever spends the frame
       budget on nothing. */
    _forceLayout() {
      this.rings = [];   // guides belong to the rings layout alone
      const nodes = this.nodes, n = nodes.length;
      const pos = nodes.map(nd => nd.radial.slice());
      const links = (this._links || []).filter(lk => lk[0] !== null && lk[1] !== null);
      const REST = NODE_W * 2.6;
      for (let iter = 0; iter < 60; iter++) {
        for (let i = 0; i < n; i++) {
          for (let j = i + 1; j < n; j++) {
            let dx = pos[j][0] - pos[i][0], dy = pos[j][1] - pos[i][1],
                dz = pos[j][2] - pos[i][2];
            let d2 = dx * dx + dy * dy + dz * dz;
            if (d2 < 1e-4) { dx = 0.01; d2 = 1e-4; }
            const d = Math.sqrt(d2);
            if (d >= REST) continue;
            const push = (REST - d) / d * 0.22;
            pos[i][0] -= dx * push; pos[i][1] -= dy * push; pos[i][2] -= dz * push;
            pos[j][0] += dx * push; pos[j][1] += dy * push; pos[j][2] += dz * push;
          }
        }
        for (const lk of links) {
          const a = lk[0], b = lk[1];
          const dx = pos[b][0] - pos[a][0], dy = pos[b][1] - pos[a][1],
                dz = pos[b][2] - pos[a][2], k = 0.012;
          pos[a][0] += dx * k; pos[a][1] += dy * k; pos[a][2] += dz * k;
          pos[b][0] -= dx * k; pos[b][1] -= dy * k; pos[b][2] -= dz * k;
        }
        for (let i = 0; i < n; i++) {
          pos[i][0] *= 0.998; pos[i][1] *= 0.998; pos[i][2] *= 0.998;
        }
      }
      this._extentX = this._extentY = 0;
      nodes.forEach((nd, i) => {
        nd.target = pos[i];
        this._extentX = Math.max(this._extentX, Math.abs(pos[i][0]) + NODE_W);
        this._extentY = Math.max(this._extentY, Math.abs(pos[i][1]) + NODE_W);
      });
    }

    _clusterLayout() {
      this.rings = [];   // guides belong to the rings layout alone
      /* Group centres on a DISC facing the viewer, not on a sphere around
         them. A sphere puts half the groups edge-on or behind the middle, and
         at the distance needed to fit it they are small, scattered and
         crossed by every spoke - which is a picture of a scatter, not of
         groups. On a disc every group is the same distance from the eye and
         all of them are in frame.

         A golden-angle spiral rather than a ring, so eight groups do not sit
         in a circle with a hole in the middle. */
      const groups = new Map();
      // One group per category. Splitting a big category into alphabetical
      // bands is for a level where EVERYTHING is one kind - a hundred folders
      // in one ball is the crowding this mode exists to undo - so the
      // threshold is high enough that ordinary categories stay whole. Two
      // clusters of tools is not "grouped by category".
      const PER = 40;
      const totals = new Map();
      for (const nd of this.nodes) totals.set(nd.kind, (totals.get(nd.kind) || 0) + 1);
      const bigKinds = new Set([...totals.keys()].filter(k => totals.get(k) > PER));
      this._bigKinds = bigKinds;
      for (const nd of this.nodes) {
        let key = nd.kind;
        if (bigKinds.has(nd.kind)) {
          const initial = String(nd.label || '?').trim().charAt(0).toUpperCase();
          key = `${nd.kind} ${/[A-Z]/.test(initial)
            ? String.fromCharCode(65 + Math.floor((initial.charCodeAt(0) - 65) / 4) * 4)
            : '#'}`;
        }
        const arr = groups.get(key) || [];
        arr.push(nd);
        groups.set(key, arr);
      }

      const keys = [...groups.keys()].filter(k => groups.get(k).length);
      const gCount = Math.max(1, keys.length);
      /* Deliberately small in world units. The camera pulls back to fit
         whatever this spans, so a roomy layout is not a roomier picture - it
         is the same picture further away, with the nodes too small to carry an
         icon or a name. Tight groups, close camera. */
      const radOf = (n) => 0.6 + Math.sqrt(n) * 0.30;
      let maxRad = 0;
      for (const k of keys) maxRad = Math.max(maxRad, radOf(groups.get(k).length));

      /* Spaced from the size of the biggest ball, so the gaps stay gaps
         whether there are three groups of two or eight of thirty. */
      const R = gCount === 1 ? 0 : maxRad * 2.2 * Math.sqrt(gCount);
      this._extentX = 0;
      this._extentY = 0;

      keys.forEach((key, gi) => {
        const t = gCount === 1 ? 0 : Math.sqrt((gi + 0.5) / gCount);
        const a = gi * 2.399963;                       // golden angle
        const centre = [Math.cos(a) * R * t, Math.sin(a) * R * t * 0.78, 0];
        const members = groups.get(key);
        const rad = radOf(members.length);
        this._extentX = Math.max(this._extentX, Math.abs(centre[0]) + rad);
        this._extentY = Math.max(this._extentY, Math.abs(centre[1]) + rad);
        members.forEach((nd, i) => {
          const p = fibSphere(i, Math.max(2, members.length));
          /* Flattened on z: a group should read as a disc of its own from the
             front, not as a ball whose far half is dimmed to nothing. */
          nd.target = [centre[0] + p[0] * rad,
                       centre[1] + p[1] * rad,
                       centre[2] + p[2] * rad * 0.45];
        });
      });
    }

    setCluster(on) {
      // Kept because the page has a button wired to it and "grouped" is the
      // arrangement people reach for. It is the 'areas' layout by a shorter
      // name, and it goes through the registry so one thing owns the state.
      this.clustered = !!on;
      this.layout = this.clustered ? 'areas' : 'orbit';
      if (this.clustered) this._clusterLayout();
      else for (const nd of this.nodes) nd.target = nd.radial.slice();
      this.wantFocus = true;
      /* Framed from what the layout actually spans, in each axis separately.
         A single "extent" understates the vertical reach of a wide disc, and
         the group that falls off the bottom edge is the one the viewer was
         looking for. 0.42 is tan(fov/2) for the 0.80 projection the Face uses;
         the horizontal check assumes the narrowest window worth designing for
         rather than reading the canvas, which this module never sees. */
      this.distWant = this.clustered ? this._fitDistance()
                                     : Math.max(9, Math.min(38, this.distWant * 0.62));
    }

    /* How far back to stand so the arrangement fits, from what it ACTUALLY
       spans in each axis. One combined "extent" understates the vertical reach
       of a wide disc, and the group that falls off the bottom edge is the one
       the viewer was looking for. 0.38/0.52 are the visible half-angles for
       the 0.80 projection the Face uses, taking the narrowest window worth
       designing for rather than reading a canvas this module never sees. */
    _fitDistance() {
      const needY = (this._extentY || 10) / 0.38;
      const needX = (this._extentX || 10) / 0.52;
      return Math.max(9, Math.min(46, Math.max(needY, needX)));
    }

    /* Send the camera to one category and dim the rest.

       This is what clicking a legend row means, and what SUNI means when she
       says "here are the machines": not a filter that hides everything else —
       the field should stay recognisable — but a spotlight. The others stay
       drawn, dark, in place, so it is visible that they are still there.

       Passing null puts everything back up. */
    spotlight(kind) {
      this.spot = kind || null;
      this._uploadShade();
      if (!kind) { this.lookWant = [0, 0, 0]; return; }
      const members = this.nodes.filter(nd => nd.kind === kind);
      if (!members.length) {
        this.spot = null;
        this._uploadShade();
        return 0;
      }
      const c = [0, 0, 0];
      for (const nd of members) for (let i = 0; i < 3; i++) c[i] += nd.home[i] / members.length;
      let far = 0;
      for (const nd of members) {
        far = Math.max(far, Math.hypot(nd.home[0] - c[0], nd.home[1] - c[1], nd.home[2] - c[2]));
      }
      this.lookWant = c;
      this.distWant = Math.max(6, Math.min(52, far * 2.6 + 6));
      this.wantFocus = true;
      return members.length;
    }

    /* Shade is a per-node attribute the shaders already dim by, so singling a
       category out is a buffer rewrite rather than a second draw path. */
    _uploadShade() {
      if (!this.ok || !this.nodes.length) return;
      const gl = this.gl;
      const shade = new Float32Array(this.count);
      shade[0] = this.spot ? 0.3 : 1;
      this.nodes.forEach((nd, i) => {
        const base = nd.shade;
        shade[i + 1] = this.spot ? (nd.kind === this.spot ? 1 : base * 0.22) : base;
      });
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bShade);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, shade);
    }

    /* ── camera ─────────────────────────────────────────────────────────── */
    orbit(dx, dy) {
      this.yaw += dx * 0.006;
      this.pitch = Math.max(-1.35, Math.min(1.35, this.pitch + dy * 0.006));
      this.wantFocus = true;
    }
    zoom(delta) {
      this.distWant = Math.max(4.5, Math.min(60, this.distWant * (1 + delta * 0.0012)));
      this.wantFocus = true;
    }
    setFocus(on) { this.wantFocus = !!on; }

    update(dt) {
      if (!this.ok) return;
      this._t += dt;
      const want = this.wantFocus ? 1 : 0;
      this.focus += (want - this.focus) * Math.min(1, 3.2 * dt);
      this.dist += (this.distWant - this.dist) * Math.min(1, 5 * dt);
      const lk = Math.min(1, 3.4 * dt);
      for (let i = 0; i < 3; i++) this.look[i] += (this.lookWant[i] - this.look[i]) * lk;
      // Ambient drift only while nobody is holding it: a view that keeps
      // rotating under the cursor is a view you cannot read.
      if (this.focus < 0.02) this.yaw += this.spin * dt;

      // Nodes breathe around their home so the field reads as alive.
      const amp = 0.16 + 0.10 * this.focus;
      let moved = false;
      const k = Math.min(1, 2.6 * dt);
      for (const nd of this.nodes) {
        if (nd.target) {
          nd.home[0] += (nd.target[0] - nd.home[0]) * k;
          nd.home[1] += (nd.target[1] - nd.home[1]) * k;
          nd.home[2] += (nd.target[2] - nd.home[2]) * k;
        }
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
      return mul(trans(0, 0, -this.dist),
                 mul(rotX(this.pitch),
                     mul(rotY(this.yaw),
                         trans(-this.look[0], -this.look[1], -this.look[2]))));
    }

    /* ── drawing ────────────────────────────────────────────────────────── */
    draw(proj, tint, canvasH) {
      if (!this.ok || !this.nodes.length) return;
      const gl = this.gl;
      this._mvp = mul(proj, this.view());

      gl.enable(gl.DEPTH_TEST);
      gl.enable(gl.BLEND);
      this._drawFloor(tint);
      this._drawNebula();
      this._drawRings();
      // Edges stay additive — they are light, and light adds. The nodes below
      // switch to ordinary blending and write depth, because a sphere that
      // accumulates with the one behind it is not a sphere.
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      gl.depthMask(false);

      // Solid spheres carry at a lower alpha than glows did, and the ambient
      // view should still sit behind her rather than in front.
      const alpha = 0.55 + 0.45 * this.focus;

      gl.useProgram(this.lprog);
      gl.uniformMatrix4fv(this.lu.mvp, false, this._mvp);
      gl.uniform3f(this.lu.tint, tint[0], tint[1], tint[2]);
      gl.uniform1f(this.lu.focus, this.focus);
      // Grouped, every spoke crosses the gaps the grouping just opened, so the
      // wires drop back and the clusters are what is left to look at.
      /* Grouped, every line runs from the middle out to a group, so the
         bundle crossing the gaps is busier than it is useful — but at 0.07 it
         had gone from quiet to absent, and the field stopped looking connected
         to anything. Quiet, not gone. */
      gl.uniform1f(this.lu.alpha, alpha * (this.clustered ? 0.34 : 0.8));
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

      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.depthMask(true);
      gl.useProgram(this.prog);
      gl.uniformMatrix4fv(this.u.mvp, false, this._mvp);
      // Pixels per unit of a_size at one unit of depth. Sized so a folder
      // node reads as a point of light at the default distance: too large and
      // additive blending turns the whole field into a white sheet, which is
      // exactly what the first render did.
      // Bigger in focus, where the field is the thing being looked at rather
      // than the backdrop — and big enough there for a glyph to survive.
      this._scalePx = (canvasH || 900) * (0.013 + 0.012 * this.focus);
      gl.uniform1f(this.u.scale, this._scalePx);
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
      gl.uniform1f(this.u.time, this._t);
      gl.uniform1f(this.u.pass, 0.0);          // the solid balls
      gl.drawArrays(gl.POINTS, 0, this.count);
      /* ...then their light, added over the top. Depth is still TESTED, so a
         node behind her head stays behind it, but not WRITTEN, so one halo
         does not carve a hole out of the next. */
      gl.uniform1f(this.u.pass, 1.0);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      gl.depthMask(false);
      gl.drawArrays(gl.POINTS, 0, this.count);
      gl.depthMask(true);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

      gl.depthMask(true);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    }

    /* The orbits themselves. Drawn after the wash and before the links, so a
       guide sits behind the network rather than across it. */
    _drawRings() {
      if (!this.gprog || !this.rings.length) return;
      const gl = this.gl;
      gl.useProgram(this.gprog);
      gl.uniformMatrix4fv(this.gu.mvp, false, this._mvp);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bRing);
      gl.enableVertexAttribArray(this.ga.unit);
      gl.vertexAttribPointer(this.ga.unit, 2, gl.FLOAT, false, 0, 0);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      gl.depthMask(false);
      for (const ring of this.rings) {
        gl.uniform2f(this.gu.scale, ring.rx, ring.ry);
        gl.uniform3f(this.gu.col, ring.col[0], ring.col[1], ring.col[2]);
        // Barely there in ambient: a guide is for someone who is reading the
        // field, and behind her head nobody is.
        gl.uniform1f(this.gu.alpha, 0.05 + 0.20 * this.focus);
        gl.drawArrays(gl.LINE_LOOP, 0, RING_SEGMENTS);
      }
      gl.depthMask(true);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    }

    /* Where to write each ring's name: the highest point of that ring on
       screen, so the words sit along the top the way the reference does.

       Sampled rather than solved. The ring is an ellipse in the layout plane
       seen through a camera that turns, so which point is topmost changes as
       the viewer drags; sixteen samples find it closely enough and cost
       nothing next to being wrong whenever the field is not square-on. */
    ringLabels(w, h, opts) {
      if (!this.rings.length) return [];
      const o = opts || {};
      const skips = !o.exclude ? []
        : (Array.isArray(o.exclude) ? o.exclude : [o.exclude]);
      const blocked = (x, y) => skips.some(
        s => x > s.x0 && x < s.x1 && y > s.y0 && y < s.y1);
      const out = [];
      for (const ring of this.rings) {
        let best = null;
        for (let i = 0; i < 16; i++) {
          const a = (i / 16) * Math.PI * 2;
          const p = this.project([Math.cos(a) * ring.rx, Math.sin(a) * ring.ry, 0], w, h);
          if (!p) continue;
          if (!best || p.y < best.y) best = p;
        }
        if (!best || best.x < 0 || best.x > w || best.y < 0 || best.y > h) continue;
        if (blocked(best.x, best.y)) continue;
        out.push({
          kind: ring.kind,
          label: (KIND_STYLE[ring.kind] || {}).label || ring.kind,
          count: ring.count,
          x: best.x, y: best.y,
          color: ring.col,
        });
      }
      return out;
    }

    /* Where each category sits on screen, how far it is spread, and how much
       of a claim that is. Computed once a frame and used twice: the wash behind
       a cluster and the hub that names it must agree, or the name floats beside
       its own glow.

       In the caller's coordinates, y downward, so it matches project(); the
       nebula flips y itself because GL counts from the bottom. */
    _clusters(w, h) {
      const groups = new Map();
      for (const nd of this.nodes) {
        if (this.spot && nd.kind !== this.spot) continue;   // singled out: one wash
        const p = this.project(nd.pos, w, h);
        if (!p) continue;
        let g = groups.get(nd.kind);
        if (!g) { g = { kind: nd.kind, pts: [], col: nd.color }; groups.set(nd.kind, g); }
        g.pts.push(p.x, p.y);
      }

      const out = [];
      for (const g of groups.values()) {
        const n = g.pts.length / 2;
        if (!n) continue;
        let cx = 0, cy = 0;
        for (let i = 0; i < n; i++) { cx += g.pts[i * 2]; cy += g.pts[i * 2 + 1]; }
        cx /= n; cy /= n;
        let spread = 0;
        for (let i = 0; i < n; i++) {
          spread += Math.hypot(g.pts[i * 2] - cx, g.pts[i * 2 + 1] - cy);
        }
        spread /= n;

        /* A wash says "this category lives HERE", so it has to earn the right
           to say it. In the areas layout each kind occupies its own patch and
           the claim is true. In rings a kind is smeared all the way round the
           circle: its centroid is the middle of the screen, which is where
           every OTHER kind's centroid is too, and ten washes stacked on one
           point turn the field white — which is exactly what happened the
           first time this was drawn.

           So weight by how concentrated the kind actually is. Tight patch,
           full wash; smeared round a ring, almost nothing. */
        let tight = Math.max(0, 1 - spread / (h * 0.30));
        /* A lone node has a spread of zero, which is perfect concentration by
           that measure and got it the brightest wash on the screen — a single
           drive lit up harder than the twenty files inside it. One node is not
           a cluster, so a category has to have some members before its wash
           carries full weight. */
        tight *= 0.30 + 0.70 * Math.min(1, n / 5);
        out.push({ kind: g.kind, col: g.col, n, x: cx, y: cy, spread, tight });
      }
      return out;
    }

    /* The reference names its clusters — a letter, a word, a count — and that
       one thing is why a screen of six hundred dots is readable at a glance
       while ours needed every node labelled to say anything at all. A hub says
       what a whole mass IS, which is the question a viewer has first.

       A hub is a louder claim than a wash, so the bar is higher: a smear that
       earns a faint haze earns no name. That is the point — in an arrangement
       where the categories are interleaved, there is no "here" to name, and
       saying so by staying silent is more honest than a label in the middle of
       everything. */
    hubs(w, h, opts) {
      const o = opts || {};
      /* Rings already name every category, on the ring that holds it. A
         cluster marker saying MODEL beside a ring labelled MODEL is the same
         word twice for the same thing. */
      if (this.rings.length) return [];
      const min = o.min || 0.34;
      const skips = !o.exclude ? []
        : (Array.isArray(o.exclude) ? o.exclude : [o.exclude]);
      const blocked = (x, y) => skips.some(
        s => x > s.x0 && x < s.x1 && y > s.y0 && y < s.y1);
      return this._clusters(w, h)
        .filter(c => c.tight >= min && c.n >= 2)
        .map(c => {
          /* Not at the centroid: that is the middle of the mass, so the marker
             lands on the very nodes it is describing. The reference puts each
             one at the OUTER edge of its cluster, and that is what makes a
             screen of six hundred dots readable — the name is beside the thing
             rather than on top of it.

             Outward means away from the middle of the screen, which is where
             everything else is. A cluster sitting on the centre has no outward,
             so it goes up. */
          const dx = c.x - w / 2, dy = c.y - h / 2;
          const len = Math.hypot(dx, dy);
          const off = Math.min(c.spread * 0.95 + 16, h * 0.14);
          const ux = len > 1 ? dx / len : 0, uy = len > 1 ? dy / len : -1;
          return {
            kind: c.kind,
            label: (KIND_STYLE[c.kind] || {}).label || c.kind,
            count: c.n,
            x: c.x + ux * off,
            y: c.y + uy * off,
            color: c.col,
          };
        })
        .filter(hb => hb.x > 0 && hb.y > 0 && hb.x < w && hb.y < h)
        .filter(hb => !blocked(hb.x, hb.y))
        .sort((a, b) => b.count - a.count)
        .slice(0, o.max || 8);
    }

    /* Under everything, and painted before everything. */
    _drawFloor(tint) {
      if (!this.fprog || !this.bQuad) return;
      const gl = this.gl;
      gl.useProgram(this.fprog);
      gl.uniform2f(this.fu.res, gl.drawingBufferWidth, gl.drawingBufferHeight);
      gl.uniform3f(this.fu.tint, tint[0], tint[1], tint[2]);
      gl.uniform1f(this.fu.focus, this.focus);
      // Panned by the yaw, so turning the field drags the floor under it.
      gl.uniform1f(this.fu.pan, this.yaw * 0.85);
      gl.uniform1f(this.fu.emit, this.emitter);
      // The same curve the wash and the guides follow: a hint behind her head,
      // present once the field has the floor. It is another light source, and
      // the last one that arrived at full strength washed out the nodes.
      gl.uniform1f(this.fu.gain, 0.45 + 0.55 * this.focus);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bQuad);
      gl.enableVertexAttribArray(this.fa.xy);
      gl.vertexAttribPointer(this.fa.xy, 2, gl.FLOAT, false, 0, 0);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      gl.depthMask(false);
      gl.disable(gl.DEPTH_TEST);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      gl.enable(gl.DEPTH_TEST);
      gl.depthMask(true);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    }

    /* One wash per category, centred on where that category actually IS.
       Recomputed every frame from the node positions, which is what makes it
       follow a layout change instead of being painted once and left behind.
       A dozen categories over a few hundred nodes is a rounding error next to
       the draw itself. */
    _drawNebula() {
      if (!this.nprog || !this.nodes.length) return;
      const gl = this.gl;
      const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight;

      const pos = [], col = [];
      for (const c of this._clusters(w, h)) {
        if (c.tight < 0.02 || pos.length / 3 >= NEB_MAX) continue;
        // A tight cluster gets a tight glow; a scattered one a broad haze.
        pos.push(c.x, h - c.y, Math.max(h * 0.06, c.spread * 1.5 + h * 0.03));
        col.push(c.col[0] * c.tight, c.col[1] * c.tight, c.col[2] * c.tight);
      }
      if (!pos.length) return;

      gl.useProgram(this.nprog);
      gl.uniform2f(this.nu.res, w, h);
      gl.uniform1i(this.nu.count, pos.length / 3);
      gl.uniform3fv(this.nu.pos, new Float32Array(pos));
      gl.uniform3fv(this.nu.col, new Float32Array(col));
      // Quiet behind her head, present when the field has the floor. A wash
      // that competes with her face is a wash nobody asked for.
      gl.uniform1f(this.nu.gain, 0.06 + 0.54 * this.focus);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bQuad);
      gl.enableVertexAttribArray(this.na.xy);
      gl.vertexAttribPointer(this.na.xy, 2, gl.FLOAT, false, 0, 0);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      gl.depthMask(false);
      gl.disable(gl.DEPTH_TEST);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      gl.enable(gl.DEPTH_TEST);
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

    /* Was that click on the thing in the middle?

       The centre is not in this.nodes - it is drawn from index 0 of the buffers
       and has no entry to pick - so pick() can never return it, and a click
       there did nothing at all. It is the most obvious target on the screen and
       it is the current level itself, which makes it the natural way back up.

       Generous by a few pixels: it is a small sphere and the thing it does is
       harmless. */
    pickCentre(x, y, w, h) {
      if (!this.ok) return false;
      const p = this.project([0, 0, 0], w, h);
      if (!p || p.w <= 0.05) return false;
      const px = (this._scalePx || (h * 0.013)) * SIZE_BY_KIND.root / Math.max(p.w, 0.001);
      const r = Math.max(14, px * 0.75);
      return (x - p.x) * (x - p.x) + (y - p.y) * (y - p.y) <= r * r;
    }

    /* Which nodes have earned a name on screen.

       Zooming in makes points bigger, so keying off a node's drawn size is the
       whole rule: far out, only the few large hubs are named; closer in, more
       appear, and inside a folder everything is named because everything is big.
       Sorted by size so the important ones win, and anything landing on top of
       an already-placed label is dropped — overlapping names are less readable
       than none, and the one underneath is usually the smaller node anyway. */
    /* How far under its node a name sits. It lives here, not in the page, so
       that the exclusion test below and the span the caller positions agree —
       two copies of this number is how a label ends up somewhere the module
       believes it cleared. Callers use the `ty` it returns. */
    labels(w, h, opts) {
      const o = opts || {};
      const minPx = o.minPx || 15;
      const max = o.max || 18;
      const spacing = o.spacing || 62;
      /* Her head is drawn over this field, so a name landing on it is a name
         nobody can read. The caller passes where she currently is — centred, or
         shrunk into the corner — and those candidates are dropped rather than
         nudged: moving a label away from its node makes it point at nothing. */
      /* More than one thing on the page has to be kept clear: her head, and
         the chat dock at the bottom. A node sitting just above the dock put
         its name straight through the AI-disclosure line — caught on a real
         screen, not here. */
      const skips = !o.exclude ? []
        : (Array.isArray(o.exclude) ? o.exclude : [o.exclude]);
      const blocked = (x, y) => skips.some(
        s => x > s.x0 && x < s.x1 && y > s.y0 && y < s.y1);
      const scale = (this._scalePx || (h * 0.013));
      const out = [];
      const cand = [];
      for (const nd of this.nodes) {
        const p = this.project(nd.pos, w, h);
        if (!p || p.w <= 0.05) continue;
        if (p.x < 0 || p.y < 0 || p.x > w || p.y > h) continue;
        const px = scale * nd.size / Math.max(p.w, 0.001);
        if (px < minPx) continue;
        /* The name is drawn BELOW its node, so testing the node alone let a
           label fall into a zone the node itself had cleared. Both points —
           and both ENDS of the text, because the span is centred on its node
           and a long name reaches a good way either side of the point being
           tested. "stable-diffusion" ran straight into a cluster marker whose
           box its centre had cleared by a comfortable margin. */
        const ty = p.y + labelDrop(px);
        const half = textHalfWidth(nd.label, o.charW);
        if (blocked(p.x, p.y) || blocked(p.x, ty)
            || blocked(p.x - half, ty) || blocked(p.x + half, ty)) continue;
        cand.push({ node: nd, x: p.x, y: p.y, ty, px, w: p.w });
      }
      /* The centre is the one node that was never named, which left the thing
         everything on screen hangs off — her, at the top level; whatever you
         opened, below it — as an anonymous dot. It goes in first and out of
         turn: it is the subject, so it outranks whatever is merely big. */
      const cp = this.project([0, 0, 0], w, h);
      cand.sort((a, b) => b.px - a.px || a.w - b.w);
      if (cp && cp.w > 0.05 && cp.x > 0 && cp.y > 0 && cp.x < w && cp.y < h) {
        const cpx = scale * SIZE_BY_KIND.root / Math.max(cp.w, 0.001);
        const cty = cp.y + labelDrop(cpx);
        const chalf = textHalfWidth(this.center.label, o.charW);
        if (!blocked(cp.x, cp.y) && !blocked(cp.x, cty)
            && !blocked(cp.x - chalf, cty) && !blocked(cp.x + chalf, cty)) {
          cand.unshift({
            node: { id: this.center.id, label: this.center.label, kind: 'root' },
            x: cp.x, y: cp.y, ty: cty, px: cpx, w: cp.w,
          });
        }
      }
      for (const c of cand) {
        if (out.length >= max) break;
        let clear = true;
        for (const placed of out) {
          if (Math.abs(placed.x - c.x) < spacing && Math.abs(placed.y - c.y) < 16) {
            clear = false;
            break;
          }
        }
        if (clear) out.push(c);
      }
      return out;
    }
  }

  global.NetGraph = NetGraph;
})(window);
