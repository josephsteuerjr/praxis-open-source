/*
 * memory_views.js — «Пространство памяти», «Пространство кода» и «Как опыт сворачивается».
 *
 * Самодостаточный ES-модуль: без внешних зависимостей, без CDN, без inline-стилей,
 * которые нарушали бы CSP. Единственный импорт — соседний `./space3d.js` (тоже
 * самодостаточный, весь GLSL инлайном). Хост передаёт авторизованный GET (`api`)
 * и открывалку шторки (`sheet`) — модуль сам аутентификацией не занимается.
 *
 * Путь импорта ОТНОСИТЕЛЬНЫЙ намеренно: под скоупом /px переписывается только
 * app.js, поэтому абсолютный путь к каталогу статики здесь сломался бы.
 *
 * Доступ: маршруты (`/memory/graph`, `/memory/compact-dag`, `/code/graph`) закрыты
 * на сервере скоупом `viewer.may("praxis.snapshot")` — тем же, что и остальная
 * секция «Память» (не owner-only: установленный PWA ходит device-токеном и обязан
 * видеть эти виды).
 *
 * Контракт полезной нагрузки, на который мы опираемся дополнительно к данным:
 *   `degraded: true` — чтение памяти не удалось; это ОШИБКА, а не пустая память;
 *   `unreadable: N` — сколько свёрток не разобралось (показываем, а не прячем).
 *
 *   import { initMemoryViews, destroyMemoryViews } from "./memory_views.js";
 *   initMemoryViews({ mount, api, sheet, onError });
 */

import { Space3D } from "./space3d.js";

const GRAPH_PATH = "/memory/graph";
const DAG_PATH = "/memory/compact-dag";
const CODE_GRAPH_PATH = "/code/graph";   // api() уже префиксует /api/praxis/v1

/* ── силовая раскладка (плоский откат Constellation2D) ─────────────── */
const MAX_NODES = 240;          // рендерим не больше — телефон должен дышать
const MAX_LINKS = 900;
const CUTOFF = 300;             // радиус отталкивания (мировые единицы)
const REP = 7200;               // сила отталкивания
const SPRING = 0.03;            // жёсткость пружины связи
const CENTER = 0.0021;          // стягивание к центру (линейно по расстоянию)
const DAMP = 0.84;
const MAX_V = 34;
const PRESTEPS = 48;            // прогон до первого кадра — чтобы не «взрыв»
const STEPS_PER_FRAME = 2;
const ALPHA_DECAY = 0.985;
const ALPHA_MIN = 0.02;
// prefers-reduced-motion: досчитываем разом, но на UI-потоке телефона.
// 180 шагов → alpha ≈ 0.985^180 ≈ 0.067: визуально уже улёгшаяся раскладка.
const STATIC_STEPS = 180;
const DPR_CAP = 2;

/* ── дендрограмма ──────────────────────────────────────────────────── */
const SLOT = 44;                // ширина слота компакта
const NODE_W = 32;
const NODE_H = 26;
const ROW_H = 78;
const PAD_X = 22;
const PAD_TOP = 18;
const EPISODE_ROW = 46;
const EVENTS_ROW = 54;
const MAX_COMPACTS_PER_TIER = 400;
const EPISODE_COL = 14;         // шаг колонок точек-эпизодов (было 9 — цели налезали)
const MAX_CHAT_CHIPS = 24;      // потоков сотни, свёрнутых — единицы; чипы не резиновые
const EPISODE_ROW_STEP = 16;    // шаг рядов точек-эпизодов
const EPISODE_HIT_R = 8;        // радиус невидимой цели тапа (было 11)
const EPISODES_PER_COMPACT = 6; // больше — сворачиваем в отметку «+N»

const SVG_NS = "http://www.w3.org/2000/svg";
const TAU = Math.PI * 2;

let active = null;

/* ═══════════════════════════ мелочи ═══════════════════════════════ */

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "" && text !== null && text !== undefined) node.textContent = String(text);
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null) continue;
    node.setAttribute(key, String(value));
  }
  return node;
}

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

/* ключ ячейки сетки отталкивания: число, а не строка.
   Строковый ключ `${gx},${gy}` давал ~10^6 аллокаций за прогон на телефоне. */
function cellKey(gx, gy) {
  return (Math.imul(gx, 73856093) ^ Math.imul(gy, 19349663)) | 0;
}

/* детерминированный «шум» — раскладка воспроизводима между запусками */
function seeded(index, salt = 0) {
  const value = Math.sin((index + 1) * 91.173 + salt * 17.719) * 43758.5453;
  return value - Math.floor(value);
}

function readVars() {
  const style = getComputedStyle(document.documentElement);
  const rgb = (name, fallback) => {
    const parts = style.getPropertyValue(name).trim().split(",").map((part) => Number(part.trim()));
    return parts.length === 3 && parts.every(Number.isFinite) ? parts : fallback;
  };
  const plain = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
  return {
    people: rgb("--violet-rgb", [157, 124, 255]),
    topic: rgb("--cyan-rgb", [85, 216, 232]),
    accent: rgb("--gold-rgb", [241, 201, 121]),
    link: rgb("--blue-rgb", [111, 165, 255]),
    text: plain("--text", "#f4f6fb"),
    textSoft: plain("--text-soft", "#b7bece"),
    muted: plain("--muted", "#7e8799"),
    light: (document.documentElement.dataset.theme || "dark") === "light",
  };
}

function rgba(rgb, alpha) {
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
}

function stamp(value) {
  if (value === null || value === undefined || value === "") return null;
  let date;
  if (typeof value === "number" || /^\d+(\.\d+)?$/.test(String(value))) {
    const numeric = Number(value);
    date = new Date(numeric > 1e11 ? numeric : numeric * 1000);
  } else {
    date = new Date(String(value));
  }
  return Number.isFinite(date.getTime()) ? date : null;
}

let timeFormat = null;
function shortTime(value) {
  const date = stamp(value);
  if (!date) return "—";
  if (!timeFormat) {
    try {
      timeFormat = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
    } catch (_) {
      timeFormat = { format: (d) => d.toISOString().slice(0, 16).replace("T", " ") };
    }
  }
  return timeFormat.format(date);
}

function stateBox(kind, title, detail = "") {
  const box = el("div", `mem-state mem-state--${kind}`);
  box.append(el("strong", "", title));
  if (detail) box.append(el("small", "", textOf(detail, 240)));
  return box;
}

function factCell(label, value) {
  const cell = el("div", "mem-fact");
  cell.append(el("small", "", label), el("strong", "", value));
  return cell;
}

/** Подпись человеку — по виду узла. Память и код говорят на разных языках. */
function kindWord(kind) {
  return {
    people: "человек", person: "человек", topic: "тема",
    module: "модуль", file: "файл", package: "пакет", dir: "каталог", directory: "каталог",
    test: "тесты", tests: "тесты", entry: "точка входа", root: "корень", lib: "библиотека",
    skill: "навык", soul: "душа", doc: "документ", web: "веб", other: "прочее",
  }[String(kind || "").toLowerCase()] || String(kind || "узел");
}

/** Подпись ребру графа кода: imports / calls / reads / indexed. */
function linkWord(kind) {
  const key = String(kind || "").toLowerCase();
  return {
    imports: "импортирует", calls: "вызывает", reads: "читает", indexed: "в индексе",
  }[key] || key;
}

/* «1 вызов», «2 вызова», «5 вызовов» — счётчик, который не режет глаз по-русски */
function plural(count, one, few, many) {
  const rest = Math.abs(count) % 100;
  const last = rest % 10;
  if (rest > 10 && rest < 20) return many;
  if (last > 1 && last < 5) return few;
  return last === 1 ? one : many;
}

/* ═══════════════════ пространство (Space3D) ═══════════════════════ */

/**
 * Хост 3D-сцены: DOM, подписи, легенда, досье. Вся геометрия — в Space3D.
 * Если WebGL2 нет, молча переключается на плоский Constellation2D ниже:
 * «не поддерживается» на чужом телефоне дороже, чем старая картинка.
 */
class Constellation {
  /**
   * @param {{sheet:Function, onError:Function, ariaLabel:string, flavour?:string,
   *          onEmptyTap?:Function, emptyTitle:string, emptyHint:string,
   *          errorTitle:string}} config
   */
  constructor(host, config = {}) {
    this.host = host;
    this.config = config;
    this.sheet = config.sheet;
    this.onError = config.onError;
    this.destroyed = false;
    this.fallback = false;      // true → рисует старый 2D-класс

    this.stage = el("div", "mem-stage mem-stage--space");
    this.canvas = el("canvas", "mem-canvas");
    this.canvas.setAttribute("role", "img");
    this.canvas.setAttribute("aria-label", config.ariaLabel || "Пространство памяти: люди и темы");
    this.labels = el("div", "mem-labels");
    this.labels.setAttribute("aria-hidden", "true");   // текст уже есть в досье
    this.status = el("div", "mem-stage__status");
    this.legend = el("div", "mem-legend");
    this.stage.append(this.canvas, this.labels, this.legend, this.status);

    this.recenter = el("button", "mem-chip mem-chip--ghost", "В центр");
    this.recenter.type = "button";
    this.recenter.addEventListener("click", () => {
      if (this.fallback) this.legacy?.recenterView();
      else this.space?.recenter();
    });

    // Вторая дверь в полноэкранный режим — рядом с первой (тап по пустому месту).
    // Кнопка нужна не для красоты: тапом сцену не развернуть с клавиатуры, а на
    // телефоне не всякий догадается ткнуть в пустоту. Обработчик вешает хост —
    // сцена не знает ни про слой, ни про то, куда её переносят.
    this.expand = el("button", "mem-chip mem-chip--ghost", "Развернуть");
    this.expand.type = "button";
    this.immersive = false;

    this.pool = [];             // переиспользуемые DOM-подписи, без пересоздания
    this.space = null;
    this.legacy = null;

    try {
      this.space = new Space3D(this.canvas, {
        ariaLabel: config.ariaLabel,
        onSelect: (node) => this.openDossier(node.id),
        onLabels: (list) => this.paintLabels(list),
        onEmptyTap: () => this.config.onEmptyTap?.(),
      });
    } catch (_) {
      // Нет WebGL2 — не «сломано», а «рисуем как раньше». Молча и без потери вида.
      this.space = null;
      this.fallback = true;
      this.legacy = new Constellation2D(host, config);
      this.canvas.remove();
      this.labels.remove();
    }
  }

  setStatus(node) {
    if (this.fallback) return this.legacy.setStatus(node);
    this.status.replaceChildren(...(node ? [node] : []));
    this.status.hidden = !node;
    return undefined;
  }

  /* ── подписи: обычный DOM поверх холста, текст в WebGL не рисуется ── */

  paintLabels(list) {
    if (this.destroyed) return;
    for (let i = 0; i < list.length; i += 1) {
      let node = this.pool[i];
      if (!node) {
        node = el("span", "mem-label");
        this.pool.push(node);
        this.labels.append(node);
      }
      const item = list[i];
      node.textContent = item.label;
      node.hidden = false;
      node.classList.toggle("is-selected", Boolean(item.selected));
      node.dataset.tone = item.colorKey;
      // CSSOM, а не атрибут style в разметке: `style-src 'self'` этого не касается —
      // CSP покрывает инлайновые style-теги и style-атрибуты в HTML, но не запись
      // свойств из скрипта.
      node.style.setProperty("--mem-label-x", `${Math.round(item.x)}px`);
      node.style.setProperty("--mem-label-y", `${Math.round(item.y + item.radius + 7)}px`);
      node.style.setProperty("--mem-label-a", item.alpha.toFixed(2));
    }
    for (let i = list.length; i < this.pool.length; i += 1) this.pool[i].hidden = true;
  }

  /* ── данные ─────────────────────────────────────────────────────── */

  load(payload) {
    if (this.fallback) return this.legacy.load(payload);
    if (this.destroyed || !this.space) return null;
    const stats = this.space.setGraph(payload);

    // degraded → память НЕ прочиталась. Пустой граф здесь был бы ложью о ней.
    if (stats.degraded) {
      this.legend.hidden = true;
      this.paintLabels([]);
      this.setStatus(stateBox(
        "error",
        this.config.errorTitle || "Граф памяти не прочитался",
        "Сервер вернул неполный ответ. Это сбой чтения, а не пустая память — попробуй позже.",
      ));
      return stats;
    }
    if (stats.empty) {
      this.legend.hidden = true;
      this.paintLabels([]);
      this.setStatus(stateBox("empty", this.config.emptyTitle, this.config.emptyHint));
      return stats;
    }

    this.legend.replaceChildren(...this.legendFor(stats));
    this.legend.hidden = false;
    this.setStatus(null);
    return stats;
  }

  /** Легенда считается по факту загруженных узлов — она честна и для кода. */
  legendFor(stats) {
    const byKind = new Map();
    for (const node of this.space.nodes) {
      const row = byKind.get(node.kind);
      if (row) row.count += 1;
      else byKind.set(node.kind, { count: 1, tone: node.colorKey });
    }
    const items = [...byKind.entries()]
      .sort((a, b) => b[1].count - a[1].count)
      .slice(0, 3)
      .map(([kind, row]) => {
        const item = el("span", "mem-legend__item");
        item.dataset.tone = row.tone;
        item.append(el("i", "mem-legend__dot"), el("span", "", `${kindWord(kind)} · ${row.count}`));
        return item;
      });
    const meta = [`${stats.links} связей`];
    if (stats.dropped) meta.push(`+${stats.dropped} не показано`);
    if (stats.linksDropped) meta.push(`+${stats.linksDropped} связей не показано`);
    items.push(el("span", "mem-legend__meta", meta.join(" · ")));
    return items;
  }

  /** Рёбра узла с направлением: normalise() его теряет, а `links` — нет. */
  edgesOf(id) {
    const out = [];
    const inc = [];
    const nodes = this.space.nodes;
    for (const link of this.space.links) {
      const a = nodes[link.a];
      const b = nodes[link.b];
      if (!a || !b) continue;
      const row = { kind: link.kind, label: link.label, weight: link.weight, calls: link.calls };
      if (a.id === id) out.push({ ...row, id: b.id });
      else if (b.id === id) inc.push({ ...row, id: a.id });
    }
    const byWeight = (x, y) => (y.weight || 0) - (x.weight || 0)
      || (this.space.getNode(y.id)?.degree || 0) - (this.space.getNode(x.id)?.degree || 0);
    out.sort(byWeight);
    inc.sort(byWeight);
    return { out, inc };
  }

  /** Список соседей кнопками — одинаковый для памяти и кода, разный только текст. */
  neighbourList(items, caption) {
    const box = el("div", "mem-neighbours");
    for (const item of items.slice(0, 60)) {
      const other = this.space.getNode(item.id);
      if (!other) continue;
      const button = el("button", "mem-neighbour");
      button.type = "button";
      button.dataset.tone = other.colorKey;
      const line = el("span", "mem-neighbour__body");
      line.append(el("strong", "", other.label), el("small", "", caption(item, other)));
      button.append(el("i", "mem-neighbour__dot"), line, el("span", "mem-neighbour__deg", String(other.degree)));
      button.addEventListener("click", () => this.openDossier(other.id));
      box.append(button);
    }
    return box;
  }

  /* ── досье: ТО ЖЕ, что было; сменился только источник данных ─────── */

  openDossier(id) {
    if (this.fallback) return this.legacy.openDossier(id);
    // кнопка соседа могла пережить уход из раздела — сцены уже нет, шторки тоже
    if (this.destroyed || !this.space) return undefined;
    const node = this.space.getNode(id);
    if (!node || typeof this.sheet !== "function") return undefined;
    this.space.focus(id);

    const code = this.space.mode === "code";
    const body = el("div", "mem-dossier");
    const facts = el("div", "mem-facts");
    facts.append(factCell("тип", kindWord(node.kind)), factCell("связей", String(node.degree)));
    if (node.loc) facts.append(factCell("строк", String(node.loc)));
    if (node.funcs) facts.append(factCell("функций", String(node.funcs)));
    if (node.classes) facts.append(factCell("классов", String(node.classes)));
    if (node.size) facts.append(factCell("байт", String(node.size)));
    body.append(facts);

    if (node.doc) {
      body.append(el("h4", "mem-dossier__title", "Док"));
      body.append(el("p", "mem-summary", node.doc));
    }

    if (code) {
      // У кода ребро направленное: «что тянет она» и «кто тянет её» — разные ответы.
      const { out, inc } = this.edgesOf(id);
      const caption = (item) => {
        const parts = [linkWord(item.kind) || "связь"];
        if (item.calls) parts.push(`${item.calls} ${plural(item.calls, "вызов", "вызова", "вызовов")}`);
        else if (item.weight > 1) parts.push(`вес ${item.weight}`);
        return parts.join(" · ");
      };

      body.append(el("h4", "mem-dossier__title", `Тянет к себе · ${out.length}`));
      body.append(out.length
        ? this.neighbourList(out, caption)
        : stateBox("empty", "Ничего не импортирует", "В собранном графе у этого узла нет исходящих рёбер."));

      body.append(el("h4", "mem-dossier__title", `Зависят от неё · ${inc.length}`));
      body.append(inc.length
        ? this.neighbourList(inc, caption)
        : stateBox("empty", "На неё никто не ссылается", "В собранном графе у этого узла нет входящих рёбер."));

      this.sheet(node.label, body);
      return undefined;
    }

    const seen = new Set();
    const list = this.space.getNeighbours(id).filter((item) => {
      if (seen.has(item.id)) return false;
      seen.add(item.id);
      return true;
    });
    list.sort((a, b) => (this.space.getNode(b.id)?.degree || 0) - (this.space.getNode(a.id)?.degree || 0));

    body.append(el("h4", "mem-dossier__title", `Соседи · ${list.length}`));
    body.append(list.length
      ? this.neighbourList(list, (item, other) => item.label || kindWord(other.kind))
      : stateBox("empty", "Одиночный узел", "У этого узла пока нет рёбер в собранном графе."));

    this.sheet(node.label, body);
    return undefined;
  }

  mountInto(container) {
    if (this.fallback) {
      this.legacy.mountInto(container);
      return this.recenter;
    }
    container.append(this.stage);
    return this.recenter;
  }

  /** Тот .mem-stage, который РЕАЛЬНО в документе. У отката без WebGL2 он свой, а
      наш при этом остаётся пустым и никуда не смонтирован — полноэкранный слой,
      перепутав их, унёс бы на весь экран пустую коробку. */
  stageEl() {
    return this.fallback && this.legacy ? this.legacy.stage : this.stage;
  }

  /** Полноэкранный режим. Слой строит хост; сцена лишь меняет правила жеста и кадр. */
  setImmersive(on) {
    this.immersive = Boolean(on);
    if (this.space) this.space.setImmersive(this.immersive);
    else this.legacy?.setImmersive(this.immersive);
  }

  /* Ресайз почти всегда ловит ResizeObserver самого холста; ручной вызов нужен
     только при монтировании в display:none-секцию — как и в плоской версии. */
  resize() {
    this.space?._onResize?.();
    this.legacy?.resize?.();
  }

  refreshTheme() {
    this.space?.refreshTheme();
    if (this.legacy) {
      this.legacy.colors = readVars();
      this.legacy.request();
    }
  }

  destroy() {
    this.destroyed = true;
    this.space?.destroy();          // RAF, слушатели, ResizeObserver, GL-буферы и контекст
    this.space = null;
    this.legacy?.destroy();
    this.legacy = null;
    this.pool = [];
    this.labels.replaceChildren();
    this.stage.remove();
  }
}

/* ═════════════ плоское созвездие — откат без WebGL2 ═══════════════ */

class Constellation2D {
  constructor(host, config = {}) {
    const { sheet, onError } = config;
    this.host = host;
    this.config = config;
    this.sheet = sheet;
    this.onError = onError;
    this.destroyed = false;
    this.immersive = false;     // см. Space3D: развёрнутой сцене жест отдавать некому

    this.stage = el("div", "mem-stage");
    this.canvas = el("canvas", "mem-canvas");
    this.canvas.setAttribute("role", "img");
    this.canvas.setAttribute("aria-label", config.ariaLabel || "Созвездие памяти: люди и темы");
    this.status = el("div", "mem-stage__status");
    this.legend = el("div", "mem-legend");
    this.stage.append(this.canvas, this.legend, this.status);

    this.recenter = el("button", "mem-chip mem-chip--ghost", "В центр");
    this.recenter.type = "button";

    this.ctx = this.canvas.getContext("2d", { alpha: true });
    this.dpr = 1;
    this.width = 0;
    this.height = 0;

    this.nodes = [];
    this.links = [];
    this.byId = new Map();
    this.neighbours = new Map();
    this.colors = readVars();

    this.scale = 1;
    this.offsetX = 0;
    this.offsetY = 0;
    this.userMoved = false;
    this.hover = null;
    this.selected = null;

    this.alpha = 1;
    this.frame = 0;
    this.running = false;
    this.dirty = true;
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)");
    this.pointers = new Map();
    this.gesture = null;
    this.pinch = null;

    this._tick = this._tick.bind(this);
    this._onPointerDown = this._onPointerDown.bind(this);
    this._onPointerMove = this._onPointerMove.bind(this);
    this._onPointerUp = this._onPointerUp.bind(this);
    this._onWheel = this._onWheel.bind(this);
    this._onKey = this._onKey.bind(this);
    this._onMotionChange = this._onMotionChange.bind(this);

    this.canvas.tabIndex = 0;
    this.canvas.addEventListener("pointerdown", this._onPointerDown);
    this.canvas.addEventListener("pointermove", this._onPointerMove);
    this.canvas.addEventListener("pointerup", this._onPointerUp);
    this.canvas.addEventListener("pointercancel", this._onPointerUp);
    this.canvas.addEventListener("pointerleave", this._onPointerUp);
    this.canvas.addEventListener("wheel", this._onWheel, { passive: false });
    this.canvas.addEventListener("keydown", this._onKey);
    this.recenter.addEventListener("click", () => this.recenterView());
    this.reduced.addEventListener?.("change", this._onMotionChange);

    this.observer = typeof ResizeObserver === "function"
      ? new ResizeObserver(() => this.resize())
      : null;
    this.observer?.observe(this.stage);
    this._onResize = () => this.resize();
    if (!this.observer) window.addEventListener("resize", this._onResize, { passive: true });
  }

  get motionOff() {
    return this.reduced.matches;
  }

  /** Кнопка «В центр» — та же, что и у 3D-сцены, чтобы хост звал одно и то же. */
  recenterView() {
    this.userMoved = false;
    this.fit(true);
    this.request();
  }

  setStatus(node) {
    this.status.replaceChildren(...(node ? [node] : []));
    this.status.hidden = !node;
  }

  /* ── данные ──────────────────────────────────────────────────── */

  load(payload) {
    const rawNodes = Array.isArray(payload?.nodes) ? payload.nodes : [];
    const rawLinks = Array.isArray(payload?.links) ? payload.links : [];
    // degraded → память НЕ прочиталась. Пустой граф здесь был бы ложью о ней.
    if (payload?.degraded === true) {
      this.nodes = [];
      this.links = [];
      this.legend.hidden = true;
      this.setStatus(stateBox(
        "error",
        this.config.errorTitle || "Граф памяти не прочитался",
        "Сервер вернул неполный ответ. Это сбой чтения, а не пустая память — попробуй позже.",
      ));
      this.paint();
      return;
    }
    if (!rawNodes.length) {
      this.nodes = [];
      this.links = [];
      this.legend.hidden = true;
      this.setStatus(stateBox(
        "empty",
        this.config.emptyTitle || "Созвездие пусто",
        this.config.emptyHint || "Граф памяти ещё не собран — карты PEOPLE и TOPICS пока без узлов.",
      ));
      this.paint();
      return;
    }

    // Степень: граф кода её не объявляет — считаем по ПОЛНОМУ списку рёбер, до
    // обрезки, иначе ранжирование «кто важнее» соврало бы ровно на срезанных.
    const degrees = new Map();
    for (const link of rawLinks) {
      const from = String(link?.a ?? link?.source ?? "");
      const to = String(link?.b ?? link?.target ?? "");
      if (!from || !to || from === to) continue;
      degrees.set(from, (degrees.get(from) || 0) + 1);
      degrees.set(to, (degrees.get(to) || 0) + 1);
    }

    const ranked = rawNodes
      .filter((node) => node && node.id !== undefined && node.id !== null)
      .map((node) => {
        // Вид узла держим сырым (код говорит «skill»/«module»/«test»), а краски
        // здесь всего две — люди и всё остальное: это откат, а не вторая палитра.
        const kind = String(node.kind || (this.config.flavour === "code" ? "module" : "topic")).toLowerCase();
        const id = String(node.id);
        return {
          id,
          label: textOf(node.label ?? node.name ?? node.id, 120) || id,
          kind,
          people: kind === "people" || kind === "person",
          degree: node.degree === undefined
            ? (degrees.get(id) || 0)
            : Math.max(0, intOf(node.degree, 0)),
        };
      })
      .sort((a, b) => b.degree - a.degree)
      .slice(0, MAX_NODES);

    const maxDegree = ranked.reduce((acc, node) => Math.max(acc, node.degree), 1);
    const count = ranked.length;
    const spread = Math.max(240, Math.sqrt(count) * 96);

    this.nodes = ranked.map((node, index) => {
      // золотой угол — детерминированный старт без «комка» в центре
      const angle = index * 2.399963229728653;
      const radius = spread * Math.sqrt((index + 0.5) / count) + seeded(index, 3) * 12;
      return {
        ...node,
        r: clamp(4.6 + Math.sqrt(node.degree) * 3.3, 4.6, 26),
        weight: 1 + node.degree / Math.max(1, maxDegree),
        x: Math.cos(angle) * radius,
        y: Math.sin(angle) * radius,
        vx: 0,
        vy: 0,
        sx: 0,
        sy: 0,
      };
    });

    this.byId = new Map(this.nodes.map((node, index) => [node.id, index]));
    this.neighbours = new Map(this.nodes.map((node) => [node.id, []]));
    this.links = [];
    for (const link of rawLinks) {
      if (this.links.length >= MAX_LINKS) break;
      // память шлёт `a`/`b`, код — `source`/`target`: откат обязан понимать обе формы
      const a = this.byId.get(String(link?.a ?? link?.source ?? ""));
      const b = this.byId.get(String(link?.b ?? link?.target ?? ""));
      if (a === undefined || b === undefined || a === b) continue;
      const label = textOf(link.label ?? linkWord(link.kind) ?? "", 90);
      this.links.push({ a, b, label });
      this.neighbours.get(this.nodes[a].id).push({ id: this.nodes[b].id, label });
      this.neighbours.get(this.nodes[b].id).push({ id: this.nodes[a].id, label });
    }

    if (this.config.flavour === "code") {
      // У кода видов больше двух, а красок в откате две — точки-маркеры соврали бы,
      // поэтому легенда здесь только счётная, без цветных кружков.
      const byKind = new Map();
      for (const node of this.nodes) byKind.set(node.kind, (byKind.get(node.kind) || 0) + 1);
      const items = [...byKind.entries()]
        .sort((a, b) => b[1] - a[1])
        .slice(0, 3)
        .map(([kind, amount]) => el("span", "mem-legend__meta", `${kindWord(kind)} · ${amount}`));
      items.push(el("span", "mem-legend__meta", `${this.links.length} связей`));
      this.legend.replaceChildren(...items);
    } else {
      const people = this.nodes.filter((node) => node.people).length;
      this.legend.replaceChildren(
        this._legendItem("people", `люди · ${people}`),
        this._legendItem("topic", `темы · ${count - people}`),
        el("span", "mem-legend__meta", `${this.links.length} связей`),
      );
    }
    this.legend.hidden = false;
    this.setStatus(null);

    this.alpha = 1;
    for (let step = 0; step < PRESTEPS; step += 1) this.step(1);
    this.fit(true);

    if (this.motionOff) {
      // без анимации раскладку досчитываем разом — тем же остыванием, что и в кадрах,
      // иначе статичный вид разошёлся бы с анимированным
      for (let step = 0; step < STATIC_STEPS && this.alpha > ALPHA_MIN; step += 1) {
        this.step(this.alpha);
        this.alpha *= ALPHA_DECAY;
      }
      this.alpha = 0;
      this.fit(true);
      this.paint();
    } else {
      this.start();
    }
  }

  _legendItem(kind, label) {
    const item = el("span", `mem-legend__item mem-legend__item--${kind}`);
    item.append(el("i", "mem-legend__dot"), el("span", "", label));
    return item;
  }

  /* ── физика ──────────────────────────────────────────────────── */

  step(alpha) {
    const nodes = this.nodes;
    const n = nodes.length;
    if (!n) return;

    // сетка: отталкивание считаем только внутри радиуса CUTOFF → почти линейно
    const grid = new Map();
    for (let i = 0; i < n; i += 1) {
      const node = nodes[i];
      const key = cellKey(Math.floor(node.x / CUTOFF), Math.floor(node.y / CUTOFF));
      const bucket = grid.get(key);
      if (bucket) bucket.push(i); else grid.set(key, [i]);
    }

    for (let i = 0; i < n; i += 1) {
      const node = nodes[i];
      const cx = Math.floor(node.x / CUTOFF);
      const cy = Math.floor(node.y / CUTOFF);
      for (let gx = cx - 1; gx <= cx + 1; gx += 1) {
        for (let gy = cy - 1; gy <= cy + 1; gy += 1) {
          const bucket = grid.get(cellKey(gx, gy));
          if (!bucket) continue;
          for (let k = 0; k < bucket.length; k += 1) {
            const j = bucket[k];
            if (j <= i) continue;
            const other = nodes[j];
            let dx = node.x - other.x;
            let dy = node.y - other.y;
            let dist2 = dx * dx + dy * dy;
            if (dist2 > CUTOFF * CUTOFF) continue;
            if (dist2 < 0.01) {
              dx = (seeded(i, j) - 0.5) * 2;
              dy = (seeded(j, i) - 0.5) * 2;
              dist2 = dx * dx + dy * dy + 0.01;
            }
            const dist = Math.sqrt(dist2);
            const force = (REP * node.weight * other.weight * alpha) / dist2;
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;
            node.vx += fx;
            node.vy += fy;
            other.vx -= fx;
            other.vy -= fy;
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
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const rest = 58 + (a.r + b.r) * 1.7;
      const force = SPRING * (dist - rest) * alpha;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }

    for (let i = 0; i < n; i += 1) {
      const node = nodes[i];
      node.vx -= node.x * CENTER;
      node.vy -= node.y * CENTER;
      node.vx = clamp(node.vx * DAMP, -MAX_V, MAX_V);
      node.vy = clamp(node.vy * DAMP, -MAX_V, MAX_V);
      node.x += node.vx;
      node.y += node.vy;
    }
  }

  settled() {
    return this.alpha <= ALPHA_MIN;
  }

  start() {
    if (this.destroyed || this.running || this.motionOff) return;
    this.running = true;
    this.frame = requestAnimationFrame(this._tick);
  }

  stop() {
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.running = false;
  }

  request() {
    this.dirty = true;
    if (this.motionOff || this.settled()) {
      this.paint();
      return;
    }
    this.start();
  }

  _tick() {
    if (this.destroyed) return;
    if (this.width <= 1 || this.height <= 1) this.resize();
    if (!this.settled()) {
      for (let i = 0; i < STEPS_PER_FRAME; i += 1) {
        this.step(this.alpha);
        this.alpha *= ALPHA_DECAY;
      }
      if (!this.userMoved) this.fit(false);
      this.dirty = true;
    }
    if (this.dirty) this.paint();
    if (!this.settled()) {
      this.frame = requestAnimationFrame(this._tick);
    } else {
      this.stop();
    }
  }

  /* ── вид ─────────────────────────────────────────────────────── */

  /** Снимает размеры сцены. true — если что-то изменилось. Не рисует (нет рекурсии). */
  measure() {
    const rect = this.stage.getBoundingClientRect();
    const width = Math.max(1, Math.round(rect.width));
    const height = Math.max(1, Math.round(rect.height));
    const dpr = clamp(window.devicePixelRatio || 1, 1, DPR_CAP);
    if (width === this.width && height === this.height && dpr === this.dpr) return false;
    this.width = width;
    this.height = height;
    this.dpr = dpr;
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);
    return true;
  }

  /** ⚠ 03.08.2026. Раньше при `userMoved` смена размера не делала ничего, кроме
      перерисовки. offsetX/offsetY заданы в ПИКСЕЛЯХ холста, поэтому подвинутая
      картинка после разворота на весь экран оставалась прижатой к прежнему углу.
      Масштаб владельца не трогаем — он его выбрал; двигаем только начало координат
      на половину прироста, чтобы в центре кадра осталась та же точка мира. */
  resize() {
    const prevWidth = this.width;
    const prevHeight = this.height;
    if (!this.measure()) return;
    if (!this.userMoved) this.fit(true);
    else if (prevWidth > 1 && prevHeight > 1) {
      this.offsetX += (this.width - prevWidth) / 2;
      this.offsetY += (this.height - prevHeight) / 2;
    }
    this.paint();
  }

  /** Полноэкранный режим плоского отката — см. Space3D.setImmersive. */
  setImmersive(on) {
    const next = Boolean(on);
    if (next === this.immersive) return;
    this.immersive = next;
    this.gesture = null;
    this.pointers.clear();
    this.pinch = null;
    this.resize();
  }

  bounds() {
    if (!this.nodes.length) return { minX: -1, minY: -1, maxX: 1, maxY: 1 };
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of this.nodes) {
      minX = Math.min(minX, node.x - node.r);
      minY = Math.min(minY, node.y - node.r);
      maxX = Math.max(maxX, node.x + node.r);
      maxY = Math.max(maxY, node.y + node.r);
    }
    return { minX, minY, maxX, maxY };
  }

  fit(instant) {
    if (!this.nodes.length || this.width <= 1 || this.height <= 1) return;
    const { minX, minY, maxX, maxY } = this.bounds();
    const pad = 34;
    const scale = clamp(
      Math.min((this.width - pad * 2) / Math.max(1, maxX - minX), (this.height - pad * 2) / Math.max(1, maxY - minY)),
      0.12,
      2.4,
    );
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const offsetX = this.width / 2 - cx * scale;
    const offsetY = this.height / 2 - cy * scale;
    if (instant) {
      this.scale = scale;
      this.offsetX = offsetX;
      this.offsetY = offsetY;
    } else {
      const ease = 0.12;
      this.scale += (scale - this.scale) * ease;
      this.offsetX += (offsetX - this.offsetX) * ease;
      this.offsetY += (offsetY - this.offsetY) * ease;
    }
  }

  toScreen(node) {
    return { x: node.x * this.scale + this.offsetX, y: node.y * this.scale + this.offsetY };
  }

  paint() {
    const ctx = this.ctx;
    if (!ctx) return;
    // Секция #view-memory стартует в display:none — при монтировании сцена 1×1.
    // Уведомление ResizeObserver может и не прийти (или прийти позже), поэтому
    // перемеряем сами на любой перерисовке: пустой холст недопустим.
    if ((this.width <= 1 || this.height <= 1) && this.measure() && !this.userMoved) this.fit(true);
    if (this.width <= 1 || this.height <= 1) return;
    this.dirty = false;
    const dpr = this.dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);
    if (!this.nodes.length) return;

    const colors = this.colors;
    const scale = this.scale;

    for (const node of this.nodes) {
      const point = this.toScreen(node);
      node.sx = point.x;
      node.sy = point.y;
    }

    // связи — тонкая паутина под пигментом
    ctx.lineWidth = clamp(0.7 * scale, 0.45, 1.4);
    ctx.strokeStyle = rgba(colors.link, colors.light ? 0.2 : 0.15);
    ctx.beginPath();
    for (const link of this.links) {
      const a = this.nodes[link.a];
      const b = this.nodes[link.b];
      if (!this._visible(a, 60) && !this._visible(b, 60)) continue;
      ctx.moveTo(a.sx, a.sy);
      ctx.lineTo(b.sx, b.sy);
    }
    ctx.stroke();

    // подсветка выбранного узла: его связи ярче
    const focus = this.selected || this.hover;
    if (focus) {
      const index = this.byId.get(focus);
      if (index !== undefined) {
        ctx.lineWidth = clamp(1.3 * scale, 0.8, 2.4);
        ctx.strokeStyle = rgba(colors.accent, 0.44);
        ctx.beginPath();
        for (const link of this.links) {
          if (link.a !== index && link.b !== index) continue;
          const a = this.nodes[link.a];
          const b = this.nodes[link.b];
          ctx.moveTo(a.sx, a.sy);
          ctx.lineTo(b.sx, b.sy);
        }
        ctx.stroke();
      }
    }

    // капли пигмента: аддитивное свечение + плотное ядро
    ctx.globalCompositeOperation = "lighter";
    for (const node of this.nodes) {
      if (!this._visible(node, 40)) continue;
      const rgb = node.people ? colors.people : colors.topic;
      const radius = Math.max(2.4, node.r * scale);
      const glow = radius * (node.id === focus ? 4.4 : 3.3);
      const gradient = ctx.createRadialGradient(node.sx, node.sy, 0, node.sx, node.sy, glow);
      const peak = colors.light ? 0.3 : 0.46;
      gradient.addColorStop(0, rgba(rgb, node.id === focus ? peak + 0.2 : peak));
      gradient.addColorStop(0.34, rgba(rgb, peak * 0.36));
      gradient.addColorStop(1, rgba(rgb, 0));
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.arc(node.sx, node.sy, glow, 0, TAU);
      ctx.fill();
    }
    ctx.globalCompositeOperation = "source-over";

    for (const node of this.nodes) {
      if (!this._visible(node, 40)) continue;
      const rgb = node.people ? colors.people : colors.topic;
      const radius = Math.max(1.8, node.r * scale * 0.52);
      ctx.fillStyle = rgba(rgb, colors.light ? 0.92 : 0.86);
      ctx.beginPath();
      ctx.arc(node.sx, node.sy, radius, 0, TAU);
      ctx.fill();
      if (node.id === focus) {
        ctx.strokeStyle = rgba(colors.accent, 0.9);
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(node.sx, node.sy, radius + 4.5, 0, TAU);
        ctx.stroke();
      }
    }

    // подписи — только там, где они читаются
    const labelCut = this.nodes.length > 90 ? 9.5 : 6;
    ctx.font = "600 10.5px Inter, ui-sans-serif, -apple-system, 'Segoe UI', sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = colors.textSoft;
    for (const node of this.nodes) {
      if (!this._visible(node, 0)) continue;
      if (node.r * scale < labelCut && node.id !== focus) continue;
      const label = node.label.length > 22 ? `${node.label.slice(0, 21)}…` : node.label;
      ctx.fillText(label, node.sx, node.sy + Math.max(2.4, node.r * scale * 0.52) + 5);
    }
  }

  _visible(node, margin) {
    return node.sx > -margin && node.sx < this.width + margin && node.sy > -margin && node.sy < this.height + margin;
  }

  /* ── жесты ───────────────────────────────────────────────────── */

  _local(event) {
    const rect = this.canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  _onPointerDown(event) {
    // захват указателя — удобство, а не условие: если браузер его не даёт,
    // жест обязан продолжить жить (иначе тап и панорама умирают молча)
    try { this.canvas.setPointerCapture?.(event.pointerId); } catch (_) { /* не критично */ }
    const point = this._local(event);
    this.pointers.set(event.pointerId, point);
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
        panning: false,
      };
    } else if (this.pointers.size === 2) {
      this.gesture = null;
      this.pinch = this._pinchState();
    }
  }

  _pinchState() {
    const [a, b] = Array.from(this.pointers.values());
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    return {
      dist: Math.hypot(dx, dy) || 1,
      cx: (a.x + b.x) / 2,
      cy: (a.y + b.y) / 2,
      scale: this.scale,
      offsetX: this.offsetX,
      offsetY: this.offsetY,
    };
  }

  _onPointerMove(event) {
    const point = this._local(event);
    if (!this.pointers.has(event.pointerId)) {
      const hit = this.hitTest(point.x, point.y);
      const id = hit ? hit.id : null;
      if (id !== this.hover) {
        this.hover = id;
        this.canvas.style.cursor = id ? "pointer" : "grab";
        this.request();
      }
      return;
    }
    this.pointers.set(event.pointerId, point);

    if (this.pointers.size >= 2 && this.pinch) {
      const next = this._pinchState();
      const ratio = clamp(next.dist / this.pinch.dist, 0.2, 5);
      const scale = clamp(this.pinch.scale * ratio, 0.1, 3.2);
      this.offsetX = next.cx - ((this.pinch.cx - this.pinch.offsetX) / this.pinch.scale) * scale;
      this.offsetY = next.cy - ((this.pinch.cy - this.pinch.offsetY) / this.pinch.scale) * scale;
      this.scale = scale;
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

    // ⚠ 03.08.2026. Одним пальцем панорамируем только явно горизонтальный жест —
    // и только пока сцена свёрнута. Вертикаль отдаём странице (`touch-action:
    // pan-y` на .mem-canvas). Прежняя редакция пережила правку CSS, где `pan-y`
    // заменили на `none`, и вертикальный свайп перестал делать что-либо вообще:
    // страница его не получала, сцена его бросала. Развёрнутая (immersive) сцена
    // занимает экран целиком — там жест некому отдавать, панорамируем обе оси.
    // Двумя пальцами — это pinch, он обработан выше. Мышь/перо — как раньше.
    if (!gesture.panning) {
      const totalDx = point.x - gesture.startX;
      const totalDy = point.y - gesture.startY;
      if (gesture.touch && !this.immersive && Math.abs(totalDx) <= Math.abs(totalDy)) return;
      gesture.panning = true;
    }

    this.offsetX += dx;
    this.offsetY += dy;
    this.userMoved = true;
    this.request();
  }

  _onPointerUp(event) {
    const point = this.pointers.get(event.pointerId);
    this.pointers.delete(event.pointerId);
    if (this.pointers.size < 2) this.pinch = null;
    const gesture = this.gesture;
    if (!gesture || gesture.id !== event.pointerId) return;
    this.gesture = null;
    if (!point) return;
    const quick = performance.now() - gesture.at < 700;
    if (gesture.moved <= 6 && quick && event.type === "pointerup") {
      const hit = this.hitTest(point.x, point.y);
      if (hit) this.openDossier(hit.id);
      else if (this.config.onEmptyTap) {
        // Тап мимо узла разворачивает сцену. У плоского отката выделения нет,
        // поэтому спорить не с кем — в отличие от 3D, где эта же ветка сначала
        // снимает подсветку.
        try {
          this.config.onEmptyTap();
        } catch (_) { /* хост не обязан переживать наши ошибки */ }
      }
    }
  }

  _onWheel(event) {
    if (!this.nodes.length) return;
    event.preventDefault();
    const point = this._local(event);
    const factor = Math.exp(-clamp(event.deltaY, -120, 120) * 0.0022);
    const scale = clamp(this.scale * factor, 0.1, 3.2);
    this.offsetX = point.x - ((point.x - this.offsetX) / this.scale) * scale;
    this.offsetY = point.y - ((point.y - this.offsetY) / this.scale) * scale;
    this.scale = scale;
    this.userMoved = true;
    this.request();
  }

  _onKey(event) {
    const stepSize = 28;
    const map = { ArrowLeft: [stepSize, 0], ArrowRight: [-stepSize, 0], ArrowUp: [0, stepSize], ArrowDown: [0, -stepSize] };
    if (map[event.key]) {
      event.preventDefault();
      this.offsetX += map[event.key][0];
      this.offsetY += map[event.key][1];
      this.userMoved = true;
      this.request();
      return;
    }
    if (event.key === "+" || event.key === "=" || event.key === "-") {
      event.preventDefault();
      const factor = event.key === "-" ? 0.86 : 1.16;
      this.scale = clamp(this.scale * factor, 0.1, 3.2);
      this.userMoved = true;
      this.request();
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      this.userMoved = false;
      this.fit(true);
      this.request();
    }
  }

  _onMotionChange() {
    if (this.motionOff) {
      this.stop();
      // тот же ОГРАНИЧЕННЫЙ прогон, что и в load(): без верхней границы этот цикл
      // держал UI-поток столько, сколько скажет alpha
      for (let step = 0; step < STATIC_STEPS && this.alpha > ALPHA_MIN; step += 1) {
        this.step(this.alpha);
        this.alpha *= ALPHA_DECAY;
      }
      if (!this.userMoved) this.fit(true);
      this.paint();
    } else {
      this.request();
    }
  }

  hitTest(x, y) {
    let best = null;
    let bestDist = Infinity;
    for (const node of this.nodes) {
      const radius = Math.max(node.r * this.scale, 15);
      const dist = Math.hypot(node.sx - x, node.sy - y);
      if (dist <= radius && dist < bestDist) {
        best = node;
        bestDist = dist;
      }
    }
    return best;
  }

  focusNode(id) {
    const index = this.byId.get(id);
    if (index === undefined) return;
    const node = this.nodes[index];
    this.offsetX = this.width / 2 - node.x * this.scale;
    this.offsetY = this.height / 2 - node.y * this.scale;
    this.userMoved = true;
    this.selected = id;
    this.request();
  }

  openDossier(id) {
    const index = this.byId.get(id);
    if (index === undefined || typeof this.sheet !== "function") return;
    const node = this.nodes[index];
    this.selected = id;
    this.focusNode(id);

    const body = el("div", "mem-dossier");
    const facts = el("div", "mem-facts");
    facts.append(
      factCell("тип", kindWord(node.kind)),
      factCell("связей", String(node.degree)),
      factCell("в графе", String(this.neighbours.get(id)?.length || 0)),
    );
    body.append(facts);

    const seen = new Set();
    const list = (this.neighbours.get(id) || []).filter((item) => {
      if (seen.has(item.id)) return false;
      seen.add(item.id);
      return true;
    });
    list.sort((a, b) => {
      const na = this.nodes[this.byId.get(a.id)];
      const nb = this.nodes[this.byId.get(b.id)];
      return (nb?.degree || 0) - (na?.degree || 0);
    });

    body.append(el("h4", "mem-dossier__title", `Соседи · ${list.length}`));
    if (!list.length) {
      body.append(stateBox("empty", "Одиночный узел", "У этого узла пока нет рёбер в собранном графе."));
    } else {
      const ul = el("div", "mem-neighbours");
      for (const item of list.slice(0, 60)) {
        const other = this.nodes[this.byId.get(item.id)];
        if (!other) continue;
        const button = el("button", `mem-neighbour${other.people ? " mem-neighbour--people" : ""}`);
        button.type = "button";
        const line = el("span", "mem-neighbour__body");
        line.append(el("strong", "", other.label), el("small", "", item.label || kindWord(other.kind)));
        button.append(el("i", "mem-neighbour__dot"), line, el("span", "mem-neighbour__deg", String(other.degree)));
        button.addEventListener("click", () => this.openDossier(other.id));
        ul.append(button);
      }
      body.append(ul);
    }

    this.sheet(node.label, body);
  }

  mountInto(container) {
    container.append(this.stage);
    return this.recenter;
  }

  destroy() {
    this.destroyed = true;
    this.stop();
    this.canvas.removeEventListener("pointerdown", this._onPointerDown);
    this.canvas.removeEventListener("pointermove", this._onPointerMove);
    this.canvas.removeEventListener("pointerup", this._onPointerUp);
    this.canvas.removeEventListener("pointercancel", this._onPointerUp);
    this.canvas.removeEventListener("pointerleave", this._onPointerUp);
    this.canvas.removeEventListener("wheel", this._onWheel);
    this.canvas.removeEventListener("keydown", this._onKey);
    this.reduced.removeEventListener?.("change", this._onMotionChange);
    this.observer?.disconnect();
    if (!this.observer) window.removeEventListener("resize", this._onResize);
    this.nodes = [];
    this.links = [];
  }
}

/* ═══════════════════════ дендрограмма ═════════════════════════════ */

class Dendrogram {
  constructor({ api, sheet, onError }) {
    this.api = api;
    this.sheet = sheet;
    this.onError = onError;
    this.destroyed = false;
    this.chats = [];
    this.chatId = "";
    this.token = 0;
    this.eventTotal = 0;   // глобальный счёт событий из обзора (не по чату!)

    this.chips = el("div", "mem-chips");
    this.scroll = el("div", "mem-scroll");
    this.summary = el("div", "mem-dag__summary");
    this.note = el("p", "mem-dag__note");
    this.note.hidden = true;
    this.root = el("div", "mem-dag");
    this.root.append(this.chips, this.summary, this.note, this.scroll);

    this._onClick = this._onClick.bind(this);
    this._onKey = this._onKey.bind(this);
    this.scroll.addEventListener("click", this._onClick);
    this.scroll.addEventListener("keydown", this._onKey);
  }

  setBody(node) {
    this.scroll.replaceChildren(node);
  }

  setNote(text) {
    this.note.textContent = text || "";
    this.note.hidden = !text;
  }

  async load() {
    this.setBody(stateBox("loading", "Читаю свёртки", "Компакты и эпизоды по чатам."));
    let payload;
    try {
      payload = await this.api(DAG_PATH);
    } catch (error) {
      this.onError?.(error);
      this.setNote("");
      this.setBody(stateBox("error", "Свёртки не прочитались", error?.message || "Запрос не прошёл."));
      return;
    }
    if (this.destroyed) return;

    // degraded → обзор не прочитался. «Свёрток ещё нет» здесь было бы утверждением
    // о её памяти, которого никто не проверял.
    if (payload?.degraded === true) {
      this.chips.replaceChildren();
      this.chips.hidden = true;
      this.summary.replaceChildren();
      this.setNote("");
      this.setBody(stateBox(
        "error",
        "Свёртки не прочитались",
        "Сервер отдал неполный обзор памяти. Это сбой чтения, а не пустая память.",
      ));
      return;
    }

    this.chats = (Array.isArray(payload?.chats) ? payload.chats : [])
      .map((chat) => ({
        chat_id: String(chat?.chat_id ?? ""),
        compacts: intOf(chat?.compacts, 0),
        episodes: intOf(chat?.episodes, 0),
        hot: intOf(chat?.hot, 0),
        unreadable: intOf(chat?.unreadable, 0),
        tiers: chat?.tiers && typeof chat.tiers === "object" ? chat.tiers : {},
      }))
      // Потоков со «свежим» hot-окном сотни, но свёрнутая структура есть у единиц:
      // чат без единого компакта и эпизода в дендрограмме нарисовать нечем, поэтому
      // в переключатель он не попадает (иначе это 160 чипов пустых обещаний).
      .filter((chat) => chat.chat_id && (chat.compacts > 0 || chat.episodes > 0))
      .sort((a, b) => b.compacts - a.compacts || b.episodes - a.episodes)
      .slice(0, MAX_CHAT_CHIPS);

    const totals = payload?.totals && typeof payload.totals === "object" ? payload.totals : {};
    const eventTotal = intOf(totals.events, 0);
    // `unreadable` может прийти как итог сверху или как поле каждого чата
    const unreadable = Math.max(
      intOf(totals.unreadable, 0),
      intOf(payload?.unreadable, 0),
      this.chats.reduce((acc, chat) => acc + chat.unreadable, 0),
    );

    const facts = [
      factCell("событий во всех потоках", String(eventTotal)),
      factCell("компактов", String(intOf(totals.compacts, 0))),
      factCell("эпизодов", String(intOf(totals.episodes, 0))),
    ];
    if (unreadable > 0) facts.push(factCell("не прочиталось", String(unreadable)));
    this.summary.replaceChildren(...facts);
    this.eventTotal = eventTotal;

    if (!this.chats.length) {
      this.chips.replaceChildren();
      this.chips.hidden = true;
      this.setNote("");
      this.setBody(stateBox("empty", "Свёрток ещё нет", "Опыт пока не сворачивался: компактов ноль."));
      return;
    }

    this.chips.hidden = false;
    this.renderChips();
    await this.select(this.chats[0].chat_id);
  }

  renderChips() {
    const chips = this.chats.slice(0, 24).map((chat) => {
      const chip = el("button", "mem-chip");
      chip.type = "button";
      chip.dataset.chatId = chat.chat_id;
      chip.setAttribute("aria-pressed", String(chat.chat_id === this.chatId));
      chip.classList.toggle("is-active", chat.chat_id === this.chatId);
      const marks = [`${chat.compacts}к`, `${chat.episodes}э`];
      if (chat.hot) marks.push(`${chat.hot} горячих`);
      if (chat.unreadable) marks.push(`${chat.unreadable} не прочит.`);
      chip.classList.toggle("has-unreadable", chat.unreadable > 0);
      chip.append(el("strong", "", chat.chat_id), el("small", "", marks.join(" · ")));
      chip.addEventListener("click", () => this.select(chat.chat_id));
      return chip;
    });
    this.chips.replaceChildren(...chips);
  }

  async select(chatId) {
    if (this.destroyed) return;
    this.chatId = String(chatId || "");
    this.renderChips();
    this.setBody(stateBox("loading", "Строю страты", this.chatId));
    const token = ++this.token;
    let payload;
    try {
      payload = await this.api(`${DAG_PATH}?chat=${encodeURIComponent(this.chatId)}`);
    } catch (error) {
      if (token !== this.token || this.destroyed) return;
      this.onError?.(error);
      this.setNote("");
      this.setBody(stateBox("error", "Страты не прочитались", error?.message || "Запрос не прошёл."));
      return;
    }
    if (token !== this.token || this.destroyed) return;
    this.render(payload);
  }

  render(payload) {
    // degraded → страты не читались. Показываем сбой, а не «компактов нет».
    if (payload?.degraded === true) {
      this.setNote("");
      this.setBody(stateBox(
        "error",
        "Страты не прочитались",
        `Сервер отдал неполный ответ по «${this.chatId}». Это сбой чтения, а не пустой чат.`,
      ));
      return;
    }

    const compacts = (Array.isArray(payload?.compacts) ? payload.compacts : [])
      .map((row) => ({
        id: String(row?.id ?? ""),
        tier: Math.max(1, intOf(row?.tier, 1)),
        depth: intOf(row?.depth, 0),
        created_at: row?.created_at ?? null,
        event_count: intOf(row?.event_count, 0),
        continued: Boolean(row?.continued),
        legacy: Boolean(row?.legacy),
        degraded: Boolean(row?.degraded),
        first_ts: row?.first_ts ?? null,
        last_ts: row?.last_ts ?? null,
        source_compact_ids: Array.isArray(row?.source_compact_ids) ? row.source_compact_ids.map(String) : [],
        source_event_count: intOf(row?.source_event_count, 0),
        summary: textOf(row?.summary ?? "", 400),
      }))
      .filter((row) => row.id);

    if (!compacts.length) {
      this.setNote("");
      this.setBody(stateBox("empty", "В этом чате ещё нет компактов", "Опыт живёт в горячем окне и пока не сворачивался."));
      return;
    }

    const episodes = (Array.isArray(payload?.episodes) ? payload.episodes : [])
      .map((row) => ({
        id: String(row?.id ?? ""),
        compact_id: String(row?.compact_id ?? ""),
        status: textOf(row?.status ?? "", 40),
        first_ts: row?.first_ts ?? null,
        last_ts: row?.last_ts ?? null,
        title: textOf(row?.title ?? "", 160),
      }))
      .filter((row) => row.id);

    const edges = (Array.isArray(payload?.edges) ? payload.edges : []).filter((edge) => edge && edge.from && edge.to);
    const frontier = new Set((Array.isArray(payload?.frontier) ? payload.frontier : []).map(String));
    const totals = payload?.totals && typeof payload.totals === "object" ? payload.totals : {};

    this.compacts = new Map(compacts.map((row) => [row.id, row]));
    this.episodes = new Map(episodes.map((row) => [row.id, row]));
    this.frontier = frontier;

    // страты: тир 1 внизу, максимальный — вверху
    const tiers = Array.from(new Set(compacts.map((row) => row.tier))).sort((a, b) => a - b);
    const byTier = new Map(tiers.map((tier) => [tier, []]));
    const order = (row) => stamp(row.first_ts)?.getTime() ?? stamp(row.created_at)?.getTime() ?? 0;
    for (const row of compacts) byTier.get(row.tier).push(row);
    for (const tier of tiers) {
      byTier.get(tier).sort((a, b) => order(a) - order(b));
      if (byTier.get(tier).length > MAX_COMPACTS_PER_TIER) {
        byTier.set(tier, byTier.get(tier).slice(-MAX_COMPACTS_PER_TIER));
      }
    }

    const widest = Math.max(1, ...tiers.map((tier) => byTier.get(tier).length));
    const width = Math.max(280, PAD_X * 2 + widest * SLOT);
    const tierRows = tiers.length;
    const height = PAD_TOP + tierRows * ROW_H + EPISODE_ROW + EVENTS_ROW;

    const positions = new Map();
    const rowY = (tier) => {
      const indexFromTop = tiers.length - 1 - tiers.indexOf(tier);
      return PAD_TOP + indexFromTop * ROW_H + NODE_H / 2 + 12;
    };
    for (const tier of tiers) {
      const row = byTier.get(tier);
      const span = row.length * SLOT;
      const startX = (width - span) / 2 + SLOT / 2;
      row.forEach((compact, index) => {
        positions.set(compact.id, { x: startX + index * SLOT, y: rowY(tier), tier });
      });
    }

    const episodeY = PAD_TOP + tierRows * ROW_H + EPISODE_ROW / 2;
    const eventsY = PAD_TOP + tierRows * ROW_H + EPISODE_ROW + EVENTS_ROW / 2;

    const root = svg("svg", {
      class: "mem-dendro",
      viewBox: `0 0 ${width} ${height}`,
      width,
      height,
      role: "group",
      "aria-label": `Свёртки чата ${this.chatId}: ${compacts.length} компактов, ${episodes.length} эпизодов`,
    });

    const layerEdges = svg("g", { class: "mem-dendro__edges" });
    const layerNodes = svg("g", { class: "mem-dendro__nodes" });
    const layerLabels = svg("g", { class: "mem-dendro__labels" });

    // подписи страт слева
    for (const tier of tiers) {
      const y = rowY(tier);
      layerLabels.append(svg("line", { class: "mem-dendro__rule", x1: 6, y1: y, x2: width - 6, y2: y }));
      const label = svg("text", { class: "mem-dendro__stratum", x: 8, y: y - 18 });
      label.textContent = `tier ${tier} · ${byTier.get(tier).length}`;
      layerLabels.append(label);
    }

    // эпизоды висят под своим компактом. Колонки разнесены на EPISODE_COL, цель
    // тапа сжата до EPISODE_HIT_R — иначе соседние точки перехватывали палец.
    const episodeSlots = new Map();
    const episodeExtra = new Map();
    for (const episode of episodes) {
      const parent = positions.get(episode.compact_id);
      if (!parent) continue;
      const slot = episodeSlots.get(episode.compact_id) || 0;
      episodeSlots.set(episode.compact_id, slot + 1);
      if (slot >= EPISODES_PER_COMPACT) {
        episodeExtra.set(episode.compact_id, (episodeExtra.get(episode.compact_id) || 0) + 1);
        continue;
      }
      const x = parent.x + (slot % 3 - 1) * EPISODE_COL;
      const y = episodeY + Math.floor(slot / 3) * EPISODE_ROW_STEP;
      const mark = svg("g", {
        // memory_life пишет только "closed" / "continued" — «open» не существует
        class: `mem-episode${episode.status === "continued" ? " is-continued" : ""}`,
        tabindex: "0",
        role: "button",
        "data-episode-id": episode.id,
      });
      const markTitle = svg("title", {});
      markTitle.textContent = episode.title || episode.id;
      mark.append(markTitle);
      mark.append(svg("circle", { class: "mem-episode__hit", cx: x, cy: y, r: EPISODE_HIT_R }));
      mark.append(svg("circle", { class: "mem-episode__dot", cx: x, cy: y, r: 3.4 }));
      layerNodes.append(mark);
      layerEdges.append(svg("path", {
        class: "mem-dendro__edge mem-dendro__edge--episode",
        d: `M ${x} ${y - 3.4} C ${x} ${y - 16}, ${parent.x} ${parent.y + 24}, ${parent.x} ${parent.y + NODE_H / 2}`,
      }));
    }

    // хвост эпизодов сверх шести — одной отметкой «+N», без каши из точек
    for (const [compactId, extra] of episodeExtra) {
      const parent = positions.get(compactId);
      if (!parent) continue;
      const more = svg("text", {
        class: "mem-episode__more",
        x: parent.x,
        y: episodeY + 2 * EPISODE_ROW_STEP + 4,
      });
      more.textContent = `+${extra}`;
      const moreTitle = svg("title", {});
      moreTitle.textContent = `ещё ${extra} эпизодов у этого компакта`;
      more.append(moreTitle);
      layerLabels.append(more);
    }

    // рёбра compact_of: ребёнок (ниже) → родитель (выше)
    for (const edge of edges) {
      if (String(edge.kind) !== "compact_of") continue;
      const from = positions.get(String(edge.from));
      const to = positions.get(String(edge.to));
      if (!from || !to) continue;
      const y1 = from.y - NODE_H / 2;
      const y2 = to.y + NODE_H / 2;
      const mid = (y1 + y2) / 2;
      layerEdges.append(svg("path", {
        class: `mem-dendro__edge${frontier.has(String(edge.to)) ? " is-frontier" : ""}`,
        d: `M ${from.x} ${y1} C ${from.x} ${mid}, ${to.x} ${mid}, ${to.x} ${y2}`,
      }));
    }

    // компакты
    for (const tier of tiers) {
      for (const compact of byTier.get(tier)) {
        const point = positions.get(compact.id);
        if (!point) continue;
        const flags = [compact.continued ? "continued" : "", compact.legacy ? "legacy" : "", compact.degraded ? "degraded" : ""].filter(Boolean);
        const group = svg("g", {
          class: [
            "mem-compact",
            frontier.has(compact.id) ? "is-frontier" : "",
            compact.degraded ? "is-degraded" : "",
            compact.legacy ? "is-legacy" : "",
          ].filter(Boolean).join(" "),
          tabindex: "0",
          role: "button",
          "data-compact-id": compact.id,
          "aria-label": `Компакт tier ${compact.tier}, ${compact.event_count} событий${flags.length ? `, ${flags.join(", ")}` : ""}`,
        });
        const title = svg("title", {});
        title.textContent = compact.summary || compact.id;
        group.append(title);
        group.append(svg("rect", {
          class: "mem-compact__box",
          x: point.x - NODE_W / 2,
          y: point.y - NODE_H / 2,
          width: NODE_W,
          height: NODE_H,
          rx: 9,
        }));
        const caption = svg("text", { class: "mem-compact__count", x: point.x, y: point.y + 3.6 });
        caption.textContent = compact.event_count > 999 ? "999+" : String(compact.event_count);
        group.append(caption);
        if (compact.degraded) {
          group.append(svg("circle", { class: "mem-compact__flag", cx: point.x + NODE_W / 2 - 3, cy: point.y - NODE_H / 2 + 3, r: 3 }));
        }
        layerNodes.append(group);
      }
    }

    // Нижняя страта — сырые события. `totals.events` ГЛОБАЛЬНЫЙ (строки во всех
    // memory/life/events/*.jsonl), одинаковый в обзоре и в любом чате, поэтому
    // число живёт в строке-примечании над дендрограммой, а не подписью под
    // однo-чатовой полосой, где читалось бы как счёт этого чата.
    const eventCount = intOf(totals.events, intOf(this.eventTotal, 0));
    this.setNote(`${eventCount} событий во всех потоках — сырое дно`);
    const barWidth = Math.max(60, width - PAD_X * 2);
    layerNodes.append(svg("rect", {
      class: "mem-events__bar",
      x: (width - barWidth) / 2,
      y: eventsY - 7,
      width: barWidth,
      height: 14,
      rx: 7,
    }));
    const eventsLabel = svg("text", { class: "mem-events__label", x: width / 2, y: eventsY + 25 });
    eventsLabel.textContent = "сырые события — дно потока";
    layerLabels.append(eventsLabel);

    root.append(layerEdges, layerNodes, layerLabels);
    this.setBody(root);
    this.scroll.scrollLeft = Math.max(0, (width - this.scroll.clientWidth) / 2);
  }

  _onClick(event) {
    const compact = event.target.closest?.("[data-compact-id]");
    if (compact) {
      this.openCompact(compact.getAttribute("data-compact-id"));
      return;
    }
    const episode = event.target.closest?.("[data-episode-id]");
    if (episode) this.openEpisode(episode.getAttribute("data-episode-id"));
  }

  _onKey(event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target.closest?.("[data-compact-id], [data-episode-id]");
    if (!target) return;
    event.preventDefault();
    const compactId = target.getAttribute("data-compact-id");
    if (compactId) this.openCompact(compactId);
    else this.openEpisode(target.getAttribute("data-episode-id"));
  }

  openCompact(id) {
    const compact = this.compacts?.get(String(id));
    if (!compact || typeof this.sheet !== "function") return;
    const body = el("div", "mem-dossier");
    const facts = el("div", "mem-facts");
    facts.append(
      factCell("tier", String(compact.tier)),
      factCell("depth", String(compact.depth)),
      factCell("событий", String(compact.event_count)),
      factCell("из источников", String(compact.source_event_count || compact.source_compact_ids.length)),
      factCell("от", shortTime(compact.first_ts)),
      factCell("до", shortTime(compact.last_ts)),
    );
    body.append(facts);

    const flags = el("div", "mem-flags");
    if (this.frontier?.has(compact.id)) flags.append(el("span", "mem-flag mem-flag--frontier", "фронтир"));
    if (compact.continued) flags.append(el("span", "mem-flag", "continued"));
    if (compact.legacy) flags.append(el("span", "mem-flag", "legacy"));
    if (compact.degraded) flags.append(el("span", "mem-flag mem-flag--warn", "degraded"));
    if (flags.childElementCount) body.append(flags);

    body.append(el("h4", "mem-dossier__title", "Суть"));
    body.append(compact.summary
      ? el("p", "mem-summary", compact.summary)
      : stateBox("empty", "Сути нет", "В компакте не нашлось раздела «## Суть»."));

    const meta = el("div", "mem-meta");
    meta.append(el("small", "", `создан ${shortTime(compact.created_at)}`), el("small", "mem-mono", compact.id));
    if (compact.source_compact_ids.length) {
      meta.append(el("small", "", `свёрнут из ${compact.source_compact_ids.length} компактов`));
    }
    body.append(meta);

    this.sheet(`Компакт tier ${compact.tier}`, body);
  }

  openEpisode(id) {
    const episode = this.episodes?.get(String(id));
    if (!episode || typeof this.sheet !== "function") return;
    const body = el("div", "mem-dossier");
    const facts = el("div", "mem-facts");
    facts.append(
      factCell("статус", episode.status || "—"),
      factCell("от", shortTime(episode.first_ts)),
      factCell("до", shortTime(episode.last_ts)),
    );
    body.append(facts);
    body.append(el("h4", "mem-dossier__title", "Эпизод"));
    body.append(el("p", "mem-summary", episode.title || "Без заголовка"));
    const meta = el("div", "mem-meta");
    meta.append(el("small", "mem-mono", episode.id), el("small", "mem-mono", episode.compact_id));
    body.append(meta);
    this.sheet(episode.title || "Эпизод", body);
  }

  destroy() {
    this.destroyed = true;
    this.token += 1;
    this.scroll.removeEventListener("click", this._onClick);
    this.scroll.removeEventListener("keydown", this._onKey);
  }
}

/* ═══════════════════════ публичный вход ═══════════════════════════ */

function block(titleText, hintText) {
  const section = el("section", "mem-block");
  const head = el("header", "mem-block__head");
  const copy = el("div", "mem-block__copy");
  copy.append(el("h3", "", titleText), el("p", "", hintText));
  // ⚠ Шапка — flex со space-between: кнопка, положенная в неё напрямую, разведёт
  // заголовок и кнопки по краям. Кнопок стало две — им нужна общая обойма.
  const tools = el("div", "mem-block__tools");
  head.append(copy, tools);
  section.append(head);
  return { section, head, tools };
}

export function initMemoryViews({ mount, api, sheet, onError, fullscreenHost, onFullscreen } = {}) {
  destroyMemoryViews();
  if (!(mount instanceof Element) || typeof api !== "function") return null;

  const report = (error) => {
    try { onError?.(error); } catch (_) { /* хост не обязан переживать наши ошибки */ }
  };

  const root = el("div", "mem-views");

  /* ── полноэкранная сцена ────────────────────────────────────────────
     Сцена физически ПЕРЕЕЗЖАЕТ в хост вне `.view`: у секции есть transform и
     will-change, а трансформированный предок становится containing block даже для
     `position: fixed` — класс на .mem-stage дал бы «фуллскрин» размером с секцию.
     На её место встаёт распорка той же высоты, иначе лента схлопнется и после
     сворачивания владелец окажется не там, где был. Хоста может не быть (старая
     оболочка из кэша) — тогда кнопка прячется и тап ничего не делает: молчаливая
     деградация честнее мёртвой кнопки. */
  const host = fullscreenHost instanceof Element ? fullscreenHost : null;
  let opened = null;                  // { scene, slot } — развёрнута ровно одна сцена

  const exitButton = el("button", "mem-chip mem-chip--ghost stage-full__exit", "Свернуть");
  exitButton.type = "button";
  exitButton.addEventListener("click", () => exitFullscreen());

  function notifyFullscreen(activeNow) {
    try { onFullscreen?.(activeNow); } catch (_) { /* хост не обязан переживать наши ошибки */ }
  }

  function enterFullscreen(scene) {
    if (!host || opened || !scene) return;
    const stage = scene.stageEl();
    const home = stage.parentNode;
    if (!home) return;                // сцена не смонтирована — разворачивать нечего
    const slot = el("div", "mem-stage-slot");
    home.insertBefore(slot, stage);
    host.replaceChildren(stage, exitButton);
    host.hidden = false;
    opened = { scene, slot };
    scene.setImmersive(true);
    scene.resize();
    // Фокус обязан уехать в слой: иначе Tab остался бы в ленте ПОД сценой, а
    // клавиатурному владельцу выйти было бы нечем, кроме Escape наугад.
    exitButton.focus({ preventScroll: true });
    notifyFullscreen(true);
  }

  function exitFullscreen() {
    if (!opened) return;
    const { scene, slot } = opened;
    opened = null;
    const stage = scene.stageEl();
    if (slot.parentNode) slot.parentNode.replaceChild(stage, slot);
    else stage.remove();              // ленту снесли, пока сцена была в отъезде
    if (host) {
      host.replaceChildren();
      host.hidden = true;
    }
    scene.setImmersive(false);
    scene.resize();
    if (scene.expand?.isConnected) scene.expand.focus({ preventScroll: true });
    notifyFullscreen(false);
  }

  const constellationBlock = block(
    "Пространство памяти",
    "Люди и темы как капли пигмента в объёме: тап — досье, тап по пустому — на весь экран, один палец — поворот, два — приблизить.",
  );
  const codeBlock = block(
    "Пространство кода",
    "Её файлы как объём: навыки золотом, модули синим; ребро — импорт или вызов, толще — чаще.",
  );
  const dendroBlock = block("Как опыт сворачивается", "Снизу — сырые события, выше — компакты по тирам, эпизоды висят под своим компактом.");

  const constellation = new Constellation(constellationBlock.section, {
    sheet,
    onError: report,
    onEmptyTap: () => enterFullscreen(constellation),
    flavour: "memory",
    ariaLabel: "Пространство памяти: люди и темы",
    emptyTitle: "Пространство пусто",
    emptyHint: "Граф памяти ещё не собран — карты PEOPLE и TOPICS пока без узлов.",
    errorTitle: "Граф памяти не прочитался",
  });
  constellationBlock.tools.append(constellation.recenter, constellation.expand);
  constellation.expand.hidden = !host;
  constellation.expand.addEventListener("click", () => enterFullscreen(constellation));
  constellation.mountInto(constellationBlock.section);

  const codeSpace = new Constellation(codeBlock.section, {
    sheet,
    onError: report,
    onEmptyTap: () => enterFullscreen(codeSpace),
    flavour: "code",
    ariaLabel: "Пространство кода: модули и связи",
    emptyTitle: "Граф кода пуст",
    emptyHint: "Обход репозитория ещё не собирал модули — узлов нет.",
    errorTitle: "Граф кода не прочитался",
  });
  codeBlock.tools.append(codeSpace.recenter, codeSpace.expand);
  codeSpace.expand.hidden = !host;
  codeSpace.expand.addEventListener("click", () => enterFullscreen(codeSpace));
  codeSpace.mountInto(codeBlock.section);

  const dendro = new Dendrogram({ api, sheet, onError: report });
  dendroBlock.section.append(dendro.root);

  root.append(constellationBlock.section, codeBlock.section, dendroBlock.section);
  mount.append(root);

  const themeObserver = new MutationObserver(() => {
    constellation.refreshTheme();
    codeSpace.refreshTheme();
  });
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  const instance = {
    root,
    constellation,
    codeSpace,
    dendro,
    themeObserver,
    exitFullscreen,
    isFullscreen: () => Boolean(opened),
    // Порядок важен: сначала свернуть, потом наблюдатель, потом сцены, потом DOM.
    // Свернуть ПЕРВЫМ обязательно: root.remove() унесёт ленту, и сцена осталась бы
    // висеть в полноэкранном хосте поверх уже другого раздела. Дальше — как было:
    // иначе MutationObserver успеет дёрнуть уже снесённый GL-контекст.
    destroy() {
      exitFullscreen();
      themeObserver.disconnect();
      constellation.destroy();
      codeSpace.destroy();
      dendro.destroy();
      root.remove();
    },
  };
  active = instance;

  constellation.resize();
  constellation.setStatus(stateBox("loading", "Собираю созвездие", "Читаю граф памяти."));

  (async () => {
    try {
      const payload = await api(GRAPH_PATH);
      if (active !== instance) return;
      constellation.resize();
      constellation.load(payload);
    } catch (error) {
      if (active !== instance) return;
      report(error);
      constellation.setStatus(stateBox("error", "Граф не прочитался", error?.message || "Запрос не прошёл."));
    }
  })();

  codeSpace.resize();
  codeSpace.setStatus(stateBox("loading", "Собираю пространство кода", "Читаю граф модулей."));

  (async () => {
    try {
      const payload = await api(CODE_GRAPH_PATH);
      if (active !== instance) return;
      codeSpace.resize();
      codeSpace.load(payload);
    } catch (error) {
      if (active !== instance) return;
      report(error);
      // 404 — это «маршрута ещё нет», а не «код не прочитался»: не пугаем красным
      codeSpace.setStatus(error?.status === 404
        ? stateBox("empty", "Граф кода ещё не отдаётся", "Маршрут /code/graph не отвечает на этой сборке.")
        : stateBox("error", "Граф кода не прочитался", error?.message || "Запрос не прошёл."));
    }
  })();

  dendro.load().catch((error) => report(error));

  return instance;
}

export function destroyMemoryViews() {
  if (!active) return;
  const instance = active;
  active = null;
  try { instance.destroy(); } catch (_) { /* уже снят */ }
}

export default { initMemoryViews, destroyMemoryViews };
