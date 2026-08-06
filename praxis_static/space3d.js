/*
 * space3d.js — «пространство»: настоящий 3D-рендер графа на WebGL2.
 *
 * Самодостаточный ES-модуль: НИ ОДНОГО импорта, никакого three.js, никакого CDN.
 * CSP `script-src 'self'` держится тем, что весь GLSL лежит инлайном здесь же.
 *
 * Что это заменяет: 2D-«Созвездие» из memory_views.js. Соглашения того модуля
 * сохранены осознанно — детерминированный шум (`seeded`), чтение палитры из
 * CSS-переменных, потолки узлов/связей ради телефона, `pan-y`-совместимые жесты
 * (панорама одним пальцем только на горизонтали), пауза на `document.hidden`,
 * честный `prefers-reduced-motion` (раскладка досчитывается разом, кадры не идут).
 * Соглашения WebGL взяты из ambient.js: замер fps с понижением 60→30, кэп DPR,
 * учёт батареи, аккуратный destroy().
 *
 * Модуль НИЧЕГО не решает про состояния «пусто» и «сбой» — их рисует хост
 * (stateBox в memory_views.js). Мы только возвращаем из setGraph() честную
 * сводку, по которой хост эти состояния и показывает.
 *
 * Путь импорта у хоста ОТНОСИТЕЛЬНЫЙ ("./space3d.js"): под скоупом /px
 * переписывается только app.js, абсолютный путь к каталогу статики там сломался бы.
 *
 *   import { Space3D } from "./space3d.js";
 *   const space = new Space3D(canvas, { onSelect, onLabels });
 *   const stats = space.setGraph(payload);   // обе формы полезной нагрузки
 *   space.start();
 */

/* ── потолки телефона ──────────────────────────────────────────────── */
const MAX_NODES = 400;
const MAX_LINKS = 1200;
const DPR_CAP = 1.75;
const DPR_CAP_LOW = 1.25;
const LABEL_CAP = 12;
const LABEL_MS = 110;           // подписи пересчитываем не чаще — DOM не резиновый

/* ── силовая раскладка (3D) ────────────────────────────────────────── */
const CUTOFF = 260;             // радиус отталкивания (мировые единицы)
const REP = 5200;               // сила отталкивания
const SPRING = 0.032;           // жёсткость пружины связи
const CENTER = 0.0026;          // стягивание к центру
const DAMP = 0.84;
const MAX_V = 30;
const PRESTEPS = 60;            // прогон до первого кадра — чтобы не «взрыв»
const ALPHA_DECAY = 0.985;
const ALPHA_MIN = 0.02;
// prefers-reduced-motion: досчитываем разом. 200 шагов → alpha ≈ 0.985^200 ≈ 0.049,
// визуально улёгшаяся раскладка; верхняя граница обязательна, иначе цикл держит
// UI-поток столько, сколько скажет alpha.
const STATIC_STEPS = 200;

/* ── камера ────────────────────────────────────────────────────────── */
const FOV = 0.86;               // ~49° по вертикали
const NEAR = 1;
const PITCH_LIMIT = 1.35;
const IDLE_SPIN = 0.055;        // рад/с — медленный дрейф покоя
const IDLE_RESUME_MS = 7000;    // сколько тишины после жеста до возврата дрейфа
const DOLLY_MIN = 0.22;         // множители к «вписанной» дистанции
const DOLLY_MAX = 3.2;

const TAU = Math.PI * 2;

/* ═══════════════════════════ мелочи ═══════════════════════════════ */

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function intOf(value, fallback = 0) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function textOf(value, max = 200) {
  const raw = String(value ?? "").replace(/\0/g, "").trim();
  return raw.length > max ? `${raw.slice(0, max - 1)}…` : raw;
}

/* детерминированный «шум» — раскладка воспроизводима между запусками */
function seeded(index, salt = 0) {
  const value = Math.sin((index + 1) * 91.173 + salt * 17.719) * 43758.5453;
  return value - Math.floor(value);
}

/* ключ ячейки сетки отталкивания: число, а не строка (в 2D-версии строковый
   ключ давал ~10^6 аллокаций за прогон; в 3D соседей 27, а не 9 — тем более) */
function cellKey(gx, gy, gz) {
  return (Math.imul(gx, 73856093) ^ Math.imul(gy, 19349663) ^ Math.imul(gz, 83492791)) | 0;
}

/* устойчивый хеш строки — чтобы незнакомый kind получал свой цвет, а не серый */
function hashString(value) {
  let hash = 2166136261;
  const text = String(value || "");
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0);
}

function readPalette() {
  const style = getComputedStyle(document.documentElement);
  const rgb = (name, fallback) => {
    const parts = style.getPropertyValue(name).trim().split(",").map((part) => Number(part.trim()));
    return parts.length === 3 && parts.every(Number.isFinite)
      ? [parts[0] / 255, parts[1] / 255, parts[2] / 255]
      : fallback;
  };
  const light = (document.documentElement.dataset.theme || "dark") === "light";
  return {
    violet: rgb("--violet-rgb", [157 / 255, 124 / 255, 1]),
    cyan: rgb("--cyan-rgb", [85 / 255, 216 / 255, 232 / 255]),
    gold: rgb("--gold-rgb", [241 / 255, 201 / 255, 121 / 255]),
    blue: rgb("--blue-rgb", [111 / 255, 165 / 255, 1]),
    green: rgb("--green-rgb", [98 / 255, 215 / 255, 160 / 255]),
    red: rgb("--red-rgb", [1, 122 / 255, 145 / 255]),
    light,
  };
}

/* Цвет узла по его виду. Память — прямая карта (люди/темы), код — по смыслу
   узла; всё незнакомое раскладывается хешем по тем же пяти краскам, чтобы
   новый вид узла появился как новый цвет, а не как безымянный серый. */
const KIND_COLOR = {
  people: "violet",
  person: "violet",
  topic: "cyan",
  module: "blue",
  file: "blue",
  package: "gold",
  dir: "gold",
  directory: "gold",
  entry: "violet",
  root: "violet",
  test: "green",
  tests: "green",
  lib: "cyan",
  vendor: "green",
  // номенклатура /code/graph: skill|soul|test|module|doc|web|other.
  // Явно закрепляем только две несущие — остальное честно раскладывает хеш.
  skill: "gold",
  soul: "violet",
};
const COLOR_WHEEL = ["violet", "cyan", "blue", "gold", "green"];

function colorKeyFor(kind) {
  const key = String(kind || "").toLowerCase();
  if (KIND_COLOR[key]) return KIND_COLOR[key];
  return COLOR_WHEEL[hashString(key) % COLOR_WHEEL.length];
}

/* ═══════════════════════ матрицы (4×4, column-major) ══════════════ */

function perspective(out, fovY, aspect, near, far) {
  const f = 1 / Math.tan(fovY / 2);
  out[0] = f / aspect; out[1] = 0; out[2] = 0; out[3] = 0;
  out[4] = 0; out[5] = f; out[6] = 0; out[7] = 0;
  out[8] = 0; out[9] = 0; out[10] = (far + near) / (near - far); out[11] = -1;
  out[12] = 0; out[13] = 0; out[14] = (2 * far * near) / (near - far); out[15] = 0;
  return out;
}

function lookAt(out, ex, ey, ez, cx, cy, cz, ux, uy, uz) {
  let zx = ex - cx;
  let zy = ey - cy;
  let zz = ez - cz;
  let len = Math.hypot(zx, zy, zz) || 1;
  zx /= len; zy /= len; zz /= len;
  let xx = uy * zz - uz * zy;
  let xy = uz * zx - ux * zz;
  let xz = ux * zy - uy * zx;
  len = Math.hypot(xx, xy, xz);
  if (len < 1e-6) {
    // взгляд совпал с «верхом» — берём запасную ось, иначе кадр схлопнется
    xx = 1; xy = 0; xz = 0;
  } else {
    xx /= len; xy /= len; xz /= len;
  }
  const yx = zy * xz - zz * xy;
  const yy = zz * xx - zx * xz;
  const yz = zx * xy - zy * xx;
  out[0] = xx; out[1] = yx; out[2] = zx; out[3] = 0;
  out[4] = xy; out[5] = yy; out[6] = zy; out[7] = 0;
  out[8] = xz; out[9] = yz; out[10] = zz; out[11] = 0;
  out[12] = -(xx * ex + xy * ey + xz * ez);
  out[13] = -(yx * ex + yy * ey + yz * ez);
  out[14] = -(zx * ex + zy * ey + zz * ez);
  out[15] = 1;
  return out;
}

function multiply(out, a, b) {
  for (let col = 0; col < 4; col += 1) {
    const b0 = b[col * 4];
    const b1 = b[col * 4 + 1];
    const b2 = b[col * 4 + 2];
    const b3 = b[col * 4 + 3];
    out[col * 4] = a[0] * b0 + a[4] * b1 + a[8] * b2 + a[12] * b3;
    out[col * 4 + 1] = a[1] * b0 + a[5] * b1 + a[9] * b2 + a[13] * b3;
    out[col * 4 + 2] = a[2] * b0 + a[6] * b1 + a[10] * b2 + a[14] * b3;
    out[col * 4 + 3] = a[3] * b0 + a[7] * b1 + a[11] * b2 + a[15] * b3;
  }
  return out;
}

/* ═══════════════════════════ GLSL ═════════════════════════════════ */

/* Узлы — капли пигмента: аддитивные point-спрайты с мягким радиальным
   спадом, посчитанным во фрагментнике. Ни текстур, ни квадов из файла. */
const NODE_VS = `#version 300 es
precision highp float;
layout(location = 0) in vec3 a_pos;
layout(location = 1) in vec3 a_color;
layout(location = 2) in float a_size;
layout(location = 3) in float a_flag;
uniform mat4 u_view;
uniform mat4 u_proj;
uniform float u_sizeScale;
uniform float u_pointMax;
uniform vec2 u_fogRange;
out vec3 v_color;
out float v_fog;
out float v_flag;
void main() {
  vec4 viewPos = u_view * vec4(a_pos, 1.0);
  gl_Position = u_proj * viewPos;
  float dist = max(0.35, -viewPos.z);
  float size = (u_sizeScale * a_size * (1.0 + a_flag * 0.34)) / dist;
  gl_PointSize = clamp(size, 2.0, u_pointMax);
  v_color = a_color;
  v_flag = a_flag;
  v_fog = clamp((dist - u_fogRange.x) / max(1.0, u_fogRange.y - u_fogRange.x), 0.0, 1.0);
}
`;

const NODE_FS = `#version 300 es
precision highp float;
in vec3 v_color;
in float v_fog;
in float v_flag;
uniform vec3 u_fogColor;
uniform float u_intensity;
out vec4 frag;
void main() {
  vec2 uv = gl_PointCoord * 2.0 - 1.0;
  float d2 = dot(uv, uv);
  if (d2 > 1.0) discard;
  float d = sqrt(d2);
  float halo = exp(-4.4 * d2);
  float core = pow(max(0.0, 1.0 - d), 3.0);
  float rim = v_flag * exp(-26.0 * (d - 0.72) * (d - 0.72)) * 0.8;
  float energy = (halo * 0.52 + core * 0.95 + rim) * u_intensity;
  energy *= mix(1.0, 0.2, v_fog);
  energy *= smoothstep(1.0, 0.86, d);
  vec3 rgb = mix(v_color, u_fogColor, v_fog * 0.55);
  frag = vec4(rgb * energy, energy * 0.85);
}
`;

/* Связи — тихая паутина: тонкие линии, гаснущие с глубиной. */
const LINK_VS = `#version 300 es
precision highp float;
layout(location = 0) in vec3 a_pos;
layout(location = 1) in float a_alpha;
uniform mat4 u_view;
uniform mat4 u_proj;
uniform vec2 u_fogRange;
out float v_alpha;
out float v_fog;
void main() {
  vec4 viewPos = u_view * vec4(a_pos, 1.0);
  gl_Position = u_proj * viewPos;
  float dist = max(0.35, -viewPos.z);
  v_fog = clamp((dist - u_fogRange.x) / max(1.0, u_fogRange.y - u_fogRange.x), 0.0, 1.0);
  v_alpha = a_alpha;
}
`;

const LINK_FS = `#version 300 es
precision highp float;
in float v_alpha;
in float v_fog;
uniform vec3 u_color;
uniform vec3 u_fogColor;
uniform float u_intensity;
out vec4 frag;
void main() {
  float alpha = v_alpha * u_intensity * mix(1.0, 0.1, v_fog);
  vec3 rgb = mix(u_color, u_fogColor, v_fog * 0.6);
  frag = vec4(rgb * alpha, alpha);
}
`;

/* ═══════════════════ нормализация полезной нагрузки ═══════════════ */

/**
 * Обе формы приводятся к одному виду. Различаем по рёбрам: `a`/`b` — память,
 * `source`/`target` — код. Если рёбер нет вовсе, смотрим на поля узлов
 * (`funcs`/`classes`/`loc`/`doc` бывают только у кода).
 */
function normalise(graph, maxNodes, maxLinks) {
  const rawNodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const rawLinks = Array.isArray(graph?.links) ? graph.links : [];

  let mode = "memory";
  const codeEdge = rawLinks.some((link) => link && (link.source !== undefined || link.target !== undefined));
  const memoryEdge = rawLinks.some((link) => link && (link.a !== undefined || link.b !== undefined));
  if (codeEdge && !memoryEdge) mode = "code";
  else if (!codeEdge && !memoryEdge) {
    const codeNode = rawNodes.some((node) => node
      && (node.funcs !== undefined || node.classes !== undefined || node.loc !== undefined || node.doc !== undefined));
    if (codeNode) mode = "code";
  }

  const endsOf = (link) => {
    if (!link) return null;
    const from = link.a !== undefined || link.b !== undefined ? link.a : link.source;
    const to = link.a !== undefined || link.b !== undefined ? link.b : link.target;
    const sa = String(from ?? "");
    const sb = String(to ?? "");
    return sa && sb && sa !== sb ? [sa, sb] : null;
  };

  // степень считаем по ПОЛНОМУ списку рёбер, до всякой обрезки — иначе
  // ранжирование «кто важнее» соврало бы ровно на тех узлах, что мы режем
  const degrees = new Map();
  for (const link of rawLinks) {
    const ends = endsOf(link);
    if (!ends) continue;
    degrees.set(ends[0], (degrees.get(ends[0]) || 0) + 1);
    degrees.set(ends[1], (degrees.get(ends[1]) || 0) + 1);
  }

  const prepared = [];
  const seen = new Set();
  for (const node of rawNodes) {
    if (!node || node.id === undefined || node.id === null) continue;
    const id = String(node.id);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const kind = textOf(node.kind ?? (mode === "memory" ? "topic" : "module"), 40).toLowerCase() || "topic";
    const declared = node.degree === undefined ? null : Math.max(0, intOf(node.degree, 0));
    const degree = declared === null ? (degrees.get(id) || 0) : declared;
    const size = Math.max(0, intOf(node.size, 0));
    const funcs = Math.max(0, intOf(node.funcs, 0));
    const classes = Math.max(0, intOf(node.classes, 0));
    const loc = Math.max(0, intOf(node.loc, 0));
    // «масса» узла: в памяти это связи, в коде — связи плюс собственный объём
    const mass = mode === "memory"
      ? degree
      : degree + funcs * 0.8 + classes * 1.2 + Math.sqrt(Math.max(loc, size / 40)) * 0.35;
    prepared.push({
      id,
      label: textOf(node.label ?? node.name ?? id, 120) || id,
      kind,
      degree,
      size,
      funcs,
      classes,
      loc,
      doc: textOf(node.doc ?? "", 600),
      mass,
      raw: node,
    });
  }

  prepared.sort((a, b) => b.mass - a.mass || b.degree - a.degree || (a.id < b.id ? -1 : 1));
  const kept = prepared.slice(0, Math.max(1, maxNodes));
  const index = new Map(kept.map((node, i) => [node.id, i]));

  const maxMass = kept.reduce((acc, node) => Math.max(acc, node.mass), 1) || 1;
  const count = kept.length;
  const spread = Math.max(220, Math.cbrt(Math.max(1, count)) * 190);

  const nodes = kept.map((node, i) => {
    // сфера Фибоначчи — детерминированный старт без «комка» в центре
    const t = (i + 0.5) / Math.max(1, count);
    const phi = Math.acos(1 - 2 * t);
    const theta = i * 2.399963229728653;
    const radius = spread * Math.cbrt(t) + seeded(i, 3) * 14;
    return {
      ...node,
      r: clamp(3.4 + Math.sqrt(node.mass) * 2.6, 3.4, 22),
      weight: 1 + node.mass / maxMass,
      x: Math.sin(phi) * Math.cos(theta) * radius,
      y: Math.cos(phi) * radius,
      z: Math.sin(phi) * Math.sin(theta) * radius,
      vx: 0,
      vy: 0,
      vz: 0,
      sx: 0,
      sy: 0,
      sw: 0,
      sr: 0,
      colorKey: colorKeyFor(node.kind),
    };
  });

  // рёбра: сначала важные — обрезка не должна съесть именно несущие связи
  const edges = [];
  const pairs = new Set();
  for (const link of rawLinks) {
    const ends = endsOf(link);
    if (!ends) continue;
    const a = index.get(ends[0]);
    const b = index.get(ends[1]);
    if (a === undefined || b === undefined || a === b) continue;
    const key = a < b ? `${a}|${b}` : `${b}|${a}`;
    if (pairs.has(key)) continue;
    pairs.add(key);
    // `calls` в графе кода — СПИСОК мест вызова, а не число: Number([1,2,3]) = NaN,
    // и счётчик молча обнулялся бы ровно на самых горячих рёбрах
    const calls = Array.isArray(link.calls) ? link.calls.length : Math.max(0, intOf(link.calls, 0));
    const weight = Math.max(0, intOf(link.weight, calls || 1)) || 1;
    edges.push({
      a,
      b,
      label: textOf(link.label ?? "", 90),
      kind: textOf(link.kind ?? "", 40).toLowerCase(),
      weight,
      calls,
    });
  }
  edges.sort((x, y) => y.weight - x.weight);
  const links = edges.slice(0, Math.max(0, maxLinks));

  const maxWeight = links.reduce((acc, link) => Math.max(acc, link.weight), 1) || 1;
  const neighbours = new Map(nodes.map((node) => [node.id, []]));
  for (const link of links) {
    const a = nodes[link.a];
    const b = nodes[link.b];
    neighbours.get(a.id).push({ id: b.id, label: link.label, kind: link.kind, weight: link.weight, calls: link.calls });
    neighbours.get(b.id).push({ id: a.id, label: link.label, kind: link.kind, weight: link.weight, calls: link.calls });
    link.alpha = clamp(0.1 + (link.weight / maxWeight) * 0.34, 0.08, 0.46);
  }

  return { mode, nodes, links, index, neighbours, dropped: prepared.length - kept.length, edgesTotal: edges.length };
}

/* ═══════════════════════════ Space3D ══════════════════════════════ */

export class Space3D {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {{onSelect?:Function, onLabels?:Function, onEmptyTap?:Function,
   *          maxNodes?:number, maxLinks?:number, labelCap?:number,
   *          autoRotate?:boolean, ariaLabel?:string}} [opts]
   * @throws {Error} если нет WebGL2 или highp float — хост обязан откатиться на 2D
   */
  constructor(canvas, opts = {}) {
    if (!(canvas instanceof HTMLCanvasElement)) {
      throw new TypeError("Space3D: нужен <canvas>");
    }
    this.canvas = canvas;
    this.opts = opts || {};
    this.onSelect = typeof opts.onSelect === "function" ? opts.onSelect : null;
    this.onLabels = typeof opts.onLabels === "function" ? opts.onLabels : null;
    // Короткий тап по пустому месту. Сцена НЕ знает ни про полноэкранный слой, ни
    // про то, куда её переносят: она только сообщает хосту о жесте.
    this.onEmptyTap = typeof opts.onEmptyTap === "function" ? opts.onEmptyTap : null;
    this.maxNodes = clamp(intOf(opts.maxNodes, MAX_NODES), 1, 4000);
    this.maxLinks = clamp(intOf(opts.maxLinks, MAX_LINKS), 0, 12000);
    this.labelCap = clamp(intOf(opts.labelCap, LABEL_CAP), 0, 40);

    const attrs = {
      alpha: true,
      antialias: false,        // спрайты и так мягкие; MSAA на телефоне — чистый налог
      depth: false,            // аддитивная смесь порядко-независима, z-буфер не нужен
      stencil: false,
      premultipliedAlpha: true,
      powerPreference: "low-power",
      desynchronized: true,
      preserveDrawingBuffer: false,
      failIfMajorPerformanceCaveat: false,
    };
    const gl = canvas.getContext("webgl2", attrs);
    if (!gl) {
      throw new Error("Space3D: WebGL2 недоступен — нужен откат на 2D-созвездие");
    }
    const precision = gl.getShaderPrecisionFormat(gl.FRAGMENT_SHADER, gl.HIGH_FLOAT);
    if (!precision || precision.precision < 16) {
      throw new Error("Space3D: во фрагментном шейдере нет highp float — нужен откат на 2D-созвездие");
    }
    this.gl = gl;
    this.loseExt = gl.getExtension("WEBGL_lose_context");

    /* ── состояние сцены ─────────────────────────────────────────── */
    this.destroyed = false;
    this.nodes = [];
    this.links = [];
    this.index = new Map();
    this.neighbours = new Map();
    this.mode = "memory";
    this.stats = { mode: "memory", nodes: 0, links: 0, dropped: 0, empty: true, degraded: false };

    this.palette = readPalette();
    this.alpha = 1;
    this.radius = 400;
    this.center = [0, 0, 0];

    /* ── камера ──────────────────────────────────────────────────── */
    this.yaw = 0.6;
    this.pitch = 0.42;
    this.dolly = 1;             // множитель к «вписанной» дистанции
    this.fitDistance = 1200;
    this.target = [0, 0, 0];
    this.targetWanted = [0, 0, 0];
    this.userMoved = false;
    this.lastInteraction = 0;
    this.selected = null;
    // ⚠ 03.08.2026. Свёрнутая сцена — полоса 286–420 px внутри прокручиваемой
    // секции, и вертикальный жест там принадлежит странице (`touch-action: pan-y`
    // на .mem-canvas). Развёрнутая занимает экран целиком, отбирать не у кого —
    // и только тогда одним пальцем можно наклонять камеру по обеим осям.
    this.immersive = false;

    /* ── кадры ───────────────────────────────────────────────────── */
    this.running = false;
    this.frame = 0;
    this.lastFrame = 0;
    this.lastDraw = 0;
    this.sampleStarted = 0;
    this.sampleFrames = 0;
    this.targetFps = 60;
    this.lowPower = false;
    this.labelAt = 0;
    this.dirty = true;

    this.width = 0;
    this.height = 0;
    this.dpr = 1;

    this.media = matchMedia("(prefers-reduced-motion: reduce)");
    this.reducedMotion = this.media.matches;

    this.pointers = new Map();
    this.gesture = null;
    this.pinch = null;

    /* ── матрицы ─────────────────────────────────────────────────── */
    this.mProj = new Float32Array(16);
    this.mView = new Float32Array(16);
    this.mViewProj = new Float32Array(16);
    this.eye = [0, 0, 0];
    // туман считается в _updateMatrices(), но _labels() может позвать раньше первого
    // кадра — без этих значений глубина подписи вышла бы NaN
    this.fogNear = 1;
    this.fogFar = 2;

    /* ── GL-ресурсы ──────────────────────────────────────────────── */
    this.nodeProgram = this._program(NODE_VS, NODE_FS);
    this.linkProgram = this._program(LINK_VS, LINK_FS);
    this.nodeU = this._uniforms(this.nodeProgram, ["u_view", "u_proj", "u_sizeScale", "u_pointMax", "u_fogRange", "u_fogColor", "u_intensity"]);
    this.linkU = this._uniforms(this.linkProgram, ["u_view", "u_proj", "u_fogRange", "u_color", "u_fogColor", "u_intensity"]);
    this.nodeCap = 0;
    this.linkCap = 0;
    this.nodeVao = null;
    this.linkVao = null;
    this.bufNodePos = null;
    this.bufNodeColor = null;
    this.bufNodeSize = null;
    this.bufNodeFlag = null;
    this.bufLinkPos = null;
    this.bufLinkAlpha = null;
    this.arrNodePos = null;
    this.arrNodeColor = null;
    this.arrNodeSize = null;
    this.arrNodeFlag = null;
    this.arrLinkPos = null;
    this.arrLinkAlpha = null;

    const pointRange = gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE);
    this.pointMax = clamp(Array.isArray(pointRange) || pointRange?.length ? pointRange[1] : 64, 8, 160);

    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    gl.enable(gl.BLEND);
    // аддитивный цвет + накапливающаяся, но не срывающаяся альфа
    gl.blendFuncSeparate(gl.ONE, gl.ONE, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.clearColor(0, 0, 0, 0);

    /* ── слушатели ───────────────────────────────────────────────── */
    this._tick = this._tick.bind(this);
    this._onPointerDown = this._onPointerDown.bind(this);
    this._onPointerMove = this._onPointerMove.bind(this);
    this._onPointerUp = this._onPointerUp.bind(this);
    this._onWheel = this._onWheel.bind(this);
    this._onKey = this._onKey.bind(this);
    this._onMotionChange = this._onMotionChange.bind(this);
    this._onVisibility = this._onVisibility.bind(this);
    this._onResize = this._onResize.bind(this);
    this._onContextLost = this._onContextLost.bind(this);

    canvas.tabIndex = 0;
    canvas.setAttribute("role", "img");
    if (opts.ariaLabel) canvas.setAttribute("aria-label", String(opts.ariaLabel));
    canvas.addEventListener("pointerdown", this._onPointerDown);
    canvas.addEventListener("pointermove", this._onPointerMove);
    canvas.addEventListener("pointerup", this._onPointerUp);
    canvas.addEventListener("pointercancel", this._onPointerUp);
    canvas.addEventListener("pointerleave", this._onPointerUp);
    canvas.addEventListener("wheel", this._onWheel, { passive: false });
    canvas.addEventListener("keydown", this._onKey);
    canvas.addEventListener("webglcontextlost", this._onContextLost);
    this.media.addEventListener?.("change", this._onMotionChange);
    document.addEventListener("visibilitychange", this._onVisibility);

    this.observer = typeof ResizeObserver === "function" ? new ResizeObserver(this._onResize) : null;
    if (this.observer) this.observer.observe(canvas);
    else window.addEventListener("resize", this._onResize, { passive: true });

    this._inspectBattery();
    this._measure();
  }

  /* ── создание программ ─────────────────────────────────────────── */

  _shader(type, source) {
    const gl = this.gl;
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(shader) || "";
      gl.deleteShader(shader);
      throw new Error(`Space3D: шейдер не собрался — ${log.trim() || "без диагностики"}`);
    }
    return shader;
  }

  _program(vsSource, fsSource) {
    const gl = this.gl;
    const vs = this._shader(gl.VERTEX_SHADER, vsSource);
    const fs = this._shader(gl.FRAGMENT_SHADER, fsSource);
    const program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const log = gl.getProgramInfoLog(program) || "";
      gl.deleteProgram(program);
      throw new Error(`Space3D: программа не слинковалась — ${log.trim() || "без диагностики"}`);
    }
    return program;
  }

  _uniforms(program, names) {
    const gl = this.gl;
    const map = {};
    for (const name of names) map[name] = gl.getUniformLocation(program, name);
    return map;
  }

  /* ── буферы: пересоздаём ТОЛЬКО когда меняются количества ──────── */

  _ensureBuffers(nodeCount, linkCount) {
    const gl = this.gl;
    if (nodeCount !== this.nodeCap) {
      this._dropNodeBuffers();
      this.nodeCap = nodeCount;
      if (nodeCount > 0) {
        this.arrNodePos = new Float32Array(nodeCount * 3);
        this.arrNodeColor = new Float32Array(nodeCount * 3);
        this.arrNodeSize = new Float32Array(nodeCount);
        this.arrNodeFlag = new Float32Array(nodeCount);
        this.nodeVao = gl.createVertexArray();
        gl.bindVertexArray(this.nodeVao);
        this.bufNodePos = this._attrib(0, this.arrNodePos, 3, gl.DYNAMIC_DRAW);
        this.bufNodeColor = this._attrib(1, this.arrNodeColor, 3, gl.DYNAMIC_DRAW);
        this.bufNodeSize = this._attrib(2, this.arrNodeSize, 1, gl.DYNAMIC_DRAW);
        this.bufNodeFlag = this._attrib(3, this.arrNodeFlag, 1, gl.DYNAMIC_DRAW);
        gl.bindVertexArray(null);
      }
    }
    if (linkCount !== this.linkCap) {
      this._dropLinkBuffers();
      this.linkCap = linkCount;
      if (linkCount > 0) {
        this.arrLinkPos = new Float32Array(linkCount * 6);
        this.arrLinkAlpha = new Float32Array(linkCount * 2);
        this.linkVao = gl.createVertexArray();
        gl.bindVertexArray(this.linkVao);
        this.bufLinkPos = this._attrib(0, this.arrLinkPos, 3, gl.DYNAMIC_DRAW);
        this.bufLinkAlpha = this._attrib(1, this.arrLinkAlpha, 1, gl.DYNAMIC_DRAW);
        gl.bindVertexArray(null);
      }
    }
  }

  _attrib(location, data, size, usage) {
    const gl = this.gl;
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, data.byteLength, usage);
    gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, size, gl.FLOAT, false, 0, 0);
    return buffer;
  }

  _dropNodeBuffers() {
    const gl = this.gl;
    for (const buffer of [this.bufNodePos, this.bufNodeColor, this.bufNodeSize, this.bufNodeFlag]) {
      if (buffer) gl.deleteBuffer(buffer);
    }
    if (this.nodeVao) gl.deleteVertexArray(this.nodeVao);
    this.nodeVao = null;
    this.bufNodePos = this.bufNodeColor = this.bufNodeSize = this.bufNodeFlag = null;
    this.arrNodePos = this.arrNodeColor = this.arrNodeSize = this.arrNodeFlag = null;
  }

  _dropLinkBuffers() {
    const gl = this.gl;
    for (const buffer of [this.bufLinkPos, this.bufLinkAlpha]) {
      if (buffer) gl.deleteBuffer(buffer);
    }
    if (this.linkVao) gl.deleteVertexArray(this.linkVao);
    this.linkVao = null;
    this.bufLinkPos = this.bufLinkAlpha = null;
    this.arrLinkPos = this.arrLinkAlpha = null;
  }

  /* ── данные ────────────────────────────────────────────────────── */

  /**
   * Принимает обе формы (память и код) и приводит к одному виду.
   * @returns {{mode:string, nodes:number, links:number, dropped:number, empty:boolean, degraded:boolean}}
   *          сводка для хоста: по ней он рисует «пусто» / «не прочиталось».
   */
  setGraph(graph) {
    if (this.destroyed) return this.stats;
    // degraded → память НЕ прочиталась. Рисовать пустоту здесь было бы ложью о ней:
    // сцену чистим, но в сводке говорим прямо, что это сбой чтения.
    if (graph?.degraded === true) {
      this._clearScene();
      this.stats = { mode: this.mode, nodes: 0, links: 0, dropped: 0, empty: true, degraded: true };
      return this.stats;
    }

    const data = normalise(graph, this.maxNodes, this.maxLinks);
    this.mode = data.mode;
    this.nodes = data.nodes;
    this.links = data.links;
    this.index = data.index;
    this.neighbours = data.neighbours;
    this.selected = null;
    this.alpha = 1;

    this.stats = {
      mode: data.mode,
      nodes: this.nodes.length,
      links: this.links.length,
      dropped: data.dropped,
      linksDropped: Math.max(0, data.edgesTotal - this.links.length),
      empty: this.nodes.length === 0,
      degraded: false,
    };

    this._ensureBuffers(this.nodes.length, this.links.length);
    if (!this.nodes.length) {
      this._emitLabels([]);
      this._draw();
      return this.stats;
    }

    this._uploadStatic();
    for (let step = 0; step < PRESTEPS; step += 1) this._step(1);
    this.userMoved = false;
    this.recenter();

    if (this.reducedMotion) {
      this._settle();
    } else {
      this.start();
    }
    return this.stats;
  }

  _clearScene() {
    this.nodes = [];
    this.links = [];
    this.index = new Map();
    this.neighbours = new Map();
    this.selected = null;
    this._ensureBuffers(0, 0);
    this._emitLabels([]);
    this._draw();
  }

  /** Досчитать раскладку разом (reduced-motion) и нарисовать один кадр. */
  _settle() {
    for (let step = 0; step < STATIC_STEPS && this.alpha > ALPHA_MIN; step += 1) {
      this._step(this.alpha);
      this.alpha *= ALPHA_DECAY;
    }
    this.alpha = 0;
    if (!this.userMoved) this.recenter();
    this._draw();
    this._emitLabels(this._labels());
  }

  _uploadStatic() {
    const gl = this.gl;
    if (!this.nodes.length || !this.arrNodeColor) return;
    const palette = this.palette;
    for (let i = 0; i < this.nodes.length; i += 1) {
      const node = this.nodes[i];
      const rgb = palette[node.colorKey] || palette.cyan;
      this.arrNodeColor[i * 3] = rgb[0];
      this.arrNodeColor[i * 3 + 1] = rgb[1];
      this.arrNodeColor[i * 3 + 2] = rgb[2];
      // спрайт шире самой капли: свечение живёт в поле точки, а не в её ядре
      this.arrNodeSize[i] = node.r * 3.1;
      this.arrNodeFlag[i] = node.id === this.selected ? 1 : 0;
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bufNodeColor);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.arrNodeColor);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bufNodeSize);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.arrNodeSize);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bufNodeFlag);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.arrNodeFlag);

    if (this.links.length && this.arrLinkAlpha) {
      for (let i = 0; i < this.links.length; i += 1) {
        this.arrLinkAlpha[i * 2] = this.links[i].alpha;
        this.arrLinkAlpha[i * 2 + 1] = this.links[i].alpha;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bufLinkAlpha);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.arrLinkAlpha);
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
  }

  /** Перечитать палитру из CSS (смена темы) и залить цвета заново. */
  refreshTheme() {
    if (this.destroyed) return;
    this.palette = readPalette();
    this._uploadStatic();
    this.request();
  }

  getNode(id) {
    const i = this.index.get(String(id));
    return i === undefined ? null : this.nodes[i];
  }

  getNeighbours(id) {
    return this.neighbours.get(String(id)) || [];
  }

  /* ── физика: 3D-сетка + пружины + центрирование ────────────────── */

  _step(alpha) {
    const nodes = this.nodes;
    const n = nodes.length;
    if (!n) return;

    // равномерная сетка: отталкивание считаем только внутри CUTOFF → почти линейно
    const grid = new Map();
    for (let i = 0; i < n; i += 1) {
      const node = nodes[i];
      const key = cellKey(
        Math.floor(node.x / CUTOFF),
        Math.floor(node.y / CUTOFF),
        Math.floor(node.z / CUTOFF),
      );
      const bucket = grid.get(key);
      if (bucket) bucket.push(i); else grid.set(key, [i]);
    }

    const cutoff2 = CUTOFF * CUTOFF;
    for (let i = 0; i < n; i += 1) {
      const node = nodes[i];
      const cx = Math.floor(node.x / CUTOFF);
      const cy = Math.floor(node.y / CUTOFF);
      const cz = Math.floor(node.z / CUTOFF);
      for (let gx = cx - 1; gx <= cx + 1; gx += 1) {
        for (let gy = cy - 1; gy <= cy + 1; gy += 1) {
          for (let gz = cz - 1; gz <= cz + 1; gz += 1) {
            const bucket = grid.get(cellKey(gx, gy, gz));
            if (!bucket) continue;
            for (let k = 0; k < bucket.length; k += 1) {
              const j = bucket[k];
              if (j <= i) continue;
              const other = nodes[j];
              let dx = node.x - other.x;
              let dy = node.y - other.y;
              let dz = node.z - other.z;
              let dist2 = dx * dx + dy * dy + dz * dz;
              if (dist2 > cutoff2) continue;
              if (dist2 < 0.01) {
                dx = (seeded(i, j) - 0.5) * 2;
                dy = (seeded(j, i) - 0.5) * 2;
                dz = (seeded(i + j, 7) - 0.5) * 2;
                dist2 = dx * dx + dy * dy + dz * dz + 0.01;
              }
              const dist = Math.sqrt(dist2);
              const force = (REP * node.weight * other.weight * alpha) / dist2;
              const fx = (dx / dist) * force;
              const fy = (dy / dist) * force;
              const fz = (dz / dist) * force;
              node.vx += fx; node.vy += fy; node.vz += fz;
              other.vx -= fx; other.vy -= fy; other.vz -= fz;
            }
          }
        }
      }
    }

    for (let i = 0; i < this.links.length; i += 1) {
      const link = this.links[i];
      const a = nodes[link.a];
      const b = nodes[link.b];
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dz = b.z - a.z;
      const dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 0.01;
      const rest = 74 + (a.r + b.r) * 1.7;
      const force = SPRING * (dist - rest) * alpha;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      const fz = (dz / dist) * force;
      a.vx += fx; a.vy += fy; a.vz += fz;
      b.vx -= fx; b.vy -= fy; b.vz -= fz;
    }

    for (let i = 0; i < n; i += 1) {
      const node = nodes[i];
      node.vx -= node.x * CENTER;
      node.vy -= node.y * CENTER;
      node.vz -= node.z * CENTER;
      node.vx = clamp(node.vx * DAMP, -MAX_V, MAX_V);
      node.vy = clamp(node.vy * DAMP, -MAX_V, MAX_V);
      node.vz = clamp(node.vz * DAMP, -MAX_V, MAX_V);
      node.x += node.vx;
      node.y += node.vy;
      node.z += node.vz;
    }
  }

  _settled() {
    return this.alpha <= ALPHA_MIN;
  }

  /* ── размеры ───────────────────────────────────────────────────── */

  _measure() {
    const rect = this.canvas.getBoundingClientRect();
    const width = Math.max(1, Math.round(rect.width));
    const height = Math.max(1, Math.round(rect.height));
    const cap = this.lowPower ? DPR_CAP_LOW : DPR_CAP;
    const dpr = clamp(window.devicePixelRatio || 1, 1, cap);
    if (width === this.width && height === this.height && dpr === this.dpr) return false;
    this.width = width;
    this.height = height;
    this.dpr = dpr;
    const pw = Math.round(width * dpr);
    const ph = Math.round(height * dpr);
    if (this.canvas.width !== pw) this.canvas.width = pw;
    if (this.canvas.height !== ph) this.canvas.height = ph;
    return true;
  }

  _onResize() {
    if (this.destroyed) return;
    if (!this._measure()) return;
    // ⚠ 03.08.2026. Раньше здесь было только `if (!this.userMoved) this.recenter()`,
    // и покрученная сцена при смене размера не пересчитывала НИЧЕГО. Пропорции при
    // этом не врали (аспект проекции живой — _updateMatrices считает его каждый
    // кадр), а вот `fitDistance` — «дистанция, с которой граф целиком влезает в
    // кадр» — оставалась от прежнего аспекта. Цена стала видна на развороте: сцена,
    // которую хоть раз повернули пальцем, разворачивалась на весь экран с рамкой от
    // полосы в 320 px. _refit() пересчитывает ТОЛЬКО кадрирование и не трогает ни
    // поворот, ни dolly, ни userMoved: камера остаётся там, куда её поставили.
    if (!this.userMoved) this.recenter();
    else this._refit();
    this.request();
  }

  /** Полноэкранный режим: сцена забирает жесты целиком и перекадрируется. */
  setImmersive(on) {
    const next = Boolean(on);
    if (next === this.immersive) return;
    this.immersive = next;
    // Жест, начатый в прежней геометрии, продолжать нечем: холст только что уехал
    // в другого родителя и сменил размер, а pointerup по нему уже не придёт.
    this.gesture = null;
    this.pointers.clear();
    this.pinch = null;
    this._onResize();
  }

  async _inspectBattery() {
    if (typeof navigator.getBattery !== "function") return;
    try {
      const battery = await navigator.getBattery();
      const update = () => {
        if (this.destroyed) return;
        const constrained = !battery.charging && battery.level < 0.28;
        if (constrained === this.lowPower) return;
        this.lowPower = constrained;
        this.targetFps = constrained ? 30 : 60;
        this.dpr = 0;             // заставляем _measure() пересчитать растр
        this._onResize();
      };
      update();
      battery.addEventListener("levelchange", update);
      battery.addEventListener("chargingchange", update);
    } catch (_) {
      // Battery status — необязательная роскошь, а не зависимость рантайма.
    }
  }

  /* ── камера ────────────────────────────────────────────────────── */

  _bounds() {
    if (!this.nodes.length) return { cx: 0, cy: 0, cz: 0, radius: 300 };
    let minX = Infinity, minY = Infinity, minZ = Infinity;
    let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    for (const node of this.nodes) {
      if (node.x < minX) minX = node.x;
      if (node.y < minY) minY = node.y;
      if (node.z < minZ) minZ = node.z;
      if (node.x > maxX) maxX = node.x;
      if (node.y > maxY) maxY = node.y;
      if (node.z > maxZ) maxZ = node.z;
    }
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const cz = (minZ + maxZ) / 2;
    let radius = 1;
    for (const node of this.nodes) {
      const d = Math.hypot(node.x - cx, node.y - cy, node.z - cz) + node.r;
      if (d > radius) radius = d;
    }
    return { cx, cy, cz, radius };
  }

  /** Только кадрирование: границы графа и дистанция «всё влезает» под текущий
      кадр. Ни поворота, ни dolly, ни userMoved — это отдельные решения владельца,
      и смена размера окна не повод их отменять. */
  _refit() {
    const { cx, cy, cz, radius } = this._bounds();
    this.center = [cx, cy, cz];
    this.radius = radius;
    const aspect = Math.max(0.35, this.width / Math.max(1, this.height));
    const fovX = 2 * Math.atan(Math.tan(FOV / 2) * aspect);
    const fit = Math.max(radius / Math.tan(FOV / 2), radius / Math.tan(fovX / 2));
    this.fitDistance = Math.max(60, fit * 1.22);
    return { cx, cy, cz };
  }

  /** Вписать весь граф в кадр. */
  recenter() {
    const { cx, cy, cz } = this._refit();
    this.dolly = 1;
    this.targetWanted = [cx, cy, cz];
    if (this.reducedMotion || !this.running) this.target = [cx, cy, cz];
    this.userMoved = false;
    this.lastInteraction = 0;
    this.request();
  }

  /** Подвести камеру к узлу и подсветить его. */
  focus(nodeId) {
    const node = this.getNode(nodeId);
    if (!node) return;
    this.selected = node.id;
    this.targetWanted = [node.x, node.y, node.z];
    if (this.reducedMotion) this.target = [node.x, node.y, node.z];
    this.dolly = clamp(this.dolly * 0.78, DOLLY_MIN, DOLLY_MAX);
    this.userMoved = true;
    this._markInteraction();
    this._uploadStatic();
    this.request();
  }

  _cameraDistance() {
    return clamp(this.fitDistance * this.dolly, 40, this.fitDistance * DOLLY_MAX + 4000);
  }

  _updateMatrices() {
    const dist = this._cameraDistance();
    const cp = Math.cos(this.pitch);
    const ex = this.target[0] + dist * cp * Math.sin(this.yaw);
    const ey = this.target[1] + dist * Math.sin(this.pitch);
    const ez = this.target[2] + dist * cp * Math.cos(this.yaw);
    const far = dist + this.radius * 3 + 500;
    perspective(this.mProj, FOV, Math.max(0.35, this.width / Math.max(1, this.height)), NEAR, far);
    lookAt(this.mView, ex, ey, ez, this.target[0], this.target[1], this.target[2], 0, 1, 0);
    multiply(this.mViewProj, this.mProj, this.mView);
    this.eye = [ex, ey, ez];
    this.fogNear = Math.max(1, dist - this.radius * 0.55);
    this.fogFar = Math.max(this.fogNear + 1, dist + this.radius * 1.35);
  }

  /* ── проекция и попадания ──────────────────────────────────────── */

  _projectAll() {
    const m = this.mViewProj;
    const halfW = this.width / 2;
    const halfH = this.height / 2;
    const sizeScale = (1 / Math.tan(FOV / 2)) * (this.height / 2);
    for (const node of this.nodes) {
      const px = node.x;
      const py = node.y;
      const pz = node.z;
      const cx = m[0] * px + m[4] * py + m[8] * pz + m[12];
      const cy = m[1] * px + m[5] * py + m[9] * pz + m[13];
      const cw = m[3] * px + m[7] * py + m[11] * pz + m[15];
      node.sw = cw;
      if (cw <= 0.0001) {
        node.sx = NaN;
        node.sy = NaN;
        node.sr = 0;
        continue;
      }
      node.sx = halfW + (cx / cw) * halfW;
      node.sy = halfH - (cy / cw) * halfH;
      node.sr = clamp((sizeScale * node.r) / cw, 1.5, 90);
    }
  }

  _hitTest(x, y) {
    let best = null;
    let bestScore = Infinity;
    for (const node of this.nodes) {
      if (!Number.isFinite(node.sx)) continue;
      const radius = Math.max(node.sr * 1.35, 17);
      const dist = Math.hypot(node.sx - x, node.sy - y);
      if (dist > radius) continue;
      // при равном попадании выигрывает тот, кто ближе к камере
      const score = dist + node.sw * 0.0025;
      if (score < bestScore) {
        best = node;
        bestScore = score;
      }
    }
    return best;
  }

  /* ── подписи: DOM поверх холста, текст в WebGL не рисуем ───────── */

  _labels() {
    if (!this.labelCap || !this.nodes.length) return [];
    const pool = this.nodes.slice(0, Math.min(this.nodes.length, this.labelCap * 4));
    const out = [];
    const span = Math.max(1, this.fogFar - this.fogNear);
    for (const node of pool) {
      if (!Number.isFinite(node.sx)) continue;
      if (node.sx < -60 || node.sx > this.width + 60) continue;
      if (node.sy < -30 || node.sy > this.height + 30) continue;
      const depth = clamp((node.sw - this.fogNear) / span, 0, 1);
      out.push({
        id: node.id,
        label: node.label,
        kind: node.kind,
        colorKey: node.colorKey,
        degree: node.degree,
        x: node.sx,
        y: node.sy,
        radius: node.sr,
        depth,
        alpha: clamp(1 - depth * 0.75, 0.22, 1),
        selected: node.id === this.selected,
      });
      if (out.length >= this.labelCap) break;
    }
    if (this.selected && !out.some((item) => item.id === this.selected)) {
      const node = this.getNode(this.selected);
      if (node && Number.isFinite(node.sx)) {
        out.pop();
        out.push({
          id: node.id,
          label: node.label,
          kind: node.kind,
          colorKey: node.colorKey,
          degree: node.degree,
          x: node.sx,
          y: node.sy,
          radius: node.sr,
          depth: 0,
          alpha: 1,
          selected: true,
        });
      }
    }
    return out;
  }

  _emitLabels(list) {
    if (!this.onLabels) return;
    try {
      this.onLabels(list);
    } catch (_) {
      // хост не обязан переживать наши ошибки — как и в 2D-версии
    }
  }

  /* ── отрисовка ─────────────────────────────────────────────────── */

  _draw() {
    const gl = this.gl;
    if (this.destroyed || gl.isContextLost()) return;
    // Секция «Память» стартует в display:none — сцена может быть 1×1, а уведомление
    // ResizeObserver прийти позже. Перемеряем сами: пустой холст недопустим.
    if (this.width <= 1 || this.height <= 1) {
      if (this._measure() && !this.userMoved) this.recenter();
      if (this.width <= 1 || this.height <= 1) return;
    }
    this.dirty = false;
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clear(gl.COLOR_BUFFER_BIT);
    if (!this.nodes.length) return;

    this._updateMatrices();
    this._projectAll();

    const light = this.palette.light;
    const fogColor = light ? [0.85, 0.88, 0.94] : [0.031, 0.043, 0.07];
    const nodeIntensity = light ? 0.62 : 0.95;
    const linkIntensity = light ? 1.05 : 0.85;
    const fogRange = [this.fogNear, this.fogFar];

    // связи — сначала, они фон для пигмента
    if (this.links.length && this.linkVao) {
      for (let i = 0; i < this.links.length; i += 1) {
        const a = this.nodes[this.links[i].a];
        const b = this.nodes[this.links[i].b];
        const o = i * 6;
        this.arrLinkPos[o] = a.x; this.arrLinkPos[o + 1] = a.y; this.arrLinkPos[o + 2] = a.z;
        this.arrLinkPos[o + 3] = b.x; this.arrLinkPos[o + 4] = b.y; this.arrLinkPos[o + 5] = b.z;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bufLinkPos);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.arrLinkPos);
      gl.useProgram(this.linkProgram);
      gl.uniformMatrix4fv(this.linkU.u_view, false, this.mView);
      gl.uniformMatrix4fv(this.linkU.u_proj, false, this.mProj);
      gl.uniform2f(this.linkU.u_fogRange, fogRange[0], fogRange[1]);
      gl.uniform3fv(this.linkU.u_color, this.palette.blue);
      gl.uniform3f(this.linkU.u_fogColor, fogColor[0], fogColor[1], fogColor[2]);
      gl.uniform1f(this.linkU.u_intensity, linkIntensity);
      gl.bindVertexArray(this.linkVao);
      gl.drawArrays(gl.LINES, 0, this.links.length * 2);
    }

    // капли пигмента
    if (this.nodeVao) {
      for (let i = 0; i < this.nodes.length; i += 1) {
        const node = this.nodes[i];
        this.arrNodePos[i * 3] = node.x;
        this.arrNodePos[i * 3 + 1] = node.y;
        this.arrNodePos[i * 3 + 2] = node.z;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bufNodePos);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.arrNodePos);
      gl.useProgram(this.nodeProgram);
      gl.uniformMatrix4fv(this.nodeU.u_view, false, this.mView);
      gl.uniformMatrix4fv(this.nodeU.u_proj, false, this.mProj);
      gl.uniform1f(this.nodeU.u_sizeScale, (1 / Math.tan(FOV / 2)) * (this.canvas.height / 2));
      gl.uniform1f(this.nodeU.u_pointMax, this.pointMax);
      gl.uniform2f(this.nodeU.u_fogRange, fogRange[0], fogRange[1]);
      gl.uniform3f(this.nodeU.u_fogColor, fogColor[0], fogColor[1], fogColor[2]);
      gl.uniform1f(this.nodeU.u_intensity, nodeIntensity);
      gl.bindVertexArray(this.nodeVao);
      gl.drawArrays(gl.POINTS, 0, this.nodes.length);
    }
    gl.bindVertexArray(null);
  }

  /* ── цикл кадров ───────────────────────────────────────────────── */

  start() {
    if (this.destroyed || this.running) return;
    if (this.reducedMotion) {
      // никакого цикла: раскладку досчитываем разом и рисуем один кадр
      this._settle();
      return;
    }
    if (document.hidden) return;
    this.running = true;
    this.lastFrame = performance.now();
    this.lastDraw = 0;
    this.sampleStarted = this.lastFrame;
    this.sampleFrames = 0;
    this.frame = requestAnimationFrame(this._tick);
  }

  stop() {
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.running = false;
  }

  /** Перерисовать: в покое — одним кадром, иначе поднять цикл. */
  request() {
    if (this.destroyed) return;
    this.dirty = true;
    if (this.reducedMotion) {
      this._draw();
      this._emitLabels(this._labels());
      return;
    }
    if (!this.running) this.start();
  }

  _tick(now) {
    if (!this.running || this.destroyed) return;
    this.frame = requestAnimationFrame(this._tick);
    const interval = 1000 / this.targetFps;
    if (now - this.lastDraw < interval - 1.5) return;
    const dt = clamp(now - this.lastFrame, 0, 50) / 1000;
    this.lastFrame = now;
    this.lastDraw = now;

    let moving = false;
    if (!this._settled()) {
      this._step(this.alpha);
      this.alpha *= ALPHA_DECAY;
      if (!this.userMoved) {
        const { cx, cy, cz, radius } = this._bounds();
        this.center = [cx, cy, cz];
        this.radius = radius;
        this.targetWanted = [cx, cy, cz];
        const aspect = Math.max(0.35, this.width / Math.max(1, this.height));
        const fovX = 2 * Math.atan(Math.tan(FOV / 2) * aspect);
        const fit = Math.max(radius / Math.tan(FOV / 2), radius / Math.tan(fovX / 2));
        this.fitDistance += (Math.max(60, fit * 1.22) - this.fitDistance) * 0.08;
      }
      moving = true;
    }

    // мягкий подъезд камеры к цели
    for (let i = 0; i < 3; i += 1) {
      const delta = this.targetWanted[i] - this.target[i];
      if (Math.abs(delta) > 0.05) {
        this.target[i] += delta * 0.12;
        moving = true;
      } else {
        this.target[i] = this.targetWanted[i];
      }
    }

    // дрейф покоя: останавливается на жесте, возвращается после тишины
    const idle = !this.pointers.size
      && (!this.lastInteraction || now - this.lastInteraction > IDLE_RESUME_MS);
    if (idle) {
      this.yaw = (this.yaw + IDLE_SPIN * dt) % TAU;
      moving = true;
    }

    if (moving || this.dirty) this._draw();
    if (this.onLabels && now - this.labelAt >= LABEL_MS) {
      this.labelAt = now;
      this._emitLabels(this._labels());
    }

    this.sampleFrames += 1;
    const sampleTime = now - this.sampleStarted;
    if (sampleTime >= 4000) {
      const measured = (this.sampleFrames * 1000) / sampleTime;
      if (measured < 47 && this.targetFps === 60) this.targetFps = 30;
      if (measured > 55 && this.targetFps === 30 && !this.lowPower) this.targetFps = 60;
      this.sampleFrames = 0;
      this.sampleStarted = now;
    }
  }

  /* ── жесты ─────────────────────────────────────────────────────── */

  _local(event) {
    const rect = this.canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  _markInteraction() {
    this.lastInteraction = performance.now();
  }

  _onPointerDown(event) {
    if (this.destroyed) return;
    // захват указателя — удобство, а не условие: если браузер его не даёт,
    // жест обязан продолжить жить (иначе тап и вращение умирают молча)
    try { this.canvas.setPointerCapture?.(event.pointerId); } catch (_) { /* не критично */ }
    const point = this._local(event);
    this.pointers.set(event.pointerId, point);
    this._markInteraction();
    if (this.pointers.size === 1) {
      this.gesture = {
        id: event.pointerId,
        startX: point.x,
        startY: point.y,
        lastX: point.x,
        lastY: point.y,
        moved: 0,
        at: performance.now(),
        touch: event.pointerType === "touch",
        rotating: false,
      };
    } else if (this.pointers.size === 2) {
      this.gesture = null;
      this.pinch = this._pinchState();
    }
  }

  _pinchState() {
    const [a, b] = Array.from(this.pointers.values());
    return {
      dist: Math.hypot(b.x - a.x, b.y - a.y) || 1,
      dolly: this.dolly,
    };
  }

  _onPointerMove(event) {
    if (this.destroyed) return;
    const point = this._local(event);
    if (!this.pointers.has(event.pointerId)) {
      const hit = this._hitTest(point.x, point.y);
      this.canvas.style.cursor = hit ? "pointer" : "grab";
      return;
    }
    this.pointers.set(event.pointerId, point);
    this._markInteraction();

    if (this.pointers.size >= 2 && this.pinch) {
      const next = this._pinchState();
      const ratio = clamp(next.dist / this.pinch.dist, 0.2, 5);
      this.dolly = clamp(this.pinch.dolly / ratio, DOLLY_MIN, DOLLY_MAX);
      this.userMoved = true;
      this.request();
      return;
    }

    const gesture = this.gesture;
    if (!gesture || gesture.id !== event.pointerId) return;
    const dx = point.x - gesture.lastX;
    const dy = point.y - gesture.lastY;
    gesture.lastX = point.x;
    gesture.lastY = point.y;
    gesture.moved += Math.abs(dx) + Math.abs(dy);
    if (gesture.moved <= 5) return;

    // ⚠ 03.08.2026. Одним пальцем вращаем только явно горизонтальный жест — и
    // только пока сцена свёрнута. Вертикаль там принадлежит странице
    // (`touch-action: pan-y` на .mem-canvas), иначе полоса в 320 px съедала бы
    // главный жест телефона. Прежняя редакция этого условия пережила правку CSS,
    // где `pan-y` заменили на `none`: страница жест уже не получала, а сцена всё
    // ещё его бросала — и вертикальный свайп не делал ВООБЩЕ ничего. Теперь
    // размен снят: свёрнутая отдаёт вертикаль странице, развёрнутая (immersive)
    // забирает обе оси. Двумя пальцами — pinch, он обработан выше.
    if (!gesture.rotating) {
      const totalDx = point.x - gesture.startX;
      const totalDy = point.y - gesture.startY;
      if (gesture.touch && !this.immersive && Math.abs(totalDx) <= Math.abs(totalDy)) return;
      gesture.rotating = true;
    }

    this.yaw = (this.yaw - dx * 0.0062) % TAU;
    this.pitch = clamp(this.pitch + dy * 0.0058, -PITCH_LIMIT, PITCH_LIMIT);
    this.userMoved = true;
    this.request();
  }

  _onPointerUp(event) {
    if (this.destroyed) return;
    const point = this.pointers.get(event.pointerId);
    this.pointers.delete(event.pointerId);
    if (this.pointers.size < 2) this.pinch = null;
    this._markInteraction();
    const gesture = this.gesture;
    if (!gesture || gesture.id !== event.pointerId) return;
    this.gesture = null;
    if (!point) return;
    const quick = performance.now() - gesture.at < 700;
    if (gesture.moved > 6 || !quick || event.type !== "pointerup") return;

    const hit = this._hitTest(point.x, point.y);
    if (hit) {
      this.selected = hit.id;
      this._uploadStatic();
      this.request();
      if (this.onSelect) {
        try {
          this.onSelect(hit);
        } catch (_) { /* хост не обязан переживать наши ошибки */ }
      }
    } else if (this.selected) {
      this.selected = null;
      this._uploadStatic();
      this.request();
    } else if (this.onEmptyTap) {
      // ⚠ Порядок веток — не украшение. Тап по пустому месту уже занят снятием
      // выделения; повесь разворот раньше — и один тап делал бы два дела сразу.
      // Сюда доходит только заведомо короткое касание: перетаскивания и долгие
      // нажатия отсеяны выше (`gesture.moved > 6 || !quick`).
      try {
        this.onEmptyTap();
      } catch (_) { /* хост не обязан переживать наши ошибки */ }
    }
  }

  _onWheel(event) {
    if (this.destroyed || !this.nodes.length) return;
    event.preventDefault();
    this._markInteraction();
    const factor = Math.exp(clamp(event.deltaY, -120, 120) * 0.0022);
    this.dolly = clamp(this.dolly * factor, DOLLY_MIN, DOLLY_MAX);
    this.userMoved = true;
    this.request();
  }

  _onKey(event) {
    if (this.destroyed) return;
    const map = {
      ArrowLeft: [-0.14, 0],
      ArrowRight: [0.14, 0],
      ArrowUp: [0, -0.1],
      ArrowDown: [0, 0.1],
    };
    if (map[event.key]) {
      event.preventDefault();
      this._markInteraction();
      this.yaw = (this.yaw + map[event.key][0]) % TAU;
      this.pitch = clamp(this.pitch + map[event.key][1], -PITCH_LIMIT, PITCH_LIMIT);
      this.userMoved = true;
      this.request();
      return;
    }
    if (event.key === "+" || event.key === "=" || event.key === "-") {
      event.preventDefault();
      this._markInteraction();
      this.dolly = clamp(this.dolly * (event.key === "-" ? 1.16 : 0.86), DOLLY_MIN, DOLLY_MAX);
      this.userMoved = true;
      this.request();
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      this.recenter();
    }
  }

  _onMotionChange(event) {
    this.reducedMotion = event?.matches ?? this.media.matches;
    if (this.reducedMotion) {
      this.stop();
      this._settle();
    } else {
      this.start();
      this.request();
    }
  }

  _onVisibility() {
    if (document.hidden) this.stop();
    else if (!this.reducedMotion && this.nodes.length) this.start();
  }

  _onContextLost(event) {
    // контекст можно потерять на фоне: молча гасим цикл, кадры без GL бессмысленны
    event.preventDefault?.();
    this.stop();
  }

  /* ── снос ──────────────────────────────────────────────────────── */

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.stop();
    const canvas = this.canvas;
    canvas.removeEventListener("pointerdown", this._onPointerDown);
    canvas.removeEventListener("pointermove", this._onPointerMove);
    canvas.removeEventListener("pointerup", this._onPointerUp);
    canvas.removeEventListener("pointercancel", this._onPointerUp);
    canvas.removeEventListener("pointerleave", this._onPointerUp);
    canvas.removeEventListener("wheel", this._onWheel);
    canvas.removeEventListener("keydown", this._onKey);
    canvas.removeEventListener("webglcontextlost", this._onContextLost);
    this.media.removeEventListener?.("change", this._onMotionChange);
    document.removeEventListener("visibilitychange", this._onVisibility);
    this.observer?.disconnect();
    if (!this.observer) window.removeEventListener("resize", this._onResize);

    const gl = this.gl;
    try {
      this._dropNodeBuffers();
      this._dropLinkBuffers();
      if (this.nodeProgram) gl.deleteProgram(this.nodeProgram);
      if (this.linkProgram) gl.deleteProgram(this.linkProgram);
    } catch (_) { /* контекст мог уже умереть — это не повод падать на снятии */ }
    this.nodeProgram = null;
    this.linkProgram = null;
    this.nodes = [];
    this.links = [];
    this.index = new Map();
    this.neighbours = new Map();
    this.pointers.clear();
    this._emitLabels([]);
    this.onLabels = null;
    this.onSelect = null;
    this.onEmptyTap = null;
    try { this.loseExt?.loseContext(); } catch (_) { /* необязательная любезность драйверу */ }
  }
}

export default { Space3D };
