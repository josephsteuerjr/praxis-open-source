/* Praxis — живой фон: густая краска в воде.
 *
 * Тёмная тема — масло в тёмной воде (светящийся пигмент поверх чернил).
 * Светлая  — акварель по влажной бумаге (пигмент вычитается из бумаги).
 *
 * Внутри — настоящий решатель Навье–Стокса на WebGL2 (адвекция скорости и краски,
 * вихревое усиление, проекция давления Якоби). Потоки реально сталкиваются и
 * перемешиваются, а не имитируют движение.
 *
 * Публичный контракт сохранён ровно как был у прежнего поля частиц:
 *   new AmbientField(canvas) · setState({theme,activeRuns,attention,bodyOnline,phase})
 *   · start() · stop() · destroy()
 *
 * Предохранители те же, что и раньше: батарея, число ядер, prefers-reduced-motion,
 * замер fps с понижением 60→30, остановка на скрытой вкладке, кап devicePixelRatio.
 * Если WebGL2 недоступен — мягкий 2D-фолбэк на тех же красках.
 */

const TAU = Math.PI * 2;
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

function cssRgb(name, fallback) {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const parts = raw.split(",").map((part) => Number(part.trim()));
  return parts.length === 3 && parts.every(Number.isFinite) ? parts : fallback;
}
const unit = (rgb) => [rgb[0] / 255, rgb[1] / 255, rgb[2] / 255];
const scale = (c, k) => [c[0] * k, c[1] * k, c[2] * k];

function seeded(index, salt = 0) {
  const value = Math.sin((index + 1) * 91.173 + salt * 17.719) * 43758.5453;
  return value - Math.floor(value);
}

/* ---------------------------------------------------------------- шейдеры */

const VERT = `#version 300 es
precision highp float;
layout(location=0) in vec2 aPos;
out vec2 vUv; out vec2 vL; out vec2 vR; out vec2 vT; out vec2 vB;
uniform vec2 texel;
void main(){
  vUv = aPos*0.5+0.5;
  vL = vUv-vec2(texel.x,0.); vR = vUv+vec2(texel.x,0.);
  vT = vUv+vec2(0.,texel.y); vB = vUv-vec2(0.,texel.y);
  gl_Position = vec4(aPos,0.,1.);
}`;

const F_CLEAR = `#version 300 es
precision highp float; in vec2 vUv; uniform sampler2D uTex; uniform float value; out vec4 o;
void main(){ o = value*texture(uTex,vUv); }`;

const F_SPLAT = `#version 300 es
precision highp float; in vec2 vUv;
uniform sampler2D uTarget; uniform vec3 color; uniform vec2 point;
uniform float radius; uniform float aspect; out vec4 o;
void main(){
  vec2 p = vUv-point; p.x *= aspect;
  float a = exp(-dot(p,p)/radius);
  o = vec4(texture(uTarget,vUv).rgb + a*color, 1.);
}`;

const F_ADVECT = `#version 300 es
precision highp float; in vec2 vUv;
uniform sampler2D uVel; uniform sampler2D uSrc; uniform vec2 texel;
uniform float dt; uniform float diss; out vec4 o;
void main(){
  vec2 v = texture(uVel,vUv).xy;
  o = diss*texture(uSrc, vUv - dt*v*texel);
}`;

const F_DIV = `#version 300 es
precision highp float; in vec2 vUv,vL,vR,vT,vB; uniform sampler2D uVel; out vec4 o;
void main(){
  float l=texture(uVel,vL).x, r=texture(uVel,vR).x;
  float t=texture(uVel,vT).y, b=texture(uVel,vB).y;
  o = vec4(0.5*(r-l+t-b),0.,0.,1.);
}`;

const F_CURL = `#version 300 es
precision highp float; in vec2 vL,vR,vT,vB; uniform sampler2D uVel; out vec4 o;
void main(){
  float l=texture(uVel,vL).y, r=texture(uVel,vR).y;
  float t=texture(uVel,vT).x, b=texture(uVel,vB).x;
  o = vec4(0.5*(r-l-(t-b)),0.,0.,1.);
}`;

const F_VORT = `#version 300 es
precision highp float; in vec2 vUv,vL,vR,vT,vB;
uniform sampler2D uVel; uniform sampler2D uCurl; uniform float curl; uniform float dt; out vec4 o;
void main(){
  float l=texture(uCurl,vL).x, r=texture(uCurl,vR).x;
  float t=texture(uCurl,vT).x, b=texture(uCurl,vB).x, c=texture(uCurl,vUv).x;
  vec2 f = 0.5*vec2(abs(t)-abs(b), abs(r)-abs(l));
  f /= (length(f)+1e-4); f *= curl*c; f.y *= -1.;
  vec2 v = texture(uVel,vUv).xy + dt*f;
  o = vec4(clamp(v,-1000.,1000.),0.,1.);
}`;

const F_PRESS = `#version 300 es
precision highp float; in vec2 vUv,vL,vR,vT,vB;
uniform sampler2D uP; uniform sampler2D uDiv; out vec4 o;
void main(){
  float l=texture(uP,vL).x, r=texture(uP,vR).x;
  float t=texture(uP,vT).x, b=texture(uP,vB).x, d=texture(uDiv,vUv).x;
  o = vec4((l+r+t+b-d)*0.25,0.,0.,1.);
}`;

const F_GRAD = `#version 300 es
precision highp float; in vec2 vUv,vL,vR,vT,vB;
uniform sampler2D uP; uniform sampler2D uVel; out vec4 o;
void main(){
  float l=texture(uP,vL).x, r=texture(uP,vR).x;
  float t=texture(uP,vT).x, b=texture(uP,vB).x;
  o = vec4(texture(uVel,vUv).xy - vec2(r-l,t-b), 0., 1.);
}`;

const F_DISPLAY = `#version 300 es
precision highp float; in vec2 vUv,vL,vR,vT,vB;
uniform sampler2D uTex; uniform float light; uniform vec3 paper; uniform float bloom; out vec4 o;
void main(){
  vec3 c = texture(uTex,vUv).rgb;
  vec3 bl = (texture(uTex,vL).rgb+texture(uTex,vR).rgb+texture(uTex,vT).rgb+texture(uTex,vB).rgb)*0.25;
  c += bl*bloom;
  vec3 col;
  if(light > 0.5){
    /* акварель: пигмент вычитается из бумаги */
    vec3 dens = 1.0 - exp(-c*1.6);
    col = paper - dens*paper*vec3(0.95,0.92,0.88) + dens*vec3(0.04);
  } else {
    /* масло: светящийся пигмент поверх чернил */
    col = paper + (vec3(1.0)-exp(-c*1.25));
  }
  vec2 q = vUv-0.5;
  col *= mix(0.80, 1.0, smoothstep(0.95, 0.26, length(q)));
  col = pow(clamp(col,0.,1.), vec3(0.92));
  o = vec4(col,1.);
}`;

/* ---------------------------------------------------------------- поле */

export class AmbientField {
  constructor(canvas) {
    if (!(canvas instanceof HTMLCanvasElement)) {
      throw new TypeError("AmbientField needs a canvas");
    }
    this.canvas = canvas;
    this.width = 0;
    this.height = 0;
    this.dpr = 1;
    this.running = false;
    this.frame = 0;
    this.lastFrame = 0;
    this.lastDraw = 0;
    this.sampleStarted = 0;
    this.sampleFrames = 0;
    this.targetFps = 60;
    this.lowPower = false;
    this.autoTimer = 0;
    this.seeded = false;
    this.reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.state = { activeRuns: 0, bodyOnline: false, attention: 0, phase: "idle", theme: "" };
    this.colors = {};
    this.pointer = { x: .5, y: .5, px: .5, py: .5, down: false };

    this._tick = this._tick.bind(this);
    this._resize = this._resize.bind(this);
    this._visibility = this._visibility.bind(this);
    this._pointer = this._pointer.bind(this);
    this._pointerDown = this._pointerDown.bind(this);
    this._pointerUp = this._pointerUp.bind(this);

    this.media = matchMedia("(prefers-reduced-motion: reduce)");
    this._onMedia = (event) => {
      this.reducedMotion = event.matches;
      this.draw(performance.now(), 0);
    };
    this.media.addEventListener?.("change", this._onMedia);

    window.addEventListener("resize", this._resize, { passive: true });
    window.addEventListener("pointermove", this._pointer, { passive: true });
    window.addEventListener("pointerdown", this._pointerDown, { passive: true });
    window.addEventListener("pointerup", this._pointerUp, { passive: true });
    document.addEventListener("visibilitychange", this._visibility);

    this._inspectBattery();

    this.mode = "none";
    try {
      this.gl = canvas.getContext("webgl2", {
        alpha: false, antialias: false, depth: false, stencil: false,
        premultipliedAlpha: false, preserveDrawingBuffer: false, powerPreference: "low-power",
      });
      if (this.gl && this._initGL()) this.mode = "fluid";
    } catch (_) {
      this.mode = "none";
    }
    if (this.mode === "fluid") {
      this._onContextLost = (event) => { event.preventDefault(); this._demote("context lost"); };
      canvas.addEventListener("webglcontextlost", this._onContextLost, false);
    }
    if (this.mode !== "fluid") {
      this.ctx = canvas.getContext("2d", { alpha: true });
      if (this.ctx) this.mode = "soft2d";
    }

    this._readColors();
    this._resize();
  }

  /* ------------------------------------------------------------ батарея */

  async _inspectBattery() {
    if (typeof navigator.getBattery !== "function") return;
    try {
      const battery = await navigator.getBattery();
      const update = () => {
        const constrained = !battery.charging && battery.level < .28;
        if (constrained !== this.lowPower) {
          this.lowPower = constrained;
          this.targetFps = constrained ? 30 : 60;
          this._resize();
        }
      };
      update();
      battery.addEventListener("levelchange", update);
      battery.addEventListener("chargingchange", update);
    } catch (_) {
      /* статус батареи опционален и намеренно не является зависимостью */
    }
  }

  /* ------------------------------------------------------------ GL-каркас */

  _compile(type, src) {
    const gl = this.gl;
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      throw new Error(gl.getShaderInfoLog(s) || "shader compile failed");
    }
    return s;
  }

  _program(fragSrc) {
    const gl = this.gl;
    const p = gl.createProgram();
    gl.attachShader(p, this._compile(gl.VERTEX_SHADER, VERT));
    gl.attachShader(p, this._compile(gl.FRAGMENT_SHADER, fragSrc));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      throw new Error(gl.getProgramInfoLog(p) || "link failed");
    }
    const u = {};
    const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < n; i += 1) {
      const info = gl.getActiveUniform(p, i);
      u[info.name] = gl.getUniformLocation(p, info.name);
    }
    return { p, u };
  }

  _initGL() {
    const gl = this.gl;
    if (!gl.getExtension("EXT_color_buffer_float") && !gl.getExtension("EXT_color_buffer_half_float")) {
      return false; // без плавающих таргетов честнее уйти в 2D, чем показывать кашу
    }
    gl.getExtension("OES_texture_float_linear");
    try {
      this.P = {
        clear: this._program(F_CLEAR),
        splat: this._program(F_SPLAT),
        advect: this._program(F_ADVECT),
        div: this._program(F_DIV),
        curl: this._program(F_CURL),
        vort: this._program(F_VORT),
        press: this._program(F_PRESS),
        grad: this._program(F_GRAD),
        display: this._program(F_DISPLAY),
      };
    } catch (_) {
      return false;
    }
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.disable(gl.BLEND);
    gl.disable(gl.DEPTH_TEST);
    this.texType = gl.HALF_FLOAT; // RGBA16F/RG16F/R16F + HALF_FLOAT — ядро WebGL2
    this._targets = [];
    return true;
  }

  /* Мобильные браузеры шлют resize на каждое появление адресной строки. Без явного
   * удаления старых текстур и FBO это течёт до потери контекста, поэтому освобождаем
   * их сами и вовсе не пересоздаём, если размеры симуляции не изменились. */
  _releaseTargets() {
    const gl = this.gl;
    if (!gl) return;
    for (const r of this._targets || []) {
      gl.deleteTexture(r.t);
      gl.deleteFramebuffer(r.f);
    }
    this._targets = [];
  }

  _fbo(w, h, internal, format, filter) {
    const gl = this.gl;
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, internal, w, h, 0, format, this.texType, null);
    const f = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, f);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const target = { t, f, w, h, texel: [1 / w, 1 / h] };
    (this._targets || (this._targets = [])).push(target);
    return target;
  }

  _double(w, h, internal, format, filter) {
    let a = this._fbo(w, h, internal, format, filter);
    let b = this._fbo(w, h, internal, format, filter);
    return {
      get read() { return a; },
      get write() { return b; },
      swap() { const t = a; a = b; b = t; },
      w, h, texel: [1 / w, 1 / h],
    };
  }

  _initTargets() {
    const gl = this.gl;
    const cores = Number(navigator.hardwareConcurrency || 4);
    const weak = cores <= 4 || this.lowPower;
    const simRes = weak ? 96 : 128;
    const dyeRes = weak ? 320 : 480;
    const ar = Math.max(0.2, this.width / Math.max(1, this.height));
    const dim = (res) => (ar >= 1
      ? { w: Math.round(res * ar), h: res }
      : { w: res, h: Math.round(res / ar) });
    const s = dim(simRes);
    const d = dim(dyeRes);
    const prev = this._dims;
    if (prev && prev.sw === s.w && prev.sh === s.h && prev.dw === d.w && prev.dh === d.h) {
      return; // размеры симуляции те же — сохраняем уже написанную краску, ничего не пересоздаём
    }
    this._releaseTargets();
    this._dims = { sw: s.w, sh: s.h, dw: d.w, dh: d.h };
    this.velocity = this._double(s.w, s.h, gl.RG16F, gl.RG, gl.LINEAR);
    this.dye = this._double(d.w, d.h, gl.RGBA16F, gl.RGBA, gl.LINEAR);
    this.divergence = this._fbo(s.w, s.h, gl.R16F, gl.RED, gl.NEAREST);
    this.curlT = this._fbo(s.w, s.h, gl.R16F, gl.RED, gl.NEAREST);
    this.pressure = this._double(s.w, s.h, gl.R16F, gl.RED, gl.NEAREST);
    this.pressureIters = weak ? 14 : 22;
    this.seeded = false;
  }

  _use(pr) { this.gl.useProgram(pr.p); return pr; }

  _bind(loc, tex, unitIndex) {
    const gl = this.gl;
    gl.activeTexture(gl.TEXTURE0 + unitIndex);
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.uniform1i(loc, unitIndex);
  }

  _blit(target) {
    const gl = this.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, target ? target.f : null);
    gl.viewport(0, 0, target ? target.w : this.canvas.width, target ? target.h : this.canvas.height);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  /* ------------------------------------------------------------ размер */

  _resize() {
    const rect = this.canvas.getBoundingClientRect();
    this.width = Math.max(1, Math.round(rect.width || window.innerWidth));
    this.height = Math.max(1, Math.round(rect.height || window.innerHeight));
    const cap = this.lowPower ? 1.1 : 1.5;
    this.dpr = clamp(window.devicePixelRatio || 1, 1, cap);
    const pw = Math.round(this.width * this.dpr);
    const ph = Math.round(this.height * this.dpr);
    if (this.canvas.width !== pw || this.canvas.height !== ph) {
      this.canvas.width = pw;
      this.canvas.height = ph;
      if (this.mode === "soft2d" && this.ctx) this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    }
    if (this.mode === "fluid") {
      try { this._initTargets(); } catch (err) { this._demote("targets failed"); }
    }
    if (this.mode === "soft2d") this._seed2D();
    this._readColors();
    if (!this.running || this.reducedMotion) this.draw(performance.now(), 0);
  }

  /* ------------------------------------------------------------ краски */

  _readColors() {
    this.colors = {
      violet: unit(cssRgb("--violet-rgb", [157, 124, 255])),
      cyan: unit(cssRgb("--cyan-rgb", [85, 216, 232])),
      blue: unit(cssRgb("--blue-rgb", [111, 165, 255])),
      gold: unit(cssRgb("--gold-rgb", [241, 201, 121])),
      green: unit(cssRgb("--green-rgb", [98, 215, 160])),
      red: unit(cssRgb("--red-rgb", [255, 122, 145])),
      white: [0.86, 0.92, 1.0],
    };
  }

  _isLight() {
    if (this.state.theme === "light") return true;
    if (this.state.theme === "dark") return false;
    const attr = document.documentElement.getAttribute("data-theme");
    if (attr) return attr === "light";
    return matchMedia("(prefers-color-scheme: light)").matches;
  }

  /* Густая бело-голубая основа — всегда. Всплески зависят от того, чем она занята. */
  _base() {
    const c = this.colors;
    return [c.white, c.blue, scale(c.cyan, .9), [0.62, 0.78, 1.0]];
  }

  _pops() {
    const c = this.colors;
    if (!this.state.bodyOnline || this.state.phase === "offline") return [c.violet, c.red, c.blue];
    if (Number(this.state.attention) > 0) return [c.gold, c.violet, c.cyan, c.red];
    return [c.violet, c.cyan, c.green, c.gold];
  }

  /* ------------------------------------------------------------ мазки */

  _splat(x, y, dx, dy, color, radius) {
    if (this.mode !== "fluid") return;
    const gl = this.gl;
    const s = this._use(this.P.splat);
    gl.uniform1f(s.u.aspect, this.canvas.width / Math.max(1, this.canvas.height));
    gl.uniform2f(s.u.point, x, y);

    this._bind(s.u.uTarget, this.velocity.read.t, 0);
    gl.uniform3f(s.u.color, dx, dy, 0);
    gl.uniform1f(s.u.radius, radius * .55);
    this._blit(this.velocity.write); this.velocity.swap();

    this._bind(s.u.uTarget, this.dye.read.t, 0);
    gl.uniform3f(s.u.color, color[0], color[1], color[2]);
    gl.uniform1f(s.u.radius, radius);
    this._blit(this.dye.write); this.dye.swap();
  }

  _seedFluid() {
    if (this.seeded) return;
    this.seeded = true;
    const base = this._base();
    const pops = this._pops();
    for (let i = 0; i < 11; i += 1) {
      const pop = i % 4 === 0;
      const color = scale(pop ? pops[i % pops.length] : base[i % base.length], pop ? .70 : .85);
      const ang = seeded(i, 9) * TAU;
      this._splat(seeded(i, 1), seeded(i, 2), Math.cos(ang) * 3200, Math.sin(ang) * 3200,
        color, 0.00040);
    }
  }

  /* ------------------------------------------------------------ 2D-фолбэк */

  _seed2D() {
    const count = this.lowPower ? 5 : 7;
    this.blobs = Array.from({ length: count }, (_, i) => ({
      x: seeded(i, 1), y: seeded(i, 2),
      r: .28 + seeded(i, 3) * .34,
      vx: (seeded(i, 4) - .5) * .00006,
      vy: (seeded(i, 5) - .5) * .00006,
      group: i,
    }));
  }

  _draw2D() {
    const ctx = this.ctx;
    if (!ctx) return;
    const light = this._isLight();
    const base = this._base();
    const pops = this._pops();
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.fillStyle = light ? "#efeae0" : "#05070c";
    ctx.fillRect(0, 0, this.width, this.height);
    ctx.globalCompositeOperation = light ? "multiply" : "lighter";
    for (const b of this.blobs || []) {
      const c = b.group % 3 === 0 ? pops[b.group % pops.length] : base[b.group % base.length];
      const rgb = `${Math.round(c[0] * 255)}, ${Math.round(c[1] * 255)}, ${Math.round(c[2] * 255)}`;
      const R = b.r * Math.max(this.width, this.height);
      const g = ctx.createRadialGradient(b.x * this.width, b.y * this.height, 0,
        b.x * this.width, b.y * this.height, R);
      g.addColorStop(0, `rgba(${rgb}, ${light ? .34 : .40})`);
      g.addColorStop(1, `rgba(${rgb}, 0)`);
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(b.x * this.width, b.y * this.height, R, 0, TAU);
      ctx.fill();
    }
    ctx.globalCompositeOperation = "source-over";
  }

  _step2D(delta) {
    const speed = 1 + clamp(Number(this.state.activeRuns) || 0, 0, 8) * .12;
    for (const b of this.blobs || []) {
      b.x += b.vx * delta * speed;
      b.y += b.vy * delta * speed;
      if (b.x < -.4) b.x = 1.4; if (b.x > 1.4) b.x = -.4;
      if (b.y < -.4) b.y = 1.4; if (b.y > 1.4) b.y = -.4;
    }
  }

  /* ------------------------------------------------------------ решатель */

  _step(dt) {
    const gl = this.gl;
    let pr;
    gl.disable(gl.BLEND);

    pr = this._use(this.P.curl);
    gl.uniform2f(pr.u.texel, this.velocity.texel[0], this.velocity.texel[1]);
    this._bind(pr.u.uVel, this.velocity.read.t, 0);
    this._blit(this.curlT);

    pr = this._use(this.P.vort);
    gl.uniform2f(pr.u.texel, this.velocity.texel[0], this.velocity.texel[1]);
    this._bind(pr.u.uVel, this.velocity.read.t, 0);
    this._bind(pr.u.uCurl, this.curlT.t, 1);
    gl.uniform1f(pr.u.curl, 24.0);
    gl.uniform1f(pr.u.dt, dt);
    this._blit(this.velocity.write); this.velocity.swap();

    pr = this._use(this.P.div);
    gl.uniform2f(pr.u.texel, this.velocity.texel[0], this.velocity.texel[1]);
    this._bind(pr.u.uVel, this.velocity.read.t, 0);
    this._blit(this.divergence);

    pr = this._use(this.P.clear);
    this._bind(pr.u.uTex, this.pressure.read.t, 0);
    gl.uniform1f(pr.u.value, 0.8);
    this._blit(this.pressure.write); this.pressure.swap();

    pr = this._use(this.P.press);
    gl.uniform2f(pr.u.texel, this.velocity.texel[0], this.velocity.texel[1]);
    for (let i = 0; i < this.pressureIters; i += 1) {
      this._bind(pr.u.uP, this.pressure.read.t, 0);
      this._bind(pr.u.uDiv, this.divergence.t, 1);
      this._blit(this.pressure.write); this.pressure.swap();
    }

    pr = this._use(this.P.grad);
    gl.uniform2f(pr.u.texel, this.velocity.texel[0], this.velocity.texel[1]);
    this._bind(pr.u.uP, this.pressure.read.t, 0);
    this._bind(pr.u.uVel, this.velocity.read.t, 1);
    this._blit(this.velocity.write); this.velocity.swap();

    pr = this._use(this.P.advect);
    gl.uniform2f(pr.u.texel, this.velocity.texel[0], this.velocity.texel[1]);
    gl.uniform1f(pr.u.dt, dt);
    gl.uniform1f(pr.u.diss, 0.985);
    this._bind(pr.u.uVel, this.velocity.read.t, 0);
    this._bind(pr.u.uSrc, this.velocity.read.t, 0);
    this._blit(this.velocity.write); this.velocity.swap();

    gl.uniform1f(pr.u.diss, 0.992);
    this._bind(pr.u.uVel, this.velocity.read.t, 0);
    this._bind(pr.u.uSrc, this.dye.read.t, 1);
    this._blit(this.dye.write); this.dye.swap();
  }

  _renderFluid() {
    const gl = this.gl;
    const pr = this._use(this.P.display);
    gl.uniform2f(pr.u.texel, this.dye.texel[0], this.dye.texel[1]);
    const light = this._isLight();
    const paper = light ? [0.937, 0.917, 0.878] : [0.020, 0.028, 0.047];
    gl.uniform3f(pr.u.paper, paper[0], paper[1], paper[2]);
    gl.uniform1f(pr.u.light, light ? 1 : 0);
    gl.uniform1f(pr.u.bloom, this.lowPower ? 0.22 : 0.34);
    this._bind(pr.u.uTex, this.dye.read.t, 0);
    this._blit(null);
  }

  /* ------------------------------------------------------------ кадр */

  /* Уходим в 2D мягко: любой отказ GPU не должен оставить владельца перед чёрным экраном. */
  _demote(reason) {
    if (this.mode !== "fluid") return;
    try { this._releaseTargets(); } catch (_) { /* контекст мог уже умереть */ }
    // Канвас, уже отданный WebGL, 2D-контекст не вернёт никогда — поэтому подменяем
    // сам элемент на чистый близнец, иначе страховка была бы только на бумаге.
    try {
      const fresh = document.createElement("canvas");
      fresh.id = this.canvas.id;
      fresh.className = this.canvas.className;
      fresh.setAttribute("aria-hidden", "true");
      if (this._onContextLost) {
        this.canvas.removeEventListener("webglcontextlost", this._onContextLost);
        this._onContextLost = null;
      }
      if (this.canvas.parentNode) this.canvas.parentNode.replaceChild(fresh, this.canvas);
      this.canvas = fresh;
      this.canvas.width = Math.round(this.width * this.dpr);
      this.canvas.height = Math.round(this.height * this.dpr);
    } catch (_) { /* если подменить не вышло — просто останемся без фона */ }
    this.mode = "soft2d";
    this.gl = null;
    this.ctx = this.canvas.getContext("2d", { alpha: true });
    if (!this.ctx) { this.mode = "none"; return; }
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    this._seed2D();
    this._draw2D();
    if (typeof console !== "undefined") console.warn("ambient: 2D fallback —", reason);
  }

  draw(now, delta) {
    if (!this.width || !this.height) return;
    if (this.mode === "fluid") {
      this._seedFluid();
      if (delta > 0) this._step(clamp(delta / 1000, 0.0001, 0.0166));
      this._renderFluid();
      if (!this._glChecked) {
        // Один честный опрос после первого полного кадра: если решатель не поехал,
        // деградируем сразу, а не рисуем битую картинку весь сеанс.
        this._glChecked = true;
        const err = this.gl.getError();
        if (err !== this.gl.NO_ERROR) { this._demote("gl error 0x" + err.toString(16)); return; }
      }
    } else if (this.mode === "soft2d") {
      if (delta > 0) this._step2D(delta);
      this._draw2D();
    }
    this.lastDraw = now;
  }

  _autoSplat(dt) {
    const active = clamp(Number(this.state.activeRuns) || 0, 0, 8);
    const attention = clamp(Number(this.state.attention) || 0, 0, 8);
    this.autoTimer += dt;
    const every = clamp(0.85 - active * .07 - attention * .03, 0.28, 0.9);
    if (this.autoTimer < every) return;
    this.autoTimer = 0;
    const pop = Math.random() < (0.22 + attention * .05);
    const palette = pop ? this._pops() : this._base();
    const color = scale(palette[(Math.random() * palette.length) | 0], pop ? .62 : .48);
    const ang = Math.random() * TAU;
    const force = 2200 + active * 260;
    this._splat(Math.random(), Math.random() * .58 + .21,
      Math.cos(ang) * force, Math.sin(ang) * force, color, 0.00034);
  }

  _tick(now) {
    if (!this.running) return;
    this.frame = requestAnimationFrame(this._tick);
    if (this.reducedMotion) return;

    const interval = 1000 / this.targetFps;
    if (now - this.lastDraw < interval - 1.5) return;
    const delta = clamp(now - this.lastFrame, 0, 50);
    this.lastFrame = now;
    this.draw(now, delta);
    if (this.mode === "fluid") this._autoSplat(delta / 1000);

    this.sampleFrames += 1;
    const sampleTime = now - this.sampleStarted;
    if (sampleTime >= 4000) {
      const measured = this.sampleFrames * 1000 / sampleTime;
      if (measured < 47 && this.targetFps === 60) this.targetFps = 30;
      if (measured > 55 && this.targetFps === 30 && !this.lowPower) this.targetFps = 60;
      this.sampleFrames = 0;
      this.sampleStarted = now;
    }
  }

  /* ------------------------------------------------------------ ввод */

  _uv(event) {
    const rect = this.canvas.getBoundingClientRect();
    return {
      x: clamp((event.clientX - rect.left) / Math.max(1, rect.width), 0, 1),
      y: clamp(1 - (event.clientY - rect.top) / Math.max(1, rect.height), 0, 1),
    };
  }

  _pointerDown(event) {
    const u = this._uv(event);
    this.pointer.x = this.pointer.px = u.x;
    this.pointer.y = this.pointer.py = u.y;
    this.pointer.down = true;
  }

  _pointerUp() { this.pointer.down = false; }

  _pointer(event) {
    if (this.reducedMotion || this.mode !== "fluid") return;
    const u = this._uv(event);
    this.pointer.px = this.pointer.x;
    this.pointer.py = this.pointer.y;
    this.pointer.x = u.x;
    this.pointer.y = u.y;
    const dx = this.pointer.x - this.pointer.px;
    const dy = this.pointer.y - this.pointer.py;
    const speed = Math.hypot(dx, dy);
    if (speed < 0.0009) return;
    const palette = this.pointer.down ? this._pops() : this._base();
    const color = scale(palette[(Math.random() * palette.length) | 0], this.pointer.down ? .70 : .34);
    this._splat(this.pointer.x, this.pointer.y,
      dx * window.innerWidth * 5.5, dy * window.innerHeight * 5.5, color, 0.00017);
  }

  _visibility() {
    if (document.hidden) this.stop();
    else this.start();
  }

  /* ------------------------------------------------------------ контракт */

  setState(next = {}) {
    this.state = { ...this.state, ...next };
    this._readColors();
    if (!this.running || this.reducedMotion) this.draw(performance.now(), 0);
  }

  start() {
    if (this.running || document.hidden) return;
    this.running = true;
    this.lastFrame = performance.now();
    this.lastDraw = 0;
    this.sampleStarted = this.lastFrame;
    this.sampleFrames = 0;
    this.frame = requestAnimationFrame(this._tick);
  }

  stop() {
    this.running = false;
    cancelAnimationFrame(this.frame);
    this.frame = 0;
  }

  destroy() {
    this.stop();
    window.removeEventListener("resize", this._resize);
    window.removeEventListener("pointermove", this._pointer);
    window.removeEventListener("pointerdown", this._pointerDown);
    window.removeEventListener("pointerup", this._pointerUp);
    document.removeEventListener("visibilitychange", this._visibility);
    this.media?.removeEventListener?.("change", this._onMedia);
    if (this._onContextLost) this.canvas.removeEventListener("webglcontextlost", this._onContextLost);
    if (this.mode === "fluid") this._releaseTargets();
  }
}
