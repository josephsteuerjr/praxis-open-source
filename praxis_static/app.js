import { AmbientField } from "/app/static/ambient.js";
import { initMemoryViews, destroyMemoryViews } from "/app/static/memory_views.js";

const API = "/api/praxis/v1";
const ACTIVE_STATUSES = new Set(["pending", "running", "blocked", "paused", "in_doubt"]);
// ⚠ 03.08.2026. ACTIVE_STATUSES означает «не терминальные», а вовсе не «движется»:
// заблокированный прогон СТОИТ и ждёт человека. Плитка на главной звала его «в движении»
// и подставляла его цель как текущую работу — то есть врала о занятости Праксис ровно там,
// где на неё смотрят первым делом. Движение — только эти два статуса.
const MOVING_STATUSES = new Set(["pending", "running"]);
const ATTENTION_STATUSES = new Set(["blocked", "paused", "in_doubt", "failed"]);
const TERMINAL_STATUSES = new Set(["done", "cancelled", "failed"]);
const IDEMPOTENT_SIDE_EFFECT_COMMANDS = new Set([
  "process.start", "process.cancel", "files.export", "files.import",
  "desktop.activate", "desktop.input", "desktop.capture", "desktop.clipboard.write",
  "telegram.join", "telegram.leave", "telegram.followup.cancel",
  "memory.rebuild", "inventory.refresh", "system.restart",
  "inbox.read", "inbox.acted", "device.revoke",
]);
// ⚠ 03.08.2026. Чистое чтение не должно двигать снимок. map.read не возвращает
// revision, а scheduleRefresh("") ранним выходом не срабатывает: проверка там
// `revision && revision === model.revision`, и пустая строка её просто пропускает.
// Значит каждое открытие карты через 120 мс заказывало полную пересборку снимка —
// листинг всех durable-манифестов прогонов (их 2120) и COUNT по recall.sqlite3.
// Прочитать страницу текста стоило столько же, сколько обновить весь экран. А
// тактильный отклик «medium» там же подписывал чтение как исполненное действие.
const READ_ONLY_COMMANDS = new Set([
  "memory.map.read", "memory.search",
]);
const DEVICE_SCOPE_CHOICES = [
  ["praxis.snapshot", "Состояние и inbox"],
  ["praxis.events", "Живые обновления"],
  ["praxis.work", "Новые runs"],
  ["praxis.runs.control", "Управление runs"],
  ["computer.read", "Статус компьютера"],
  ["computer.files", "Файлы"],
  ["computer.process", "Процессы"],
  ["computer.apps", "Desktop-приложения"],
  ["praxis.telegram", "Telegram"],
  ["praxis.system.read", "Статус системы"],
  ["praxis.system.control", "Рестарт Praxis"],
];
const ICONS = new Set([
  "praxis", "now", "runs", "computer", "memory", "telegram", "more", "trust",
  "system", "artifact", "process", "map", "followup", "arrow", "back", "refresh",
  "pause", "play", "stop", "search", "plus", "close", "download", "clock", "warning",
]);

const ui = {
  app: document.querySelector("#app"),
  main: document.querySelector("#main"),
  boot: document.querySelector("#bootState"),
  refresh: document.querySelector("#refreshButton"),
  back: document.querySelector("#backButton"),
  scrim: document.querySelector("#scrim"),
  sheet: document.querySelector("#sheet"),
  sheetClose: document.querySelector("#sheetClose"),
  sheetTitle: document.querySelector("#sheetTitle"),
  sheetEyebrow: document.querySelector("#sheetEyebrow"),
  sheetBody: document.querySelector("#sheetBody"),
  toast: document.querySelector("#toastRegion"),
  sync: document.querySelector("#syncState"),
  brandState: document.querySelector("#brandState"),
  brandPulse: document.querySelector("#brandPulse"),
  stale: document.querySelector("#staleNotice"),
  staleTime: document.querySelector("#staleTime"),
  install: document.querySelector("#installButton"),
  enroll: document.querySelector("#enrollButton"),
  newDevice: document.querySelector("#newDeviceButton"),
  deviceSummary: document.querySelector("#deviceSummary"),
  deviceList: document.querySelector("#deviceList"),
};

const model = {
  snapshot: null,
  revision: "",
  currentView: "now",
  history: [],
  runFilter: "all",
  stream: null,
  streamTimer: 0,
  streamBackoff: 1000,
  pollingTimer: 0,
  refreshing: null,
  snapshotStale: false,
  verifiedAt: 0,
  cachedSnapshotLoaded: false,
  deviceAuth: null,
  authLocked: false,
  enrollmentToken: "",
  installPrompt: null,
  sheetOpen: false,
  sheetReturnFocus: null,
  // Развёрнутая на весь экран сцена памяти — отдельное состояние, а не вид и не
  // шторка: «назад» обязан свернуть её раньше, чем уводить владельца из раздела.
  memoryFullscreen: false,
};

// Созвездие и дендрограмма монтируются один раз за сессию: renderMemory зовётся
// на каждый snapshot (в т.ч. на каждый SSE-тик), пересборка графа там недопустима.
let memoryViewsMounted = false;
let memoryViews = null;         // тот же экземпляр: нужен, чтобы свернуть фуллскрин

const ambient = new AmbientField(document.querySelector("#ambient"));

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "" && text !== null && text !== undefined) node.textContent = String(text);
  return node;
}

function svgIcon(name, className = "icon") {
  const safe = ICONS.has(name) ? name : "praxis";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${safe}`);
  svg.append(use);
  return svg;
}

function replace(node, ...children) {
  node.replaceChildren(...children.filter(Boolean));
  return node;
}

function asList(value) {
  if (Array.isArray(value)) return value;
  if (value && Array.isArray(value.items)) return value.items;
  if (value && Array.isArray(value.rows)) return value.rows;
  return [];
}

function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function first(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function count(value) {
  if (Array.isArray(value)) return value.length;
  if (value && Array.isArray(value.items)) return value.items.length;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function boundedText(value, max = 500) {
  const text = String(value ?? "").replace(/\0/g, "").trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function dateValue(value) {
  const date = new Date(value || 0);
  return Number.isFinite(date.getTime()) ? date : null;
}

function relativeTime(value) {
  const date = dateValue(value);
  if (!date) return "—";
  const delta = Math.round((Date.now() - date.getTime()) / 1000);
  if (delta < -5) return date.toLocaleString("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  if (delta < 10) return "сейчас";
  if (delta < 60) return `${delta}с`;
  if (delta < 3600) return `${Math.floor(delta / 60)}м`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}ч`;
  if (delta < 604800) return `${Math.floor(delta / 86400)}д`;
  return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "short" });
}

function exactTime(value) {
  const date = dateValue(value);
  return date ? date.toLocaleString("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
}

function bytes(value) {
  let size = Number(value);
  if (!Number.isFinite(size) || size < 0) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 10 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
}

function statusLabel(value) {
  return ({
    pending: "ожидает", running: "в работе", paused: "на паузе", blocked: "заблокирован",
    in_doubt: "нужна сверка", done: "готов", failed: "ошибка", cancelled: "отменён",
    accepted: "принято", retry: "повтор", online: "на связи", offline: "не в сети",
    healthy: "здоров", degraded: "ослаблен", answered: "ответил", notified: "сообщено",
    applied: "применено", prepared: "подготовлено", intent: "намерение", active: "активно", revoked: "отозвано",
    // ⚠ 03.08.2026. snapshot.now.state приходит одним из четырёх слов: active | ready |
    // online | offline (praxis_app.snapshot, обе ветки). Слова ready в таблице не было —
    // и её покой печатался бы на экране латиницей. Пилюли и подписи ниже читают now.state
    // напрямую, поэтому таблица обязана покрывать весь словарь сервера.
    ready: "наготове",
  })[String(value || "").toLowerCase()] || boundedText(value || "неизвестно", 38);
}

function statusTone(value) {
  const status = String(value || "").toLowerCase();
  if (["done", "accepted", "online", "healthy", "applied", "notified", "active", "ready"].includes(status)) return "green";
  if (["running", "pending", "prepared", "intent"].includes(status)) return "violet";
  if (["blocked", "paused", "retry", "answered", "degraded"].includes(status)) return "gold";
  if (["failed", "cancelled", "in_doubt", "offline", "revoked"].includes(status)) return "red";
  return "cyan";
}

function statusPill(value) {
  const pill = element("span", "status-pill", statusLabel(value));
  pill.dataset.tone = statusTone(value);
  return pill;
}

function emptyState(title, detail = "", iconName = "praxis") {
  const box = element("div", "empty-state");
  box.append(svgIcon(iconName), element("strong", "", title));
  if (detail) box.append(element("small", "", detail));
  return box;
}

function errorState(title, detail = "") {
  const box = element("div", "error-state");
  box.append(svgIcon("warning"), element("strong", "", title));
  if (detail) box.append(element("small", "", detail));
  return box;
}

function loadingState(label = "Загружаю") {
  const box = element("div", "loading-state");
  const line = element("span", "skeleton", `${label}................`);
  box.append(line);
  return box;
}

function setText(selector, value, fallback = "—") {
  const node = document.querySelector(selector);
  if (node) node.textContent = String(value === undefined || value === null || value === "" ? fallback : value);
}

function showBadge(selector, value) {
  const node = document.querySelector(selector);
  if (!node) return;
  const numeric = Number(value) || 0;
  node.hidden = numeric <= 0;
  if (node.tagName !== "I") node.textContent = numeric > 99 ? "99+" : String(numeric);
  node.setAttribute("aria-label", numeric > 0 ? `${numeric} требуют внимания` : "");
}

function toast(message, kind = "info", timeout = 2800) {
  const node = element("div", `toast${kind === "error" ? " is-error" : ""}`, boundedText(message, 520));
  ui.toast.append(node);
  window.setTimeout(() => {
    node.classList.add("is-leaving");
    window.setTimeout(() => node.remove(), 230);
  }, timeout);
}

const DB_NAME = "praxis-app";
const DB_STORE = "kv";
let databasePromise = null;

function database() {
  if (!databasePromise) {
    databasePromise = new Promise((resolve, reject) => {
      if (!("indexedDB" in window)) {
        reject(new Error("IndexedDB недоступна"));
        return;
      }
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(DB_STORE)) request.result.createObjectStore(DB_STORE);
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("IndexedDB не открылась"));
      request.onblocked = () => reject(new Error("IndexedDB заблокирована другой вкладкой"));
    });
  }
  return databasePromise;
}

async function dbGet(key) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const request = db.transaction(DB_STORE, "readonly").objectStore(DB_STORE).get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("IndexedDB read failed"));
  });
}

async function dbPut(key, value) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(DB_STORE, "readwrite");
    transaction.objectStore(DB_STORE).put(value, key);
    transaction.oncomplete = () => resolve(value);
    transaction.onerror = () => reject(transaction.error || new Error("IndexedDB write failed"));
    transaction.onabort = () => reject(transaction.error || new Error("IndexedDB write aborted"));
  });
}

async function dbDelete(key) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(DB_STORE, "readwrite");
    transaction.objectStore(DB_STORE).delete(key);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("IndexedDB delete failed"));
    transaction.onabort = () => reject(transaction.error || new Error("IndexedDB delete aborted"));
  });
}

async function dbDeletePrefix(prefix) {
  if (!prefix) return;
  const db = await database();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(DB_STORE, "readwrite");
    const store = transaction.objectStore(DB_STORE);
    const request = store.openCursor(IDBKeyRange.bound(prefix, `${prefix}\uffff`));
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) return;
      cursor.delete();
      cursor.continue();
    };
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("IndexedDB prefix delete failed"));
    transaction.onabort = () => reject(transaction.error || new Error("IndexedDB prefix delete aborted"));
  });
}

function normalizeDeviceAuth(value) {
  const auth = object(value);
  // first(...) yields undefined when no candidate is present; String(undefined)
  // would become the truthy literal "undefined" and forge an authenticated
  // session on a fresh install, so coerce the miss to an empty token.
  const token = String(first(auth.token, auth.device_token) || "");
  if (!token || token.length > 8192) return null;
  const rawTime = first(auth.enrolledAt, auth.enrolled_at, auth.issued_at, Date.now());
  const numericTime = Number(rawTime);
  const enrolledAt = Number.isFinite(numericTime) ? numericTime : Date.parse(String(rawTime));
  return {
    token,
    deviceId: boundedText(first(auth.deviceId, auth.device_id, "local"), 180),
    label: boundedText(first(auth.label, "Praxis desktop"), 180),
    enrolledAt: Number.isFinite(enrolledAt) ? enrolledAt : Date.now(),
  };
}

async function loadDeviceAuth() {
  try {
    model.deviceAuth = normalizeDeviceAuth(await dbGet("device-auth"));
  } catch (_) {
    model.deviceAuth = null;
  }
  return model.deviceAuth;
}

async function storeDeviceAuth(payload) {
  if (!window.isSecureContext) throw new Error("Device token можно сохранить только в защищённом HTTPS-контексте");
  const auth = normalizeDeviceAuth(payload);
  if (!auth) throw new Error("Сервер не вернул device token");
  await dbPut("device-auth", auth);
  model.deviceAuth = auth;
  model.authLocked = false;
  const persistence = navigator.storage?.persist?.();
  persistence?.catch(() => {});
  return auth;
}

function takeEnrollmentToken() {
  const url = new URL(location.href);
  const hash = new URLSearchParams(url.hash.replace(/^#/, ""));
  const names = ["enroll", "enrollment", "device_enroll"];
  let token = "";
  for (const name of names) {
    // Enrollment credentials are fragment-only so they never reach proxy or
    // HTTP access logs.  A query-string secret is intentionally not accepted.
    token = token || hash.get(name) || "";
    hash.delete(name);
  }
  url.hash = hash.toString() ? `#${hash}` : "";
  if (token) history.replaceState(history.state, "", `${url.pathname}${url.search}${url.hash}`);
  return String(token).slice(0, 8192);
}

// Recover an enrollment secret from text the owner pasted inside the installed
// app: either the full one-time link (secret lives in its #fragment) or the raw
// token. Adding Praxis to the Home Screen drops the fragment, so the standalone
// PWA never inherits the token from its own URL — the owner re-supplies it here.
function extractEnrollmentToken(raw) {
  const text = String(raw || "").trim();
  if (!text) return "";
  if (/^praxis_enroll_/.test(text)) return text.slice(0, 8192);
  try {
    const hash = new URLSearchParams(new URL(text).hash.replace(/^#/, ""));
    for (const name of ["enroll", "enrollment", "device_enroll"]) {
      const token = hash.get(name);
      if (token) return String(token).slice(0, 8192);
    }
  } catch (_) {
    // Not a URL; fall through — only a recognised token shape is accepted.
  }
  return "";
}

function initDataFromLocation() {
  // НЕ срезаем tgWebAppData: это HMAC-подписанные launch-данные Telegram, они НЕ уходят на
  // сервер (фрагмент не передаётся в HTTP) и служат единственным источником восстановления
  // initData при перезагрузке вебвью, если SDK почему-то не отдал WebApp.initData.
  const hash = new URLSearchParams(new URL(location.href).hash.replace(/^#/, ""));
  return String(hash.get("tgWebAppData") || "");
}

const telegram = (() => {
  // Живой доступ к WebApp (не захват в null на eval, если SDK ещё догружается) и живой
  // getter initData с last-known-good: reload/возврат из фона не замораживает пустую сессию.
  const wa = () => window.Telegram?.WebApp || null;
  let lastInitData = "";
  function currentInitData() {
    const live = String(wa()?.initData || "");
    if (live) { lastInitData = live; return live; }
    const frag = initDataFromLocation();
    if (frag) { lastInitData = frag; return frag; }
    return lastInitData;
  }
  currentInitData();  // засеять last-known-good на старте
  const callbacks = new Set();

  function nativePost(eventType, eventData = {}) {
    if (window.TelegramWebviewProxy?.postEvent) {
      window.TelegramWebviewProxy.postEvent(eventType, JSON.stringify(eventData));
      return;
    }
    const payload = JSON.stringify({ eventType, eventData });
    if (window.external?.notify) {
      window.external.notify(payload);
    } else if (window.parent && window.parent !== window) {
      window.parent.postMessage(payload, "*");
    }
  }

  function ready() {
    const webApp = wa();
    if (webApp) {
      webApp.ready();
      webApp.expand();
      if (typeof webApp.disableVerticalSwipes === "function") webApp.disableVerticalSwipes();
      else if ("isVerticalSwipesEnabled" in webApp) webApp.isVerticalSwipesEnabled = false;
    } else {
      nativePost("web_app_ready");
      nativePost("web_app_expand");
      nativePost("web_app_setup_swipe_behavior", { allow_vertical_swipe: false });
    }
  }

  function haptic(style = "light") {
    try {
      const webApp = wa();
      if (webApp?.HapticFeedback) webApp.HapticFeedback.impactOccurred(style);
      else nativePost("web_app_trigger_haptic_feedback", { type: "impact", impact_style: style });
    } catch (_) {
      // Haptics are a flourish, never a control dependency.
    }
  }

  function setBack(visible) {
    const webApp = wa();
    if (webApp?.BackButton) {
      if (visible) webApp.BackButton.show(); else webApp.BackButton.hide();
    } else {
      nativePost("web_app_setup_back_button", { is_visible: Boolean(visible) });
    }
  }

  function onBack(callback) {
    callbacks.add(callback);
    const webApp = wa();
    if (webApp?.BackButton) webApp.BackButton.onClick(callback);
  }

  function dispatchBack() {
    callbacks.forEach((callback) => callback());
  }

  if (!wa()) {
    const receive = (eventType) => {
      if (eventType === "back_button_pressed") dispatchBack();
    };
    window.TelegramGameProxy = window.TelegramGameProxy || {};
    window.TelegramGameProxy.receiveEvent = receive;
    window.TelegramGameProxy_receiveEvent = receive;
    window.addEventListener("message", (event) => {
      try {
        const data = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
        receive(data?.eventType || data?.event_type);
      } catch (_) {
        // Ignore unrelated WebView messages.
      }
    });
  }

  return {
    get webApp() { return wa(); },
    get initData() { return currentInitData(); },
    ready, haptic, setBack, onBack,
  };
})();

function telegramActorId() {
  if (!telegram.initData) return "";
  try {
    const user = JSON.parse(new URLSearchParams(telegram.initData).get("user") || "{}");
    return /^\d+$/.test(String(user.id || "")) ? String(user.id) : "";
  } catch (_) {
    return "";
  }
}

function snapshotPartition() {
  if (telegram.initData) {
    const actor = telegramActorId();
    return actor ? `telegram:${actor}` : "";
  }
  if (model.deviceAuth?.token) return `device:${model.deviceAuth.deviceId || "local"}`;
  return "";
}

function hasAuthenticatedSession() {
  return !model.authLocked && !model.enrollmentToken
    && Boolean(telegram.initData || model.deviceAuth?.token);
}

async function lockRejectedSession(error) {
  const partition = snapshotPartition();
  closeEvents();
  if (partition) {
    await Promise.allSettled([
      dbDelete(`verified-snapshot:${partition}`),
      dbDeletePrefix(`command-draft:${partition}:`),
    ]);
  }
  if (!telegram.initData && model.deviceAuth) {
    await dbDelete("device-auth").catch(() => {});
    model.deviceAuth = null;
  }
  model.authLocked = true;
  model.snapshot = null;
  model.revision = "";
  model.cachedSnapshotLoaded = true;
  setSync("error", "доступ отозван");
  scrubProtectedSurface(error?.status === 403 ? "Устройство больше не авторизовано" : "Сессия недействительна");
  if (installedStandalone()) renderEnrollmentReopen();  // F6: дать переоткрыть привязку после отзыва
  toast("Доступ не подтверждён. Локальный snapshot и черновики этой сессии удалены.", "error", 5600);
}

async function saveVerifiedSnapshot(snapshot) {
  const partition = snapshotPartition();
  if (!partition) return;
  await dbPut(`verified-snapshot:${partition}`, {
    partition,
    verifiedAt: Date.now(),
    snapshot,
  });
}

async function loadVerifiedSnapshot() {
  const partition = snapshotPartition();
  if (!partition) return null;
  try {
    const record = object(await dbGet(`verified-snapshot:${partition}`));
    if (record.partition !== partition || !object(record.snapshot).schema) return null;
    const verifiedAt = Number(record.verifiedAt);
    if (!Number.isFinite(verifiedAt) || verifiedAt <= 0) return null;
    return { snapshot: record.snapshot, verifiedAt };
  } catch (_) {
    return null;
  }
}

function setSnapshotFreshness(stale, verifiedAt = Date.now()) {
  model.snapshotStale = Boolean(stale);
  model.verifiedAt = Number(verifiedAt) || 0;
  ui.stale.hidden = !model.snapshotStale;
  ui.app.classList.toggle("is-stale", model.snapshotStale);
  if (model.snapshotStale) {
    const suffix = navigator.onLine ? "связь с сервером потеряна" : "команды отключены до возвращения связи";
    ui.staleTime.textContent = `${exactTime(model.verifiedAt)} · ${suffix}`;
    ui.brandState.textContent = "offline snapshot";
    ui.brandPulse.classList.remove("is-live");
  }
}

class OfflineMutationError extends Error {
  constructor() {
    super("Офлайн: действие не отправлено и не будет повторено автоматически");
    this.name = "OfflineMutationError";
    this.offline = true;
  }
}

function freshIdempotencyKey() {
  if (typeof crypto.randomUUID === "function") return `pwa-${crypto.randomUUID()}`;
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return `pwa-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function commandNeedsIdempotency(domain, action) {
  return IDEMPOTENT_SIDE_EFFECT_COMMANDS.has(`${String(domain)}.${String(action)}`);
}

function applyTheme() {
  document.documentElement.dataset.theme = "dark";
  ambient.setState({ theme: "dark" });
}

function attachSessionAuth(headers) {
  if (telegram.initData) headers.set("X-Telegram-Init-Data", telegram.initData);
  else if (model.deviceAuth?.token) headers.set("Authorization", `Bearer ${model.deviceAuth.token}`);
}

async function api(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  if (!navigator.onLine && !["GET", "HEAD"].includes(method)) throw new OfflineMutationError();
  const headers = new Headers(options.headers || {});
  if (options.auth !== false) attachSessionAuth(headers);
  headers.set("Accept", "application/json");
  if (options.body !== undefined && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  let response;
  try {
    response = await fetch(`${API}${path}`, {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      cache: "no-store",
      credentials: "same-origin",
      signal: options.signal,
    });
  } catch (cause) {
    const error = new Error("Сеть не подтвердила доставку запроса");
    error.network = true;
    error.offline = !navigator.onLine;
    error.cause = cause;
    throw error;
  }
  let payload = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    payload = await response.json().catch(() => null);
  } else {
    payload = { text: await response.text().catch(() => "") };
  }
  if (!response.ok) {
    const error = new Error(boundedText(payload?.error || payload?.message || `HTTP ${response.status}`, 600));
    error.status = response.status;
    error.payload = payload;
    if (options.auth !== false && response.status === 401) {
      await lockRejectedSession(error);
      error.sessionLocked = true;
    }
    throw error;
  }
  return payload || {};
}

async function apiMultipart(path, body) {
  if (!navigator.onLine) throw new OfflineMutationError();
  const headers = new Headers({ Accept: "application/json" });
  attachSessionAuth(headers);
  let response;
  try {
    response = await fetch(`${API}${path}`, {
      method: "POST",
      headers,
      body,
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch (cause) {
    const error = new Error("Сеть не подтвердила доставку файла");
    error.network = true;
    error.offline = !navigator.onLine;
    error.cause = cause;
    throw error;
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(boundedText(payload?.error || payload?.message || `HTTP ${response.status}`, 600));
    error.status = response.status;
    error.payload = payload;
    if (response.status === 401) {
      // Telegram-сессия регенерируема: разовый 401 (протухла initData после долгого простоя)
      // НЕ запираем в «нет доступа» — подтолкнём SDK, поллер повторит со свежей подписью.
      // Запираем (и чистим креды) только device-auth (PWA), где токен реально отозван.
      if (telegram.initData) {
        telegram.ready();
      } else {
        await lockRejectedSession(error);
        error.sessionLocked = true;
      }
    }
    throw error;
  }
  return payload || {};
}

function setSync(mode, label) {
  ui.sync.className = `sync-state${mode ? ` is-${mode}` : ""}`;
  const text = ui.sync.querySelector("span");
  if (text) text.textContent = label;
}

function snapshotRevision(snapshot) {
  return String(first(snapshot.revision, snapshot.rev, snapshot.generated_revision) || "");
}

async function refreshSnapshot({ quiet = false } = {}) {
  if (!hasAuthenticatedSession()) throw new Error("Нет подтверждённой Praxis-сессии");
  if (model.refreshing) return model.refreshing;
  if (!quiet) setSync("", "обновляю");
  model.refreshing = (async () => {
    try {
      const snapshot = await api("/snapshot");
      const verifiedAt = Date.now();
      model.authLocked = false;
      document.querySelector("#reopenEnrollButton")?.remove();  // F6: авторизовались — кнопка не нужна
      applySnapshot(snapshot, { stale: false, verifiedAt });
      saveVerifiedSnapshot(snapshot).catch(() => {});
      setSync("live", "live");
      model.streamBackoff = 1000;
      return snapshot;
    } catch (error) {
      if (Number(error.status) === 401) {
        if (!error.sessionLocked) await lockRejectedSession(error);
        throw error;
      }
      if (model.snapshot) {
        setSnapshotFreshness(true, model.verifiedAt || Date.now());
        setSync("stale", navigator.onLine ? "связь потеряна" : "офлайн");
        if (!quiet) toast("Текущее подтверждённое состояние помечено устаревшим. Действие не поставлено в очередь.", "info", 4800);
        return model.snapshot;
      }
      const cached = model.cachedSnapshotLoaded ? null : await loadVerifiedSnapshot();
      model.cachedSnapshotLoaded = true;
      if (cached) {
        applySnapshot(cached.snapshot, { stale: true, verifiedAt: cached.verifiedAt });
        setSync("stale", "offline snapshot");
        if (!quiet) toast("Показано последнее подтверждённое состояние. Команды не поставлены в очередь.", "info", 4800);
        return cached.snapshot;
      }
      setSync("error", navigator.onLine ? "нет связи" : "офлайн");
      ui.brandState.textContent = "snapshot недоступен";
      ui.brandPulse.classList.remove("is-live");
      if (!quiet || !model.snapshot) toast(`Не удалось обновить: ${error.message}`, "error", 4200);
      if (!model.snapshot) renderOffline(error);
      throw error;
    } finally {
      model.refreshing = null;
      ui.boot.classList.add("is-done");
    }
  })();
  return model.refreshing;
}

function scheduleRefresh(revision = "") {
  if (!hasAuthenticatedSession()) return;
  if (revision && revision === model.revision) return;
  clearTimeout(scheduleRefresh.timer);
  scheduleRefresh.timer = window.setTimeout(() => refreshSnapshot({ quiet: true }).catch(() => {}), 120);
}

async function connectEvents() {
  closeEvents();
  if (document.hidden || !navigator.onLine || !hasAuthenticatedSession()
      || !hasScope("praxis.events")) return;
  try {
    const ticketPayload = await api("/events-ticket");
    const ticket = String(ticketPayload.ticket || "");
    if (!ticket) throw new Error("events ticket отсутствует");
    const source = new EventSource(`${API}/events?ticket=${encodeURIComponent(ticket)}`);
    model.stream = source;
    source.onopen = () => {
      model.streamBackoff = 1000;
      setSync("live", "live");
    };
    const consume = (event) => {
      if (!event.data) return;
      try {
        const data = JSON.parse(event.data);
        if (data.snapshot && typeof data.snapshot === "object") {
          applySnapshot(data.snapshot);
          saveVerifiedSnapshot(data.snapshot).catch(() => {});
        } else {
          scheduleRefresh(String(data.revision || data.rev || event.lastEventId || ""));
        }
      } catch (_) {
        scheduleRefresh(event.lastEventId || "");
      }
    };
    source.onmessage = consume;
    source.addEventListener("revision", consume);
    source.addEventListener("snapshot", consume);
    source.onerror = () => {
      closeEvents(false);
      if (!hasAuthenticatedSession()) return;
      setSync("", "переподключаю");
      const delay = model.streamBackoff;
      model.streamBackoff = Math.min(30000, Math.round(delay * 1.8));
      model.streamTimer = window.setTimeout(() => connectEvents().catch(() => {}), delay);
    };
  } catch (error) {
    if (!hasAuthenticatedSession()) return;
    setSync("", "polling");
    if (Number(error?.status) === 403) {
      refreshSnapshot({ quiet: true }).catch(() => {});
      return;
    }
    const delay = model.streamBackoff;
    model.streamBackoff = Math.min(30000, Math.round(delay * 1.8));
    model.streamTimer = window.setTimeout(() => connectEvents().catch(() => {}), delay);
  }
}

function closeEvents(clearTimer = true) {
  if (model.stream) {
    model.stream.close();
    model.stream = null;
  }
  if (clearTimer) {
    clearTimeout(model.streamTimer);
    model.streamTimer = 0;
  }
}

function session(snapshot = model.snapshot || {}) {
  return object(first(snapshot.viewer, snapshot.session, snapshot.me, snapshot.authority, {}));
}

function isOwner(snapshot = model.snapshot || {}) {
  const value = session(snapshot);
  return value.role === "owner" || value.owner === true;
}

function hasScope(scope, snapshot = model.snapshot || {}) {
  if (isOwner(snapshot)) return true;
  return asList(session(snapshot).scopes).includes(scope);
}

function allowedSections(snapshot = model.snapshot || {}) {
  if (isOwner(snapshot)) return new Set(["now", "runs", "computer", "memory", "telegram", "more"]);
  const declared = asList(session(snapshot).sections).map(String);
  const mapped = declared.map((name) => name === "trust" || name === "system" ? "more" : name === "inbox" ? "now" : name);
  return new Set(mapped.length ? ["now", ...mapped] : ["now", "computer"]);
}

function applySectionAccess(snapshot) {
  const allowed = allowedSections(snapshot);
  document.querySelectorAll("[data-nav], [data-open]").forEach((control) => {
    const target = control.dataset.nav || control.dataset.open;
    control.hidden = !allowed.has(target);
  });
  if (!allowed.has(model.currentView)) {
    document.querySelectorAll("[data-view]").forEach((view) => view.classList.remove("view--active", "view--leaving"));
    document.querySelector("[data-view='now']")?.classList.add("view--active");
    model.currentView = "now";
    model.history = [];
    updateBack();
  }
}

function runsOf(snapshot = model.snapshot || {}) {
  return asList(snapshot.runs);
}

// ⚠ 03.08.2026. runsOf — это ВЫБОРКА (snapshot.runs.items), а не история. Рядом в том
// же снимке лежат counts по манифестам и total; апп их не читал ни разу за 134 КБ кода,
// поэтому 1990 успешных прогонов не существовали для экрана, и раздел выглядел аварийным.
// Числа берём отсюда, карточки — из среза, и нигде не путаем одно с другим.
function runsMeta(snapshot = model.snapshot || {}) {
  return object(snapshot.runs);
}

function runsCounts(snapshot = model.snapshot || {}) {
  return object(runsMeta(snapshot).counts);
}

// null, а не 0: «сервер не прислал числа» и «прогонов нет» — разные утверждения,
// и второе нельзя печатать вместо первого.
function runsCountOf(snapshot, statuses) {
  const counts = runsCounts(snapshot);
  let sum = 0;
  let known = false;
  statuses.forEach((status) => {
    const value = Number(counts[String(status).toLowerCase()]);
    if (Number.isFinite(value) && value >= 0) {
      sum += value;
      known = true;
    }
  });
  return known ? sum : null;
}

function runsTotal(snapshot = model.snapshot || {}) {
  const declared = Number(runsMeta(snapshot).total);
  if (Number.isFinite(declared) && declared >= 0) return declared;
  const summed = runsCountOf(snapshot, Object.keys(runsCounts(snapshot)));
  if (summed !== null) return summed;
  return runsOf(snapshot).length;
}

// Статусы, которых в срезе нет НИ ОДНОЙ карточкой, хотя в истории они есть.
// Это и есть цена выборки, названная вслух.
function runsMissingStatuses(snapshot = model.snapshot || {}) {
  const present = new Set(runsOf(snapshot).map((run) => String(run.status || "").toLowerCase()));
  return Object.entries(runsCounts(snapshot))
    .map(([status, value]) => [String(status).toLowerCase(), Number(value)])
    .filter(([status, value]) => Number.isFinite(value) && value > 0 && !present.has(status))
    // Порядок — по величине. Object.entries отдаёт порядок, в котором ключи положил сервер,
    // то есть для читателя случайный: пропавшие 1990 успехов могли оказаться после одного
    // пропавшего blocked. Первым должно стоять то, чего не хватает больше всего.
    .sort((a, b) => b[1] - a[1]);
}

function computerOf(snapshot = model.snapshot || {}) {
  return object(first(snapshot.computer, snapshot.windows, snapshot.body, {}));
}

function telegramOf(snapshot = model.snapshot || {}) {
  return object(snapshot.telegram);
}

function memoryOf(snapshot = model.snapshot || {}) {
  return object(snapshot.memory);
}

function systemOf(snapshot = model.snapshot || {}) {
  return object(snapshot.system);
}

// ⚠ 03.08.2026. snapshot.now сервер собирает в КАЖДОМ снимке (praxis_app.snapshot):
// state, active_runs, body_online, pending_followups, inbox_unread. Во всём app.js слова
// snapshot.now не встречалось ни разу — шапка и герой вместо него спрашивали
// snapshot.praxis, ключ, которого не существует ни в одной версии схемы. Промах падал в
// литерал "online", то есть подпись под её именем говорила «на связи» безусловно: и когда
// она работает, и когда снимок вообще без состояния. Утверждение без источника хуже
// прочерка — прочерк виден, а зелёная надпись закрывает вопрос.
function nowOf(snapshot = model.snapshot || {}) {
  return object(snapshot.now);
}

function nowState(snapshot = model.snapshot || {}) {
  return String(nowOf(snapshot).state || "");
}

function durationText(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total}с`;
  if (total < 3600) return `${Math.floor(total / 60)}м`;
  if (total < 86400) return `${Math.floor(total / 3600)}ч ${Math.floor((total % 3600) / 60)}м`;
  return `${Math.floor(total / 86400)}д ${Math.floor((total % 86400) / 3600)}ч`;
}

function gigabytes(megabytes) {
  const value = Number(megabytes);
  return Number.isFinite(value) && value >= 0 ? Math.round(value / 102.4) / 10 : null;
}

// ⚠ 03.08.2026. panel.server_state() отдаёт loadavg, cpus, mem{total_mb, available_mb},
// disk{total_gb, free_gb}, uptime_sec, head и возраст каждого лога — и ни одно из этих
// чисел в аппе не показывалось. Вместо них плитка «Контур» и раздел «Сервисы» просили
// system.services: ключа с таким именем server_state() не возвращает никогда. Пустой
// список давал unhealthy = 0, а ноль печатался словами «Контур устойчив» и «0 сервисов».
// Зелёный вывод, сделанный ровно из нуля данных. Здесь числа читаются как есть, а
// отсутствие фактов называется отсутствием фактов.
function systemNumbers(system = {}) {
  const mem = object(system.mem);
  const disk = object(system.disk);
  const logs = object(system.logs);
  const load = String(system.loadavg || "").trim();
  const diskFree = Number(disk.free_gb);
  const diskTotal = Number(disk.total_gb);
  return {
    load: load ? load.split(" ")[0] : "",
    loadFull: load,
    cpus: Number(system.cpus) || 0,
    memFreeGb: gigabytes(mem.available_mb),
    memTotalGb: gigabytes(mem.total_mb),
    diskFreeGb: disk.free_gb !== undefined && Number.isFinite(diskFree) ? diskFree : null,
    diskTotalGb: disk.total_gb !== undefined && Number.isFinite(diskTotal) ? diskTotal : null,
    uptimeSec: Number(system.uptime_sec) || 0,
    logs: Object.entries(logs).filter(([, row]) => row && typeof row === "object"),
    error: system.error ? String(system.error) : "",
  };
}

function systemKnown(sys) {
  return Boolean(sys.loadFull) || sys.memTotalGb !== null || sys.diskTotalGb !== null || sys.uptimeSec > 0;
}

function inboxOf(snapshot = model.snapshot || {}) {
  return object(snapshot.inbox);
}

function inboxItems(snapshot = model.snapshot || {}) {
  return asList(inboxOf(snapshot).items);
}

function inboxUnread(snapshot = model.snapshot || {}) {
  const declared = Number(inboxOf(snapshot).unread);
  if (Number.isFinite(declared) && declared >= 0) return declared;
  return inboxItems(snapshot).filter((item) => ["queued", "delivered"].includes(String(item.status || "queued"))).length;
}

function applySnapshot(snapshot, { stale = false, verifiedAt = Date.now() } = {}) {
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) throw new Error("snapshot должен быть объектом");
  model.snapshot = snapshot;
  model.revision = snapshotRevision(snapshot);
  model.snapshotStale = Boolean(stale);
  model.verifiedAt = Number(verifiedAt) || 0;
  ui.refresh.hidden = false;
  applySectionAccess(snapshot);
  renderHeader(snapshot);
  renderNow(snapshot);
  renderRuns(snapshot);
  renderComputer(snapshot);
  renderMemory(snapshot);
  renderTelegram(snapshot);
  renderMore(snapshot);
  setSnapshotFreshness(stale, verifiedAt);
  ui.boot.classList.add("is-done");
}

function renderHeader(snapshot) {
  // ⚠ 03.08.2026. Было: first(praxis.status, praxis.state, system.status, "online") —
  // три несуществующих ключа и литерал в конце. Из четырёх кандидатов работал ровно
  // последний, поэтому огонёк рядом с именем горел всегда, а подпись всегда читалась
  // «на связи». Теперь и подпись, и огонёк берутся из now.state, а снимок без состояния
  // честно называется снимком без состояния — гасить огонёк на этом правильно.
  const state = nowState(snapshot);
  const online = Boolean(state) && !["offline", "failed", "down"].includes(state.toLowerCase());
  ui.brandState.textContent = boundedText(state ? statusLabel(state) : "состояние не приехало", 45);
  ui.brandPulse.classList.toggle("is-live", online);
}

function activeRuns(snapshot) {
  return runsOf(snapshot).filter((run) => ACTIVE_STATUSES.has(String(run.status || "").toLowerCase()));
}

function attentionRuns(snapshot) {
  return runsOf(snapshot).filter((run) => ATTENTION_STATUSES.has(String(run.status || "").toLowerCase()));
}

function bodyOnline(computer) {
  const status = String(first(computer.status, computer.state, computer.connection, computer.ok === true ? "online" : ""));
  return computer.online === true || computer.ok === true || ["online", "connected", "ready", "healthy"].includes(status.toLowerCase());
}

function followupsOf(telegram) {
  return asList(first(telegram.followups, telegram.obligations, []));
}

// ⚠ 03.08.2026. Правило отбора одно и живёт на сервере (praxis_app._telegram): в счёт
// идут только ЗАКАЗАННЫЕ Егором отчёты, остальные нити — её собственный след, и
// зеркалить его бейджем он просил не надо. Здесь правило было пересказано по памяти
// и разошлось дважды: notify_owner не проверялся вовсе, а !notified_at был
// тождественно истинным (обоих полей в карточке не было). Пересказ убран: признаки
// те же, что у сервера, поэтому бейдж плитки равен telegram.pending_followups, а не
// «всем нитям, какие приехали».
function pendingFollowups(telegram) {
  return followupsOf(telegram).filter((row) => row.notify_owner
    && ["pending", "answered"].includes(String(row.status || "pending").toLowerCase()));
}

function renderNow(snapshot) {
  const computer = computerOf(snapshot);
  const telegram = telegramOf(snapshot);
  const memory = memoryOf(snapshot);
  const system = systemOf(snapshot);
  const now = nowOf(snapshot);
  const active = activeRuns(snapshot);
  const attention = attentionRuns(snapshot);
  const pending = pendingFollowups(telegram);
  const unread = inboxUnread(snapshot);
  const online = bodyOnline(computer);
  // ⚠ 03.08.2026. Пять строк ниже спрашивали praxis.status / praxis.title / praxis.summary
  // и т.д. — ключа snapshot.praxis не существует, поэтому КАЖДАЯ из них печатала свой
  // литерал: «Живой контур», «Praxis на связи», «Состояние собрано из durable runs…».
  // Экран сообщал Егору благополучие вообще без входных данных. Имя переменной status
  // сохранено намеренно: ниже им же кормится ambient.setState({ phase }), и живая краска
  // теперь окрашивается настоящим состоянием, а не литералом.
  const status = nowState(snapshot);
  const known = Boolean(status);
  const healthy = known && !["offline", "failed", "down"].includes(status.toLowerCase());
  // active_runs сервер считает по ВСЕМ манифестам, а active[] — по срезу снимка; при
  // расхождении верить надо счётчику, срез его не видит целиком.
  const openRuns = first(now.active_runs, active.length);

  document.querySelector("#heroDot")?.classList.toggle("is-offline", !healthy);
  setText("#heroEyebrow", known ? `Praxis · ${statusLabel(status)}` : "Состояние не приехало");
  setText("#heroTitle", first(active[0]?.goal, known
    ? (Number(openRuns) > 0 ? `${openRuns} незакрытых прогонов` : "Незакрытых прогонов нет")
    : "Снимок пришёл без состояния"));
  setText("#heroSummary", first(active[0]?.summary, "Состояние собрано из durable runs, server receipts и карт памяти."));
  setText("#heroRuns", openRuns);
  setText("#heroBody", online ? "online" : "offline");
  setText("#heroFollowups", unread);
  setText("#heroRevision", model.revision ? `rev ${model.revision}` : "rev —");

  const moving = active.filter((run) => MOVING_STATUSES.has(String(run.status || "").toLowerCase()));
  const runsHistory = runsTotal(snapshot);
  // ⚠ 03.08.2026. Здесь под словом «в истории» печаталась длина среза, runsOf(...).length:
  // выборка выдавалась за прожитую работу — 74 против 2120 в runs.total, занижение в 28 раз.
  // (Дословную старую строку тут не цитирую: тест ловит её как признак отката.) И ровно она делала
  // раздел «весь красный»: успешных карточек в срезе нет, поэтому история читалась как список
  // аварий. Счётчики «в движении» и «требуют внимания» остаются по срезу — это карточки,
  // которые действительно можно открыть; «всего» — по манифестам сервера.
  setText("#tileRunsTitle", moving[0]?.goal || (active.length ? "Ничего не движется" : (runsHistory ? "Активных нет" : "Пока пусто")));
  setText("#tileRunsSub", `${moving.length} в движении · ${attention.length} требуют внимания · ${runsHistory} всего`);
  showBadge("#tileRunsBadge", attention.length);
  showBadge("#navRunsBadge", attention.length);
  showBadge("#navNowBadge", unread);

  setText("#tileComputerTitle", first(computer.name, computer.device_name, computer.device_id, online ? "Windows online" : "Windows offline"));
  setText("#tileComputerSub", first(computer.summary, computer.execution_summary, online ? "interactive/system routes" : "жду body bridge"));
  document.querySelector("#tileComputerSignal")?.classList.toggle("is-online", online);

  const maps = asList(memory.maps);
  setText("#tileMemoryTitle", maps.length ? `${maps.length} карт` : "Карты памяти");
  // ⚠ 03.08.2026. memory.records в снимке нет и не было: _memory_health отдаёт
  // canonical/maps/index/raw_journal_is_normative. count(undefined) возвращает 0 —
  // не «не знаю», а уверенный ноль, — и плитка писала «0 записей · provenance»
  // при 436 566 кусках и 7 644 источниках в index. Ноль про её память читается как
  // «памяти нет». Берём то, что сервер действительно посчитал, и оговариваем
  // роль индекса: он пересобираемая навигация, а не канон.
  const memoryIndex = object(memory.index);
  const indexChunks = Number(memoryIndex.chunks) || 0;
  setText("#tileMemorySub", first(memory.summary, memory.index_status,
    memoryIndex.available === false
      ? "индекс не собран · канон в Markdown/JSONL"
      : `${indexChunks} кусков индекса · ${Number(memoryIndex.sources) || 0} источников`));

  const rooms = asList(telegram.rooms);
  setText("#tileTelegramTitle", rooms.length ? `${rooms.length} комнат` : "Нити Telegram");
  setText("#tileTelegramSub", pending.length ? `${pending.length} ждут продолжения` : "follow-ups закрыты");
  showBadge("#tileTelegramBadge", pending.length);

  // ⚠ 03.08.2026. Прежняя плитка считала «нездоровые сервисы» в списке, которого нет:
  // system.services не возвращает ни server_state(), ни _system(). Пустой список давал
  // unhealthy = 0 и плитка писала «Контур устойчив · 0 сервисов». Ни то, ни другое не было
  // измерено. Теперь на плитке ровно то, что сервер прочитал из /proc и с диска, а бейдж
  // зажигается только когда сервер ЧЕСТНО сказал, что состояние не прочиталось (_system()
  // кладёт "error" в этот же объект). Отсутствие фактов — не повод для тревоги и не повод
  // для зелёного: это отдельная, названная вслух третья возможность.
  const sys = systemNumbers(system);
  const sysKnown = systemKnown(sys);
  setText("#tileSystemTitle", sys.error ? "Сервер не отдал состояние"
    : sysKnown ? `LA ${sys.load || "—"} · ${sys.cpus || "?"} CPU`
      : "Состояния сервера в снимке нет");
  setText("#tileSystemSub", sys.error ? boundedText(sys.error, 80)
    : sysKnown ? [
      sys.memFreeGb !== null && sys.memTotalGb !== null ? `RAM ${sys.memFreeGb} из ${sys.memTotalGb} ГБ` : "",
      sys.diskFreeGb !== null && sys.diskTotalGb !== null ? `диск ${sys.diskFreeGb} из ${sys.diskTotalGb} ГБ` : "",
      sys.uptimeSec ? `аптайм ${durationText(sys.uptimeSec)}` : "",
    ].filter(Boolean).join(" · ") : "фактов о сервере не приехало");
  showBadge("#navMoreBadge", sys.error ? 1 : 0);

  const observed = first(snapshot.generated_at, snapshot.observed_at, snapshot.updated_at);
  setText("#snapshotAge", observed ? relativeTime(observed) : "live");
  renderSignals(snapshot);
  ambient.setState({ activeRuns: active.length, attention: attention.length + pending.length + unread, bodyOnline: online, phase: status });
}

function inferredSignals(snapshot) {
  const deliveries = inboxItems(snapshot);
  if (deliveries.length) return deliveries.slice(0, 20);
  const source = asList(first(snapshot.signals, snapshot.activity, snapshot.events, []));
  if (source.length) return source.slice(0, 8);
  const rows = [];
  runsOf(snapshot).slice(0, 3).forEach((run) => rows.push({
    kind: "run", title: run.goal || run.run_id || "Run", detail: statusLabel(run.status), at: run.updated_at || run.created_at,
  }));
  asList(computerOf(snapshot).evidence).slice(0, 2).forEach((item) => rows.push({
    kind: "computer", title: item.summary || item.subject || item.capability, detail: item.status, at: item.at,
  }));
  pendingFollowups(telegramOf(snapshot)).slice(0, 2).forEach((item) => rows.push({
    kind: "followup", title: item.target_label || "Follow-up", detail: item.request_text || item.status, at: item.sent_at || item.created_at,
  }));
  return rows;
}

function renderSignals(snapshot) {
  const target = document.querySelector("#signalFeed");
  const rows = inferredSignals(snapshot);
  if (!rows.length) {
    replace(target, emptyState("Входящих пока нет", "Ответы, результаты, готовые файлы и просьбы о решении появятся здесь.", "now"));
    return;
  }
  if (rows.some((row) => String(row.id || "").startsWith("delivery-"))) {
    replace(target, ...rows.map(deliveryCard));
    return;
  }
  const nodes = rows.map((row) => {
    const kind = String(row.kind || row.type || "event");
    const iconName = kind.includes("run") ? "runs" : kind.includes("computer") || kind.includes("body") ? "computer" : kind.includes("follow") || kind.includes("telegram") ? "followup" : kind.includes("memory") ? "memory" : "now";
    const item = element("div", "signal-row");
    const dot = element("span", "signal-row__dot");
    dot.append(svgIcon(iconName));
    const copy = element("span", "signal-row__copy");
    copy.append(
      element("strong", "", first(row.title, row.goal, row.summary, row.kind, "Событие")),
      element("small", "", first(row.detail, row.text, row.status, row.subject, "evidence")),
    );
    const time = element("time", "", relativeTime(first(row.at, row.updated_at, row.created_at)));
    item.append(dot, copy, time);
    return item;
  });
  replace(target, ...nodes);
}

function deliveryCard(item) {
  const status = String(item.status || "queued");
  const kind = String(item.type || "attention");
  const outcome = String(item.outcome || "info");
  const iconName = kind === "file_ready" ? "artifact"
    : kind === "followup_answer" || kind === "social_suggestion" ? "followup"
      : kind === "run_result" ? "runs"
        : kind === "system_alert" ? "warning" : "now";
  const card = element("article", `delivery-card${["queued", "delivered"].includes(status) ? " is-unread" : ""}`);
  card.dataset.deliveryId = String(item.id || "");
  card.dataset.deliveryRevision = String(item.revision || "");

  const head = element("div", "delivery-card__head");
  const sigil = element("span", "delivery-card__sigil");
  sigil.append(svgIcon(iconName));
  const title = element("span", "delivery-card__title");
  title.append(
    element("strong", "", first(item.title, "Сообщение Praxis")),
    element("small", "", [statusLabel(outcome), relativeTime(first(item.updated_at, item.created_at))].join(" · ")),
  );
  head.append(sigil, title, statusPill(status));
  card.append(head);

  const result = boundedText(first(item.result, item.body, ""), 5000);
  if (result) card.append(element("p", "delivery-card__result", result));
  if (item.reason) card.append(deliveryDetail("Почему", item.reason));
  if (item.expectation) card.append(deliveryDetail(outcome === "needs_input" ? "Нужно от тебя" : "Дальше", item.expectation));

  const actions = element("div", "delivery-card__actions");
  const serverAction = object(item.action);
  if (serverAction.action || serverAction.label) {
    const open = element("button", "text-button", first(serverAction.label, "Открыть"));
    open.type = "button";
    open.dataset.deliveryAction = String(item.id || "");
    actions.append(open);
  }
  if (["queued", "delivered"].includes(status)) {
    const read = element("button", "text-button", "Прочитано");
    read.type = "button";
    read.dataset.deliveryTransition = "read";
    read.dataset.deliveryId = String(item.id || "");
    read.dataset.deliveryRevision = String(item.revision || "");
    actions.append(read);
  }
  if (status !== "acted" && status !== "superseded") {
    const acted = element("button", "text-button", "Разобрано");
    acted.type = "button";
    acted.dataset.deliveryTransition = "acted";
    acted.dataset.deliveryId = String(item.id || "");
    acted.dataset.deliveryRevision = String(item.revision || "");
    actions.append(acted);
  }
  if (actions.childElementCount) card.append(actions);
  return card;
}

function deliveryDetail(label, value) {
  const row = element("p", "delivery-card__detail");
  row.append(element("strong", "", label), document.createTextNode(` ${boundedText(value, 2200)}`));
  return row;
}

function deliveryById(deliveryId) {
  return inboxItems().find((item) => String(item.id || "") === String(deliveryId || ""));
}

async function transitionDelivery(button) {
  const target = String(button.dataset.deliveryTransition || "");
  const deliveryId = String(button.dataset.deliveryId || "");
  const expectedRevision = Number(button.dataset.deliveryRevision);
  if (!deliveryId || !["read", "acted"].includes(target) || !Number.isInteger(expectedRevision)) return;
  button.disabled = true;
  const idempotencyKey = String(button.dataset.idempotencyKey || freshIdempotencyKey());
  button.dataset.idempotencyKey = idempotencyKey;
  try {
    await sendCommand("inbox", target, {
      delivery_id: deliveryId,
      expected_revision: expectedRevision,
    }, { quiet: true, idempotencyKey });
    delete button.dataset.idempotencyKey;
    await refreshSnapshot({ quiet: true });
  } catch (error) {
    if (error.idempotencyKey) button.dataset.idempotencyKey = error.idempotencyKey;
    if ([400, 409].includes(Number(error.status))) refreshSnapshot({ quiet: true }).catch(() => {});
    commandError(error);
    button.disabled = false;
  }
}

function openDeliveryAction(deliveryId, trigger) {
  const item = deliveryById(deliveryId);
  if (!item) return;
  const action = object(item.action);
  const correlation = object(item.correlation);
  const runId = String(first(action.run_id, correlation.run_id) || "");
  if ((action.action === "run.open" || item.type === "run_result" || item.type === "file_ready") && runId) {
    openRun(runId, trigger);
    return;
  }
  if (String(action.domain || "") === "telegram" || action.action === "open_followup") {
    navigate("telegram");
    return;
  }
  toast("У этого входящего нет безопасного встроенного действия — детали сохранены в карточке.", "info", 4200);
}

// ⚠ 03.08.2026. Вкладка «Готовые» спрашивала TERMINAL_STATUSES = done + cancelled + failed.
// Успешных карточек в срезе нет вообще — значит вкладка показывала ровно 73 УПАВШИХ прогона
// под словом «готовые», то есть переименовывала поражения в успехи. Это хуже умолчания:
// умолчание скрывает факт, а это его переворачивает. Успех — это done и только done.
const DONE_STATUSES = new Set(["done"]);
// Таблица вместо лесенки if: подпись кнопки в разметке и предикат теперь сверяются тестом
// (test_app_runs.py), иначе они расходятся молча — как и разошлись.
const RUN_FILTER_STATUSES = new Map([
  ["all", null],
  ["active", ACTIVE_STATUSES],
  ["attention", ATTENTION_STATUSES],
  ["done", DONE_STATUSES],
]);

function runMatchesFilter(run) {
  const status = String(run.status || "").toLowerCase();
  const allowed = RUN_FILTER_STATUSES.get(model.runFilter);
  return allowed ? allowed.has(status) : true;
}

function runCard(run) {
  const button = element("button", "stack-card");
  button.type = "button";
  button.dataset.runId = String(run.run_id || run.id || "");
  const iconBox = element("span", "stack-card__icon");
  iconBox.append(svgIcon(String(run.status) === "running" ? "play" : "runs"));
  const body = element("span", "stack-card__body");
  body.append(
    element("strong", "", first(run.goal, run.title, run.run_id, "Run")),
    element("small", "", [boundedText(first(run.kind, run.scope, "execution"), 34), relativeTime(first(run.updated_at, run.created_at))].filter(Boolean).join(" · ")),
  );
  const meta = element("span", "stack-card__meta");
  meta.append(statusPill(run.status));
  if (run.outstanding_call_ids?.length) meta.append(element("small", "quiet-label", `${run.outstanding_call_ids.length} tool`));
  button.append(iconBox, body, meta, svgIcon("arrow", "icon bento-card__arrow"));
  if (String(run.status) === "running") {
    const progress = element("span", "run-progress");
    const bar = element("i");
    const numeric = Number(first(run.progress, run.progress_percent, 0));
    bar.dataset.progress = String(numeric > 0 ? Math.ceil(Math.min(100, numeric) / 10) : 4);
    progress.append(bar);
    button.append(progress);
  }
  return button;
}

// ⚠ 03.08.2026. statusLabel отдаёт именительный падеж единственного числа: «готов», «отменён».
// В строке «Ни одной карточки в срезе: готов (1990)» это читается как обрывок, а не как факт
// про 1990 прогонов. Нужен родительный множественного, и отдельной таблицей: statusLabel живёт
// в плашках статуса каждой карточки, и менять падеж ТАМ значит сломать их ради этой строки.
const RUN_STATUS_PLURAL = {
  done: "успешных", failed: "упавших", cancelled: "отменённых", blocked: "заблокированных",
  paused: "на паузе", in_doubt: "требующих сверки", pending: "ожидающих", running: "идущих",
};

// Сводка по манифестам сервера: пять чисел и одна фраза о границе среза. Стоит НАД списком,
// потому что без неё список — утверждение «вот вся история Праксис», а он ею не является.
function renderRunsSummary(snapshot) {
  const box = document.querySelector("#runsSummary");
  if (!box) return;
  const counts = runsCounts(snapshot);
  if (!Object.keys(counts).length) {
    // Снимок без counts (старый сервер или ошибка сборки среза): молчим. Нарисовать нули
    // здесь значило бы выдумать поле — это ложь на экране, а не деградация.
    box.hidden = true;
    replace(box);
    return;
  }
  box.hidden = false;
  const shown = runsOf(snapshot).length;
  const total = runsTotal(snapshot);
  const metrics = element("div", "runs-summary__metrics");
  [
    [total, "всего"],
    [runsCountOf(snapshot, ["done"]) ?? "—", "успешно"],
    [runsCountOf(snapshot, ["failed"]) ?? "—", "ошибка"],
    [runsCountOf(snapshot, ["cancelled"]) ?? "—", "отменено"],
    [runsCountOf(snapshot, [...ACTIVE_STATUSES]) ?? "—", "открытых"],
  ].forEach(([value, label]) => {
    const cell = element("span");
    cell.append(element("strong", "", value), element("small", "", label));
    metrics.append(cell);
  });
  const note = element(
    "small",
    "runs-summary__note",
    shown >= total
      ? `Показаны все ${total}.`
      : `Показаны ${shown} из ${total}: снимок держит живые и требующие внимания, за остальными нужен отдельный запрос — его в API пока нет.`,
  );
  const missing = runsMissingStatuses(snapshot);
  const gap = missing.length
    ? element(
      "small",
      "runs-summary__note",
      `Ни одной карточки в срезе: ${missing.map(([status, value]) => `${RUN_STATUS_PLURAL[status] || statusLabel(status)} — ${value}`).join(", ")}.`,
    )
    : null;
  replace(box, metrics, note, gap);
}

// ⚠ 03.08.2026. Прежняя пустота говорила «История появится после durable execution» —
// под вкладкой успешных это прямое отрицание 1990 уже прожитых прогонов. Считаем по counts:
// если такие прогоны есть, но в срезе их нет, так и пишем — и называем причину.
function runsEmptyState(snapshot) {
  const allowed = RUN_FILTER_STATUSES.get(model.runFilter);
  const inHistory = allowed ? runsCountOf(snapshot, [...allowed]) : runsTotal(snapshot);
  if (Number.isFinite(inHistory) && inHistory > 0) {
    return emptyState(
      `Таких прогонов ${inHistory}, но их нет в срезе`,
      "Снимок отдаёт карточками только живые и требующие внимания. Открыть остальные из аппа пока нельзя: маршрута за историей в API нет.",
      "runs",
    );
  }
  return emptyState(model.runFilter === "all" ? "Runs пока нет" : "В этом фильтре пусто", "История появится после durable execution.", "runs");
}

function renderRuns(snapshot) {
  const target = document.querySelector("#runsList");
  renderRunsSummary(snapshot);
  const rows = runsOf(snapshot).filter(runMatchesFilter);
  if (!rows.length) {
    replace(target, runsEmptyState(snapshot));
    return;
  }
  replace(target, ...rows.map(runCard));
}

function renderComputer(snapshot) {
  const computer = computerOf(snapshot);
  const target = document.querySelector("#deviceHero");
  const online = bodyOnline(computer);
  const identity = object(computer.identity);
  const manifest = object(computer.manifest);
  const device = object(first(computer.device, asList(computer.devices)[0], computer));
  const head = element("div", "device-head");
  const orb = element("span", `device-orb${online ? " is-online" : ""}`);
  orb.append(svgIcon("computer"));
  const copy = element("span", "device-copy");
  copy.append(
    element("strong", "", first(device.name, device.hostname, device.device_id, computer.device_id, "Windows body")),
    element("small", "", online ? "body bridge на связи" : boundedText(first(computer.error, "body bridge недоступен"), 100)),
  );
  head.append(orb, copy, statusPill(online ? "online" : "offline"));
  const facts = element("div", "device-facts");
  const capabilitySource = first(manifest.capabilities, computer.capabilities, []);
  const capabilityCount = Array.isArray(capabilitySource)
    ? capabilitySource.length
    : Object.values(object(capabilitySource)).filter(Boolean).length;
  // ⚠ 03.08.2026. computer.inventory приезжает в КАЖДОМ снимке с 21.07 (praxis_app._inventory:
  // hostname, os, machine, volumes, tools, apps_count, projects_count, observed_at) и не был
  // показан нигде: единственным вхождением слова inventory во всём app.js было имя команды
  // inventory.refresh. Кнопка «обновить карту машины» существовала, сама карта — нет.
  // Снятая карта (available:false) называется снятой, а не рисуется пустыми значениями.
  const inventory = object(computer.inventory);
  const inventoryOs = object(inventory.os);
  const inventoryFacts = inventory.available ? [
    [boundedText(first(inventory.hostname, "—"), 40), "hostname"],
    [boundedText(first(inventoryOs.caption, inventoryOs.version, "—"), 40), "ОС"],
    [asList(inventory.volumes).length, "томов"],
    [asList(inventory.tools).length, "инструментов"],
    [Number(inventory.apps_count) || 0, "приложений"],
    [Number(inventory.projects_count) || 0, "проектов"],
    [first(inventory.observed_at, inventory.captured_at) ? relativeTime(first(inventory.observed_at, inventory.captured_at)) : "—", "снято"],
  ] : [[inventory.state === "scope_required" ? "нет доступа" : "не собрана", "inventory"]];
  [
    [first(identity.integrity, device.integrity, "—"), "integrity"],
    [first(computer.probe_execution, device.execution, "—"), "route"],
    [capabilityCount, "capabilities"],
    ...inventoryFacts,
  ].forEach(([value, label]) => {
    const fact = element("span", "device-fact");
    fact.append(element("strong", "", value), element("small", "", label));
    facts.append(fact);
  });
  replace(target, head, facts);

  // ⚠ 03.08.2026. computer.processes и computer.operations не существуют: snapshot()
  // собирает computer из _body_status + inventory + evidence + capabilities, перечня
  // запущенного там нет по построению. Раздел поэтому показывал «0» и «Управляемых
  // процессов нет» — фразу, которая утверждает, что процессов НЕТ, тогда как правда в
  // том, что их нечем посмотреть. Догадка про operations убрана, пустота названа своим
  // именем; чтобы здесь появились живые процессы, нужен новый источник в снимке.
  const processes = asList(computer.processes);
  setText("#processCount", processes.length ? processes.length : "—");
  const processTarget = document.querySelector("#processList");
  replace(processTarget, ...(processes.length ? processes.map(processCard) : [emptyState("Снимок не несёт списка процессов", "Тело отвечает на probe, но перечня запущенного в snapshot нет. Запущенное самой Praxis — в разделе Runs.", "process")]));

  const artifacts = asList(first(computer.artifacts, computer.evidence, []));
  setText("#artifactCount", artifacts.length);
  const artifactTarget = document.querySelector("#artifactList");
  replace(artifactTarget, ...(artifacts.length ? artifacts.map(artifactCard) : [emptyState("Артефактов пока нет", "Файлы и screenshots остаются hash-addressed evidence.", "artifact")]));

  document.querySelectorAll("#view-computer [data-command], #view-computer [data-stop-process]").forEach((button) => {
    const token = button.dataset.command || "process.cancel";
    const required = token.startsWith("process.") ? "computer.process"
      : token.startsWith("files.") ? "computer.files"
        : token.startsWith("desktop.") ? "computer.apps"
          : "computer.read";
    const allowed = hasScope(required, snapshot);
    button.hidden = !allowed;
    button.disabled = !allowed;
  });
}

function processCard(process) {
  const card = element("div", "stack-card");
  const iconBox = element("span", "stack-card__icon");
  iconBox.append(svgIcon("process"));
  const body = element("span", "stack-card__body");
  body.append(
    element("strong", "", first(process.name, process.command, process.operation_id, "Процесс")),
    element("small", "", [first(process.operation_id, process.pid), first(process.exit_code !== undefined ? `exit ${process.exit_code}` : "", process.cwd)].filter(Boolean).join(" · ")),
  );
  const meta = element("span", "stack-card__meta");
  meta.append(statusPill(first(process.status, process.state, "running")));
  const status = String(first(process.status, process.state) || "").toLowerCase();
  if (!TERMINAL_STATUSES.has(status) && process.operation_id) {
    const stop = element("button", "text-button", "Стоп");
    stop.type = "button";
    stop.dataset.stopProcess = String(process.operation_id);
    stop.setAttribute("aria-label", `Остановить ${first(process.name, process.operation_id)}`);
    meta.append(stop);
  }
  card.append(iconBox, body, meta);
  return card;
}

function safeDownload(value) {
  try {
    const url = new URL(String(value || ""), location.origin);
    return url.origin === location.origin && url.pathname.startsWith(`${API}/`) ? url.href : "";
  } catch (_) {
    return "";
  }
}

function artifactCard(artifact) {
  const card = element("div", "stack-card");
  const iconBox = element("span", "stack-card__icon");
  iconBox.append(svgIcon("artifact"));
  const body = element("span", "stack-card__body");
  body.append(
    // ⚠ 03.08.2026. Эта же карточка рисует ДВА разных потока. У артефактов run’а
    // есть name/size/sha256; у computer.evidence (praxis_app._computer_evidence)
    // их нет вовсе — там id/at/capability/status/subject/summary. Ни одного из
    // прежних ключей не совпадало, поэтому все 13 строк раздела назывались словом
    // «Артефакт», а подписью шло «— · 2д»: минус приезжал из bytes(undefined),
    // который возвращает «—» и проходит filter(Boolean) как настоящее значение.
    // Экран показывал ровно тринадцать одинаковых строк там, где лежат тринадцать
    // разных доказательств. Ключи evidence дописаны В КОНЕЦ цепочки — приоритет
    // артефактов не тронут, а размер печатается только когда он есть.
    element("strong", "", first(artifact.name, artifact.filename, artifact.artifact_id, artifact.subject, artifact.capability, "Артефакт")),
    element("small", "", [
      artifact.size !== undefined ? bytes(artifact.size) : "",
      artifact.sha256 ? `sha ${String(artifact.sha256).slice(0, 10)}` : "",
      boundedText(first(artifact.summary, artifact.capability, ""), 140),
      relativeTime(first(artifact.at, artifact.created_at)),
    ].filter(Boolean).join(" · ")),
  );
  card.append(iconBox, body);
  const href = !model.snapshotStale && navigator.onLine
    ? safeDownload(first(artifact.download_url, artifact.href))
    : "";
  if (href) {
    const link = element("a", "icon-button");
    link.href = href;
    link.download = boundedText(first(artifact.name, artifact.filename, "artifact"), 120);
    link.setAttribute("aria-label", `Скачать ${first(artifact.name, "артефакт")}`);
    link.append(svgIcon("download"));
    card.append(link);
  } else {
    card.append(statusPill(first(artifact.status, "accepted")));
  }
  return card;
}

function artifactPreview(artifact) {
  const mediaType = String(artifact.media_type || "").toLowerCase().split(";", 1)[0].trim();
  const href = !model.snapshotStale && navigator.onLine
    ? safeDownload(first(artifact.download_url, artifact.href))
    : "";
  if (!href || !["image/png", "image/jpeg", "image/webp", "image/gif"].includes(mediaType)) {
    return artifactCard(artifact);
  }
  const figure = element("figure", "artifact-preview");
  const image = element("img");
  image.src = href;
  image.alt = boundedText(first(artifact.name, artifact.filename, "Screen capture"), 160);
  image.loading = "eager";
  image.decoding = "async";
  const caption = element("figcaption");
  caption.append(
    element("strong", "", first(artifact.name, artifact.filename, "Screen capture")),
    element("small", "", [bytes(artifact.size), artifact.sha256 ? `sha ${String(artifact.sha256).slice(0, 12)}` : ""].filter(Boolean).join(" · ")),
  );
  figure.append(image, caption);
  return figure;
}

function renderMemory(snapshot) {
  const memory = memoryOf(snapshot);
  const health = document.querySelector("#memoryHealth");
  const index = object(first(memory.index, memory.fts, {}));
  const row = element("div", "health-row");
  const ring = element("span", "health-ring");
  ring.append(svgIcon("memory"));
  const copy = element("span", "health-copy");
  copy.append(
    element("strong", "", first(memory.status_label, memory.status === "error" ? "Индекс требует внимания" : "Навигация пересобираема")),
    element("small", "", first(memory.summary, "Markdown/JSONL — канон; карты и SQL — проекции.")),
  );
  // ⚠ 03.08.2026. Ни memory.status, ни index.status в снимке не существует —
  // значит пилюля печатала литерал "healthy" ВСЕГДА, включая случай, когда
  // recall.sqlite3 отсутствует или не читается. Сервер про это честен и отдаёт
  // index.available и index.error; зелёный без источника хуже отсутствия пилюли,
  // потому что закрывает вопрос. Отдельно: available:false здесь НЕ авария —
  // индекс пересобираем, канон лежит в Markdown/JSONL, поэтому статус «ослаблен»,
  // а не «ошибка».
  const indexHealth = index.error ? "failed"
    : index.available === false ? "degraded" : "healthy";
  row.append(ring, copy, statusPill(first(memory.status, index.status, indexHealth)));
  const metrics = element("div", "health-metrics");
  [
    [first(index.chunks, memory.fts_chunks, 0), "chunks"],
    [first(index.sources, memory.fts_sources, 0), "sources"],
    [first(memory.provenance_percent, memory.sourced_percent, "—"), "provenance"],
  ].forEach(([value, label]) => {
    const metric = element("span");
    metric.append(element("strong", "", value), element("small", "", label));
    metrics.append(metric);
  });
  replace(health, row, metrics);

  const defaultMaps = ["PEOPLE", "ROOMS", "PROJECTS", "THREADS", "RUNS", "COMPUTERS"].map((name) => ({ name }));
  const maps = asList(memory.maps);
  const target = document.querySelector("#memoryMaps");
  // ⚠ 03.08.2026. Сетка карт рисовалась безусловно, хотя соседняя строка уже прячет
  // поиск без scope praxis.snapshot. Зритель без этого scope получал шесть нажимаемых
  // карточек, и каждая отвечала 403: гейт у чтения карты ровно тот же — ветка
  // map.read в praxis_app требует praxis.snapshot.
  const mayReadMemory = hasScope("praxis.snapshot", snapshot);
  replace(target, ...(mayReadMemory
    ? (maps.length ? maps : defaultMaps).map(mapCard)
    : [emptyState("Карты закрыты", "Чтение карт памяти требует доступа praxis.snapshot.", "map")]));
  document.querySelector("#memorySearch").hidden = !mayReadMemory;
  const rebuild = document.querySelector("[data-command='memory.rebuild']");
  rebuild.hidden = !hasScope("praxis.work", snapshot);
  rebuild.disabled = rebuild.hidden;
  // Снимок мог прийти, когда владелец уже стоит на «Памяти» — тогда монтируем сейчас.
  // На скрытой вкладке не монтируем никогда: укладка графа сгорела бы вхолостую.
  if (model.currentView === "memory") mountMemoryViews(snapshot);
}

// Эндпоинты памяти открыты тем же scope'ом praxis.snapshot, что и карты с поиском
// выше (Viewer.public отдаёт секцию памяти ровно по нему) — иначе установленный PWA,
// где сессия всегда role="device", получал бы 403 именно там, ради чего всё делалось.
function mountMemoryViews(snapshot) {
  if (memoryViewsMounted || !hasScope("praxis.snapshot", snapshot) || model.snapshotStale) return;
  const view = document.querySelector("#view-memory");
  if (!view) return;
  let mount = document.querySelector("#memoryViews");
  if (!mount) {
    mount = element("div", "memory-views-mount");
    mount.id = "memoryViews";
    view.append(mount);
  }
  memoryViewsMounted = true;
  memoryViews = initMemoryViews({
    mount,
    api: (path) => api(path),
    sheet: (title, body) => openSheet({ title, eyebrow: "Память", content: body }),
    // Хост полноэкранного слоя лежит в #app, но вне `.view` — почему именно так,
    // расписано в praxisapp.html. Если его нет (оболочка из старого кэша),
    // initMemoryViews просто спрячет кнопку «Развернуть»: мёртвой она не будет.
    fullscreenHost: document.querySelector("#stageFullscreen"),
    onFullscreen: (active) => {
      model.memoryFullscreen = Boolean(active);
      updateBack();
    },
    onError: (error) => {
      if (error?.status === 401 || error?.sessionLocked || error?.network) return;
      toast(error?.message || "Память не отдала проекцию", "error", 3600);
    },
  });
}

// ⚠ 03.08.2026. Карточка читала у карты поле count — которого в снимке нет и не было:
// praxis_app._memory_health отдаёт про карту ровно {id, name, available, updated_at,
// bytes}. Ветка была мёртвой, зато подпись «Открыть bounded map» одинаково бодро
// стояла и под живой картой, и под отсутствующим файлом: карта с available:false
// выглядела открываемой и по нажатию отвечала голым not_found. Теперь карточка
// говорит только то, что снимок знает — размер и когда карта обновлялась.
function mapCard(map) {
  const card = element("button", "map-card");
  card.type = "button";
  const name = String(first(map.name, map.id, map.slug, "MAP")).toUpperCase();
  card.dataset.memoryMap = name;
  card.append(svgIcon(name === "COMPUTERS" ? "computer" : name === "RUNS" ? "runs" : "map"));
  const available = map.available !== false;
  const facts = available
    ? [map.bytes ? bytes(map.bytes) : "", map.updated_at ? relativeTime(map.updated_at) : ""]
      .filter(Boolean).join(" · ")
    : "файла нет на диске";
  card.append(element("strong", "", name), element("small", "", facts || "Открыть карту"));
  card.disabled = !available;
  return card;
}

function renderTelegram(snapshot) {
  const telegramState = telegramOf(snapshot);
  const followups = followupsOf(telegramState);
  const rooms = asList(telegramState.rooms);
  // ⚠ 03.08.2026. Здесь стояла карточка «Социальный пульс» с зелёной пилюлей «здоров»,
  // собранная ЦЕЛИКОМ из литералов: ни social_pulse, ни pulse снимок не отдаёт
  // (_telegram — это ровно rooms, followups, pending_followups, membership). Заголовок,
  // подпись «часовой обзор обещаний и ответов» и статус были написаны здесь и здесь же
  // прочитаны — утверждение о здоровье механизма без единого факта о нём. Пока источника
  // нет, карточка говорит только то, что действительно приехало, — состав раздела, — а
  // молчание о самом пульсе названо вслух, а не закрашено зелёным.
  const pulseTarget = document.querySelector("#socialPulse");
  const ordered = Number(telegramState.pending_followups);
  const row = element("div", "social-row");
  const sigil = element("span", "social-sigil");
  sigil.append(svgIcon("telegram"));
  const copy = element("span", "social-copy");
  copy.append(
    element("strong", "", `${rooms.length} комнат · ${followups.length} нитей`),
    element("small", "", Number.isFinite(ordered)
      ? `${ordered} заказанных отчётов ждут ответа · о самом пульсе снимок не сообщает`
      : "о состоянии социального пульса снимок не сообщает"),
  );
  row.append(sigil, copy);
  replace(pulseTarget, row);

  setText("#followupCount", followups.length);
  const followupTarget = document.querySelector("#followupList");
  replace(followupTarget, ...(followups.length ? followups.map(followupCard) : [emptyState("Открытых ожиданий нет", "Когда Praxis напишет кому-то по просьбе, нить появится здесь.", "followup")]));

  const roomTarget = document.querySelector("#roomList");
  replace(roomTarget, ...(rooms.length ? rooms.map(roomCard) : [emptyState("Комнаты ещё не синхронизированы", "Вход и выход проходят через durable membership ledger.", "telegram")]));

  const transactions = asList(first(telegramState.membership, telegramState.membership_transactions, []));
  const membershipTarget = document.querySelector("#membershipList");
  replace(membershipTarget, ...(transactions.length ? transactions.map(membershipCard) : [emptyState("Транзакций членства нет", "Новые join/leave intents будут fsync-нуты до Telethon.", "clock")]));
  const allowed = hasScope("praxis.telegram", snapshot);
  document.querySelector("#joinRoomButton").hidden = !allowed;
  document.querySelectorAll("#view-telegram [data-cancel-followup], #view-telegram [data-leave-room]").forEach((button) => {
    button.hidden = !allowed;
    button.disabled = !allowed;
  });
}

function followupCard(item) {
  const card = element("div", "stack-card");
  const iconBox = element("span", "stack-card__icon");
  iconBox.append(svgIcon("followup"));
  const body = element("span", "stack-card__body");
  body.append(
    element("strong", "", first(item.target_label, item.target_ref, "Follow-up")),
    element("small", "", first(item.answer_preview, item.response?.text, item.request_text, "Ожидает конкретный ответ")),
  );
  const meta = element("span", "stack-card__meta");
  meta.append(statusPill(item.status || "pending"));
  if (["pending", "answered"].includes(String(item.status)) && item.id) {
    const cancel = element("button", "text-button", "Снять");
    cancel.type = "button";
    cancel.dataset.cancelFollowup = String(item.id);
    meta.append(cancel);
  }
  card.append(iconBox, body, meta);
  return card;
}

// Тон пилюли — это оформление, а не пересказ: слово в пилюлю кладёт сервер.
const ROOM_MODE_TONE = { frozen: "red", dead: "red", quiet: "gold", observer: "violet", normal: "green" };

function roomCard(room) {
  const card = element("div", "stack-card");
  const iconBox = element("span", "stack-card__icon");
  iconBox.append(svgIcon("telegram"));
  const body = element("span", "stack-card__body");
  // ⚠ 03.08.2026. Пилюля просила room.status (такого ключа нет), падала на room.mode и
  // печатала сырой идентификатор: statusLabel не знает ни frozen, ни quiet, ни observer,
  // ни dead, ни normal, поэтому на экране Егора стояла латиница. Рядом лежало готовое
  // русское mode_word, а с ним — весь провенанс, ради которого он 28.07 и добавлялся:
  // причина режима, срок, ЧЕЙ это режим и раскрыта ли её визитка. Панель свой пересказ
  // тогда же убрала — «два пересказа одного факта расходятся всегда»; убираем и здесь.
  // Комната, которую ей молча притушили, не должна выглядеть как её собственный выбор.
  const mode = String(room.mode || "").toLowerCase();
  const detail = [
    room.reason ? boundedText(room.reason, 90) : "",
    room.until ? `до ${relativeTime(room.until)}` : "",
    room.author ? `режим · ${boundedText(room.author, 40)}` : "",
    String(room.disclosure || "") === "open"
      ? `визитка раскрыта${room.disclosure_author ? ` (${boundedText(room.disclosure_author, 40)})` : ""}`
      : "",
  ].filter(Boolean).join(" · ");
  body.append(
    element("strong", "", first(room.title, room.name, room.id, "Комната")),
    element("small", "", detail || "повода и срока у режима не записано"),
  );
  const meta = element("span", "stack-card__meta");
  const modePill = element("span", "status-pill", boundedText(first(room.mode_word, room.mode, "режим неизвестен"), 38));
  modePill.dataset.tone = ROOM_MODE_TONE[mode] || "cyan";
  meta.append(modePill);
  if (room.can_leave !== false && first(room.id, room.peer_id)) {
    const leave = element("button", "text-button", "Выйти");
    leave.type = "button";
    leave.dataset.leaveRoom = String(first(room.id, room.peer_id));
    meta.append(leave);
  }
  card.append(iconBox, body, meta);
  return card;
}

function membershipCard(item) {
  const card = element("div", "stack-card");
  const iconBox = element("span", "stack-card__icon");
  iconBox.append(svgIcon(item.action === "leave" ? "back" : "telegram"));
  const body = element("span", "stack-card__body");
  body.append(
    element("strong", "", `${item.action === "leave" ? "Выход" : "Вход"}: ${first(item.target, item.title, "Telegram")}`),
    element("small", "", [first(item.id, item.tx_id), relativeTime(first(item.updated_at, item.intent_at))].filter(Boolean).join(" · ")),
  );
  card.append(iconBox, body, statusPill(first(item.status, "intent")));
  return card;
}

function grantsOf(snapshot) {
  const access = object(first(snapshot.access, snapshot.trust, {}));
  if (Array.isArray(access.grants)) return access.grants;
  if (access.grants && typeof access.grants === "object") return Object.values(access.grants);
  return asList(access);
}

function devicesOf(snapshot) {
  const access = object(first(snapshot.access, snapshot.trust, {}));
  return asList(first(access.devices, snapshot.devices, []));
}

function installedStandalone() {
  return Boolean(window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone);
}

function renderMore(snapshot) {
  const owner = isOwner(snapshot);
  const access = object(first(snapshot.access, snapshot.trust, {}));
  const target = document.querySelector("#trustSummary");
  const row = element("div", "trust-row");
  const sigil = element("span", "trust-sigil");
  sigil.append(svgIcon("trust"));
  const copy = element("span", "trust-copy");
  const grants = grantsOf(snapshot);
  copy.append(
    element("strong", "", owner ? "Владелец — корень доверия" : "Доступ выдан владельцем"),
    element("small", "", owner ? `${grants.length} доверенных · делегирование запрещено` : `Ваши scopes: ${asList(session(snapshot).scopes).join(", ") || "нет"}`),
  );
  row.append(sigil, copy, statusPill(owner ? "healthy" : "accepted"));
  replace(target, row);
  document.querySelector("#grantButton").hidden = !owner;

  const grantTarget = document.querySelector("#grantList");
  replace(grantTarget, ...(grants.length ? grants.map((grant) => grantCard(grant, owner)) : [emptyState("Доверенных людей нет", "Только владелец может выдать точные computer scopes.", "trust")]));

  const authLabel = telegram.initData
    ? "Telegram WebApp · подписанная сессия"
    : model.deviceAuth
      ? `${model.deviceAuth.label} · Bearer из защищённого хранилища браузера`
      : "Это устройство ещё не привязано";
  const deviceRow = element("div", "trust-row");
  const deviceSigil = element("span", "trust-sigil");
  deviceSigil.append(svgIcon("computer"));
  const deviceCopy = element("span", "trust-copy");
  deviceCopy.append(
    element("strong", "", installedStandalone() ? "Praxis установлена" : "Praxis в браузере"),
    element("small", "", authLabel),
  );
  deviceRow.append(deviceSigil, deviceCopy, statusPill(model.deviceAuth || telegram.initData ? "accepted" : "offline"));
  replace(ui.deviceSummary, deviceRow);
  ui.install.hidden = !model.installPrompt || installedStandalone();
  ui.enroll.hidden = !model.enrollmentToken;
  ui.newDevice.hidden = !owner;

  const devices = devicesOf(snapshot);
  replace(ui.deviceList, ...(devices.length
    ? devices.map((device) => deviceCard(device, owner))
    : [emptyState(owner ? "Сервер не прислал привязанные устройства" : "Устройства видит только владелец", "Одноразовая ссылка привязывает PWA без копирования токена в URL или localStorage.", "computer")]));

  const system = systemOf(snapshot);
  setText("#systemHead", first(system.head, system.commit, "HEAD —"));
  // ⚠ 03.08.2026. Раздел назывался «Сервисы» и ждал system.services — ключа, которого
  // server_state() не возвращает никогда. Итог: вечная заглушка «Сервисы не прислали
  // состояние» под пустым разделом, при том что рядом, в том же объекте, лежали
  // нагрузка, память, диск, аптайм и возраст трёх логов. Заглушка была честной по форме
  // и лживой по существу: сервер прислал ровно то, что умеет, спрашивали не о том.
  const sys = systemNumbers(system);
  const factRows = [];
  if (sys.error) factRows.push(["Состояние сервера не прочиталось", boundedText(sys.error, 160), "failed"]);
  if (sys.loadFull) factRows.push(["Нагрузка", `${sys.loadFull} · ${sys.cpus || "?"} CPU`, ""]);
  if (sys.memTotalGb !== null) factRows.push(["Память", `${sys.memFreeGb} ГБ свободно из ${sys.memTotalGb} ГБ`, ""]);
  if (sys.diskTotalGb !== null) factRows.push(["Диск", `${sys.diskFreeGb} ГБ свободно из ${sys.diskTotalGb} ГБ`, ""]);
  if (sys.uptimeSec) factRows.push(["Аптайм процесса", durationText(sys.uptimeSec), ""]);
  sys.logs.forEach(([name, row]) => factRows.push([
    `Лог ${name}`,
    [
      Number.isFinite(Number(row.age_sec)) ? `последняя запись ${durationText(row.age_sec)} назад` : "",
      Number.isFinite(Number(row.size_kb)) ? `${Number(row.size_kb)} КБ` : "",
    ].filter(Boolean).join(" · "),
    "",
  ]));
  const serviceTarget = document.querySelector("#serviceList");
  replace(serviceTarget, ...(factRows.length
    ? factRows.map(([title, detail, tone]) => systemFactCard(title, detail, tone))
    : [emptyState("Сервер не прислал состояние", "panel.server_state() читает /proc и диск хоста; если фактов нет — подставить их нечем.", "system")]));
  document.querySelectorAll("#view-more [data-command]").forEach((button) => {
    const scope = button.dataset.command === "system.restart"
      ? "praxis.system.control"
      : "praxis.system.read";
    const allowed = hasScope(scope, snapshot);
    button.hidden = !allowed;
    button.disabled = !allowed;
  });
}

function grantCard(grant, owner) {
  const card = element("div", "stack-card");
  const iconBox = element("span", "stack-card__icon");
  iconBox.append(svgIcon("trust"));
  const body = element("span", "stack-card__body");
  body.append(
    element("strong", "", first(grant.name, grant.principal, "Доверенный")),
    element("small", "", asList(grant.scopes).join(" · ") || "scopes не указаны"),
  );
  const meta = element("span", "stack-card__meta");
  meta.append(statusPill("accepted"));
  if (owner && grant.principal) {
    const revoke = element("button", "text-button", "Отозвать");
    revoke.type = "button";
    revoke.dataset.revoke = String(grant.principal).replace(/^telegram:/, "");
    meta.append(revoke);
  }
  card.append(iconBox, body, meta);
  return card;
}

function deviceCard(device, owner) {
  const card = element("div", "stack-card");
  const iconBox = element("span", "stack-card__icon");
  iconBox.append(svgIcon("computer"));
  const deviceId = String(first(device.device_id, device.id) || "");
  const body = element("span", "stack-card__body");
  body.append(
    element("strong", "", first(device.label, device.name, deviceId, "Устройство")),
    element("small", "", [deviceId, relativeTime(first(device.last_seen_at, device.last_seen, device.enrolled_at, device.issued_at))].filter(Boolean).join(" · ")),
  );
  const meta = element("span", "stack-card__meta");
  meta.append(statusPill(first(device.status, device.revoked_at ? "cancelled" : "online")));
  if (owner && deviceId && !device.revoked_at) {
    const revoke = element("button", "text-button", "Отозвать");
    revoke.type = "button";
    revoke.dataset.revokeDevice = deviceId;
    meta.append(revoke);
  }
  card.append(iconBox, body, meta);
  return card;
}

// ⚠ 03.08.2026. Здесь была serviceCard — карточка для строк system.services. Раз такого
// ключа нет и никогда не было, карточка не вызывалась ни разу за всё время жизни аппа, но
// исправно подсказывала следующему читателю, что сервисы «вот-вот приедут». Заменена
// карточкой факта: она печатает то, что сервер измерил, и пилюлю показывает только тогда,
// когда состояние ему известно.
function systemFactCard(title, detail, tone) {
  const card = element("div", "stack-card");
  const iconBox = element("span", "stack-card__icon");
  iconBox.append(svgIcon("system"));
  const body = element("span", "stack-card__body");
  body.append(element("strong", "", title), element("small", "", detail));
  card.append(iconBox, body);
  if (tone) card.append(statusPill(tone));
  return card;
}

function renderOffline(error) {
  const feed = document.querySelector("#signalFeed");
  replace(feed, errorState("Snapshot недоступен", boundedText(error?.message || "Нет ответа от server-side Praxis", 220)));
  ambient.setState({ activeRuns: 0, attention: 1, bodyOnline: false, phase: "offline" });
}

function scrubProtectedSurface(reason) {
  model.history = [];
  model.currentView = "now";
  model.sheetOpen = false;
  model.sheetReturnFocus = null;
  ui.sheetBody.replaceChildren();
  ui.sheet.classList.remove("is-visible");
  ui.sheet.hidden = true;
  ui.scrim.classList.remove("is-visible");
  ui.scrim.hidden = true;
  ui.app.inert = false;
  ui.app.classList.remove("is-stale");
  model.snapshotStale = false;
  model.verifiedAt = 0;
  ui.stale.hidden = true;
  ui.refresh.hidden = true;
  document.querySelectorAll("[data-view]").forEach((view) => {
    view.classList.toggle("view--active", view.dataset.view === "now");
    view.classList.remove("view--leaving");
  });
  document.querySelectorAll("[data-nav]").forEach((button) => {
    const active = button.dataset.nav === "now";
    button.hidden = !active;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-open]").forEach((button) => { button.hidden = true; });
  destroyMemoryViews();          // сначала свернёт фуллскрин и вернёт сцену в ленту
  memoryViewsMounted = false;
  memoryViews = null;
  model.memoryFullscreen = false;
  [
    "#runsList", "#runsSummary", "#deviceHero", "#processList", "#artifactList", "#memoryHealth",
    "#memoryMaps", "#memoryResults", "#socialPulse", "#followupList", "#roomList",
    "#membershipList", "#trustSummary", "#grantList", "#deviceSummary", "#deviceList",
    "#serviceList",
  ].forEach((selector) => document.querySelector(selector)?.replaceChildren());
  // replaceChildren() убирает содержимое, но не сам блок: .runs-summary — коробка glass с рамкой,
  // и после первого renderRuns она живёт с hidden=false. Без этой строки после реавторизации и
  // до первого снимка на экране висела бы пустая рамка ни о чём. Возвращаем ровно то состояние,
  // в котором сводка приезжает из разметки (hidden), — второй половиной той же уборки.
  document.querySelector("#runsSummary")?.setAttribute("hidden", "");
  [
    ["#tileRunsTitle", "—"], ["#tileRunsSub", "—"],
    ["#tileComputerTitle", "—"], ["#tileComputerSub", "—"],
    ["#tileMemoryTitle", "—"], ["#tileMemorySub", "—"],
    ["#tileTelegramTitle", "—"], ["#tileTelegramSub", "—"],
    ["#tileSystemTitle", "—"], ["#tileSystemSub", "—"],
    ["#snapshotAge", "—"], ["#processCount", "—"], ["#artifactCount", "—"],
    ["#followupCount", "—"], ["#systemHead", "HEAD —"],
  ].forEach(([selector, value]) => setText(selector, value));
  ["#tileRunsBadge", "#navRunsBadge", "#navNowBadge", "#tileTelegramBadge", "#navMoreBadge"]
    .forEach((selector) => showBadge(selector, 0));
  document.querySelector("#tileComputerSignal")?.classList.remove("is-online");
  const memoryQuery = document.querySelector("#memoryQuery");
  if (memoryQuery) memoryQuery.value = "";
  setText("#heroEyebrow", "Сессия закрыта");
  setText("#heroTitle", "Нужна новая авторизация");
  setText("#heroSummary", reason || "Доступ к локально сохранённому состоянию закрыт.");
  setText("#heroRuns", "—");
  setText("#heroBody", "locked");
  setText("#heroFollowups", "—");
  setText("#heroRevision", "rev —");
  ui.brandState.textContent = "доступ не подтверждён";
  ui.brandPulse.classList.remove("is-live");
  renderOffline(new Error(reason || "Нужна авторизация"));
  updateBack();
}

function navigate(view, { push = true } = {}) {
  const views = Array.from(document.querySelectorAll("[data-view]"));
  const next = views.find((node) => node.dataset.view === view);
  if (!next || view === model.currentView) return;
  const current = views.find((node) => node.dataset.view === model.currentView);
  if (push) model.history.push(model.currentView);
  if (current) {
    current.classList.remove("view--active");
    current.classList.add("view--leaving");
    window.setTimeout(() => current.classList.remove("view--leaving"), 250);
  }
  next.classList.add("view--active");
  next.scrollTop = 0;
  model.currentView = view;
  if (view === "memory") mountMemoryViews(model.snapshot || {});
  document.querySelectorAll("[data-nav]").forEach((button) => {
    const active = button.dataset.nav === view || (view === "telegram" && button.dataset.nav === "more");
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  updateBack();
  telegram.haptic("light");
  window.setTimeout(() => ui.main.focus({ preventScroll: true }), 20);
}

function back() {
  if (model.sheetOpen) {
    closeSheet();
    return;
  }
  // ⚠ Порядок веток — смысловой, а не косметический. Досье узла открывается ПОВЕРХ
  // развёрнутой сцены (шторка — сиблинг #app с z-index 61), поэтому первый «назад»
  // закрывает шторку, второй сворачивает сцену и только третий уводит из раздела.
  // Поменяй местами — и владелец уедет из фуллскрина в предыдущий вид, а сцена
  // останется висеть поверх него, перекрывая и топбар, и док.
  if (model.memoryFullscreen) {
    memoryViews?.exitFullscreen?.();
    return;
  }
  const previous = model.history.pop();
  if (previous) navigate(previous, { push: false });
  else if (model.currentView !== "now") navigate("now", { push: false });
}

function updateBack() {
  // Развёрнутая сцена закрывает собой и топбар, и док. Не будь её в этом списке,
  // Telegram спрятал бы BackButton — и на «Сейчас» без истории выйти из фуллскрина
  // было бы нечем, кроме кнопки «Свернуть».
  const visible = model.sheetOpen || model.memoryFullscreen
    || model.history.length > 0 || model.currentView !== "now";
  ui.back.hidden = !visible;
  telegram.setBack(visible);
}

function openSheet({ title, eyebrow = "", content, returnFocus = document.activeElement }) {
  ui.sheetTitle.textContent = boundedText(title, 140);
  ui.sheetEyebrow.textContent = boundedText(eyebrow, 80);
  replace(ui.sheetBody, content || emptyState("Нет данных"));
  ui.scrim.hidden = false;
  ui.sheet.hidden = false;
  model.sheetReturnFocus = returnFocus instanceof HTMLElement ? returnFocus : null;
  model.sheetOpen = true;
  ui.app.inert = true;
  requestAnimationFrame(() => {
    ui.scrim.classList.add("is-visible");
    ui.sheet.classList.add("is-visible");
    ui.sheetClose.focus({ preventScroll: true });
  });
  updateBack();
}

function closeSheet() {
  if (!model.sheetOpen) return;
  model.sheetOpen = false;
  ui.scrim.classList.remove("is-visible");
  ui.sheet.classList.remove("is-visible");
  window.setTimeout(() => {
    ui.scrim.hidden = true;
    ui.sheet.hidden = true;
    ui.sheetBody.replaceChildren();
    if (!model.sheetOpen) ui.app.inert = false;
    model.sheetReturnFocus?.focus?.({ preventScroll: true });
    model.sheetReturnFocus = null;
  }, 270);
  updateBack();
}

function detailCell(label, value) {
  const cell = element("div", "detail-cell");
  cell.append(element("small", "", label), element("strong", "", value));
  return cell;
}

async function openRun(runId, trigger) {
  if (!runId) return;
  openSheet({ title: "Run", eyebrow: "Durable execution", content: loadingState("Читаю evidence"), returnFocus: trigger });
  try {
    const detail = object(await api(`/runs/${encodeURIComponent(runId)}`));
    const run = { ...detail, ...object(detail.run) };
    for (const key of ["events", "artifacts", "recap"]) {
      if (detail[key] !== undefined) run[key] = detail[key];
    }
    renderRunDetail(run, trigger);
  } catch (error) {
    replace(ui.sheetBody, errorState("Run не загрузился", error.message));
  }
}

function renderRunDetail(run, trigger) {
  ui.sheetTitle.textContent = boundedText(first(run.goal, run.title, run.run_id, "Run"), 140);
  const container = element("div");
  const grid = element("div", "detail-grid");
  grid.append(
    detailCell("status", statusLabel(run.status)),
    detailCell("revision", first(run.revision, "—")),
    detailCell("kind", first(run.kind, "—")),
    detailCell("scope", first(run.scope, "—")),
    detailCell("principal", first(run.principal_id, "—")),
    detailCell("created", exactTime(run.created_at)),
  );
  container.append(grid);
  const context = first(run.context_summary, run.context?.summary, run.summary);
  if (context) {
    const section = element("section", "detail-section");
    section.append(element("h3", "", "Контекст"), element("p", "detail-copy", boundedText(context, 5000)));
    container.append(section);
  }
  const recap = String(run.recap || "").trim();
  if (recap) {
    const section = element("section", "detail-section");
    section.append(
      element("h3", "", "RECAP"),
      element("pre", "recap-copy", boundedText(recap, 64000)),
    );
    container.append(section);
  }
  const events = asList(first(run.events, run.timeline, []));
  if (events.length) {
    const section = element("section", "detail-section");
    section.append(element("h3", "", "Evidence timeline"));
    const timeline = element("div", "timeline");
    events.slice(-80).forEach((event) => {
      const item = element("div", "timeline-row");
      item.append(
        element("strong", "", first(event.title, event.kind, event.type, "event")),
        element("small", "", [boundedText(first(event.summary, event.detail, event.status, ""), 260), exactTime(first(event.at, event.created_at))].filter(Boolean).join(" · ")),
      );
      timeline.append(item);
    });
    section.append(timeline);
    container.append(section);
  }
  const artifacts = asList(run.artifacts);
  if (artifacts.length) {
    const section = element("section", "detail-section");
    section.append(element("h3", "", `Артефакты · ${artifacts.length}`));
    const list = element("div", "stack-list");
    artifacts.forEach((artifact) => list.append(artifactCard(artifact)));
    section.append(list);
    container.append(section);
  }
  const status = String(run.status || "").toLowerCase();
  const actions = element("div", "action-row");
  if (hasScope("praxis.runs.control")) {
    if (["running", "blocked"].includes(status)) actions.append(runControlButton("pause", "Пауза", "pause", run));
    if (status === "paused") actions.append(runControlButton("resume", "Продолжить", "play", run, true));
    if (!TERMINAL_STATUSES.has(status)) actions.append(runControlButton("cancel", "Отменить", "stop", run, false, true));
  }
  if (actions.childElementCount) container.append(actions);
  replace(ui.sheetBody, container);
  model.sheetReturnFocus = trigger || model.sheetReturnFocus;
}

function runControlButton(action, label, iconName, run, primary = false, danger = false) {
  const button = element("button", `action-button${primary ? " action-button--primary" : ""}${danger ? " action-button--danger" : ""}`);
  button.type = "button";
  button.dataset.runControl = action;
  button.dataset.runId = String(run.run_id || run.id || "");
  button.dataset.runRevision = String(run.revision || "");
  button.append(svgIcon(iconName), document.createTextNode(label));
  return button;
}

function openRunControlForm(button) {
  const action = button.dataset.runControl;
  const runId = button.dataset.runId;
  const revision = button.dataset.runRevision;
  const labels = { pause: "Поставить run на паузу", resume: "Продолжить run", cancel: "Отменить run" };
  const form = element("form");
  form.dataset.controlForm = action;
  form.dataset.runId = runId;
  form.dataset.runRevision = revision;
  const field = element("div", "field");
  const label = element("label", "", "Причина");
  label.htmlFor = "runControlReason";
  const input = element("textarea");
  input.id = "runControlReason";
  input.name = "reason";
  input.maxLength = 500;
  input.required = true;
  input.value = `Владелец через Praxis mini-app: ${action}`;
  field.append(label, input);
  const actions = element("div", "action-row");
  const submit = element("button", `action-button ${action === "cancel" ? "action-button--danger" : "action-button--primary"}`, labels[action]);
  submit.type = "submit";
  actions.append(submit);
  form.append(field, actions);
  openSheet({
    title: labels[action], eyebrow: `run ${runId}`, content: form,
    returnFocus: model.sheetReturnFocus || button,
  });
  window.setTimeout(() => input.focus(), 50);
}

async function submitRunControl(form) {
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    await api(`/runs/${encodeURIComponent(form.dataset.runId)}/control`, {
      method: "POST",
      body: {
        action: form.dataset.controlForm,
        reason: form.elements.reason.value.trim(),
        expected_revision: form.dataset.runRevision ? Number(form.dataset.runRevision) : undefined,
      },
    });
    telegram.haptic("medium");
    toast("Control receipt принят");
    closeSheet();
    await refreshSnapshot({ quiet: true });
  } catch (error) {
    toast(error.message, "error", 4200);
    submit.disabled = false;
  }
}

async function sendCommand(domain, action, payload = {}, { quiet = false, idempotencyKey = "" } = {}) {
  const body = { domain, action, ...payload };
  const key = commandNeedsIdempotency(domain, action)
    ? String(idempotencyKey || body.idempotency_key || freshIdempotencyKey())
    : "";
  if (key) body.idempotency_key = key;
  let result;
  try {
    result = await api("/command", { method: "POST", body });
  } catch (error) {
    if (key) error.idempotencyKey = key;
    throw error;
  }
  if (!quiet) toast(first(result.message, result.note, `${action}: принято`));
  // Снимок, приехавший вместе с ответом, применяем всегда — он уже в руках и даром.
  // Заказывать НОВЫЙ снимок после чтения — нет: платить за это нечем.
  const readOnly = READ_ONLY_COMMANDS.has(`${String(domain)}.${String(action)}`);
  if (!readOnly) telegram.haptic("medium");
  if (result.snapshot) {
    applySnapshot(result.snapshot);
    saveVerifiedSnapshot(result.snapshot).catch(() => {});
  } else if (!readOnly) {
    scheduleRefresh(String(result.revision || ""));
  }
  return result;
}

function draftKey(token) {
  const partition = snapshotPartition();
  return partition ? `command-draft:${partition}:${boundedText(token, 120)}` : "";
}

async function saveCommandDraft(token, payload, idempotencyKey = "") {
  const key = draftKey(token);
  if (!key) throw new Error("Черновик требует подтверждённую device/Telegram сессию");
  const fields = {};
  Object.entries(object(payload)).forEach(([key, value]) => {
    const safeKey = String(key || "").replace(/\0/g, "").slice(0, 120);
    fields[safeKey] = String(value ?? "").replace(/\0/g, "").slice(0, 10000);
  });
  await dbPut(key, {
    token,
    fields,
    idempotencyKey: String(idempotencyKey || "").slice(0, 200),
    savedAt: Date.now(),
  });
}

async function clearCommandDraft(token) {
  const key = draftKey(token);
  if (!key) return;
  try { await dbDelete(key); } catch (_) { /* Storage is optional. */ }
}

async function restoreCommandDraft(form) {
  const token = form.dataset.commandForm;
  const key = draftKey(token);
  if (!key) return;
  try {
    const draft = object(await dbGet(key));
    if (!draft.savedAt || !form.isConnected) return;
    Object.entries(object(draft.fields)).forEach(([name, value]) => {
      const control = form.elements.namedItem(name);
      if (control && "value" in control) control.value = String(value);
    });
    if (draft.idempotencyKey) form.dataset.idempotencyKey = String(draft.idempotencyKey).slice(0, 200);
    const note = element("p", "draft-note", `Локальный черновик от ${exactTime(draft.savedAt)}. Он не был отправлен и никогда не отправится автоматически.`);
    form.insertBefore(note, form.querySelector(".action-row"));
  } catch (_) {
    // Draft storage is a convenience, never a prerequisite for execution.
  }
}

function commandFromToken(token, trigger) {
  const [domain, ...actionParts] = String(token || "").split(".");
  const action = actionParts.join(".");
  if (!domain || !action) return;
  if (token === "process.start") return openProcessForm(trigger);
  if (token === "files.open") return openFilesForm(trigger);
  if (token === "system.restart") return openSystemConfirm(trigger);
  if (token === "system.logs") return sendCommand("system", "logs").then((result) => showCommandResult("Логи", result, trigger)).catch(commandError);
  trigger.disabled = true;
  const idempotencyKey = commandNeedsIdempotency(domain, action)
    ? String(trigger.dataset.idempotencyKey || freshIdempotencyKey())
    : "";
  if (idempotencyKey) trigger.dataset.idempotencyKey = idempotencyKey;
  sendCommand(domain, action, {}, { idempotencyKey })
    .then((result) => {
      delete trigger.dataset.idempotencyKey;
      if (token === "desktop.capture" && result) showCommandResult("Снимок", result, trigger);
    })
    .catch((error) => {
      if (error.idempotencyKey) trigger.dataset.idempotencyKey = error.idempotencyKey;
      commandError(error);
    })
    .finally(() => { trigger.disabled = false; });
}

function commandError(error) {
  toast(error.message || String(error), "error", 4300);
}

function openProcessForm(trigger) {
  const form = element("form");
  form.dataset.commandForm = "process.start";
  form.append(
    formField("Команда", "command", "textarea", "python -m unittest -q …", true),
    formField("Рабочая папка", "cwd", "text", "C:\\path\\to\\project", false),
    selectField("Контекст", "execution", isOwner()
      ? [["interactive", "Interactive"], ["system", "SYSTEM"]]
      : [["interactive", "Interactive"]]),
  );
  const actions = element("div", "action-row");
  const submit = element("button", "action-button action-button--primary", "Запустить с evidence");
  submit.type = "submit";
  submit.prepend(svgIcon("play"));
  actions.append(submit);
  form.append(actions);
  openSheet({ title: "Новый процесс", eyebrow: "Windows execution body", content: form, returnFocus: trigger });
  restoreCommandDraft(form);
}

function openFilesForm(trigger) {
  const executionOptions = isOwner()
    ? [["interactive", "Interactive"], ["system", "SYSTEM"]]
    : [["interactive", "Interactive"]];
  const workbench = element("div", "file-workbench");

  const listForm = element("form", "file-operation");
  listForm.dataset.commandForm = "files.list";
  listForm.append(
    element("h3", "", "Посмотреть папку"),
    formField("Путь", "path", "text", "C:\\Users\\…", true, "files-list"),
    selectField("Контекст", "execution", executionOptions, "files-list"),
    formActions("Открыть", "artifact"),
  );

  const exportForm = element("form", "file-operation");
  exportForm.dataset.commandForm = "files.export";
  exportForm.append(
    element("h3", "", "Получить с компьютера"),
    element("p", "detail-copy", "Файл пройдёт через hash-addressed bridge и станет скачиваемым artifact этого run."),
    formField("Полный путь к файлу", "path", "text", "C:\\Users\\…\\report.pdf", true, "files-export"),
    selectField("Контекст", "execution", executionOptions, "files-export"),
    formActions("Подготовить к скачиванию", "download"),
  );

  const uploadForm = element("form", "file-operation");
  uploadForm.dataset.fileUploadForm = "windows";
  const fileField = element("div", "field");
  const fileLabel = element("label", "", "Файл с этого устройства");
  fileLabel.htmlFor = "files-import-file";
  const fileInput = element("input");
  fileInput.id = "files-import-file";
  fileInput.name = "file";
  fileInput.type = "file";
  fileInput.required = true;
  fileField.append(fileLabel, fileInput);
  uploadForm.append(
    element("h3", "", "Отправить на компьютер"),
    element("p", "detail-copy", "До 64 МиБ за один перенос. Запись на Windows подтверждается тем же stable operation id."),
    fileField,
    formField("Куда сохранить", "destination", "text", "C:\\Users\\…\\Downloads\\file.ext", true, "files-import"),
    selectField("Контекст", "execution", executionOptions, "files-import"),
    formActions("Отправить с проверкой hash", "artifact"),
  );

  workbench.append(listForm, exportForm, uploadForm);
  openSheet({ title: "Файлы компьютера", eyebrow: "Verified transfer · no offline replay", content: workbench, returnFocus: trigger });
  restoreCommandDraft(listForm);
  restoreCommandDraft(exportForm);
}

function formActions(label, iconName) {
  const actions = element("div", "action-row");
  const submit = element("button", "action-button action-button--primary", label);
  submit.type = "submit";
  submit.prepend(svgIcon(iconName));
  actions.append(submit);
  return actions;
}

function openSystemConfirm(trigger) {
  const form = element("form");
  form.dataset.commandForm = "system.restart";
  form.append(element("p", "detail-copy", "Мягкий рестарт сохраняет durable runs и поднимает recovery по receipts. Это действие не обнуляет память или task store."));
  const actions = element("div", "action-row");
  const submit = element("button", "action-button action-button--danger", "Запросить рестарт");
  submit.type = "submit";
  submit.prepend(svgIcon("refresh"));
  actions.append(submit);
  form.append(actions);
  openSheet({ title: "Мягкий рестарт", eyebrow: "System action", content: form, returnFocus: trigger });
}

function formField(labelText, name, type = "text", placeholder = "", required = false, idPrefix = "field") {
  const field = element("div", "field");
  const label = element("label", "", labelText);
  label.htmlFor = `${idPrefix}-${name}`;
  const input = element(type === "textarea" ? "textarea" : "input");
  input.id = `${idPrefix}-${name}`;
  input.name = name;
  if (input instanceof HTMLInputElement) input.type = type;
  input.placeholder = placeholder;
  input.required = required;
  input.maxLength = type === "textarea" ? 10000 : 1000;
  field.append(label, input);
  return field;
}

function selectField(labelText, name, options, idPrefix = "field") {
  const field = element("div", "field");
  const label = element("label", "", labelText);
  label.htmlFor = `${idPrefix}-${name}`;
  const select = element("select");
  select.id = `${idPrefix}-${name}`;
  select.name = name;
  options.forEach(([value, title]) => {
    const option = element("option", "", title);
    option.value = value;
    select.append(option);
  });
  field.append(label, select);
  return field;
}

async function submitCommandForm(form) {
  const token = form.dataset.commandForm;
  const [domain, ...rest] = token.split(".");
  const action = rest.join(".");
  const payload = Object.fromEntries(new FormData(form).entries());
  const needsIdempotency = commandNeedsIdempotency(domain, action);
  const idempotencyKey = needsIdempotency
    ? String(form.dataset.idempotencyKey || freshIdempotencyKey())
    : "";
  if (idempotencyKey) form.dataset.idempotencyKey = idempotencyKey;
  const submit = form.querySelector("button[type='submit']");
  if (!navigator.onLine) {
    if (token !== "system.restart") {
      try {
        await saveCommandDraft(token, payload, idempotencyKey);
        toast("Сохранён только локальный черновик. Автоотправки не будет.", "info", 4700);
        if (!form.querySelector(".draft-note")) {
          const note = element("p", "draft-note", "Офлайн-черновик сохранён на этом устройстве. Отправить его можно только вручную после возвращения связи.");
          form.insertBefore(note, form.querySelector(".action-row"));
        }
      } catch (_) {
        toast("Офлайн: команда не отправлена; хранилище черновика недоступно", "error", 4700);
      }
    } else {
      toast("Офлайн: системное действие не отправлено и не поставлено в очередь", "error", 4700);
    }
    return;
  }
  submit.disabled = true;
  try {
    const result = await sendCommand(domain, action, payload, { quiet: true, idempotencyKey });
    delete form.dataset.idempotencyKey;
    await clearCommandDraft(token);
    showCommandResult(action === "start" ? "Процесс запущен" : action === "list" ? "Файлы" : "Команда принята", result, model.sheetReturnFocus);
  } catch (error) {
    if ((error.offline || error.network) && token !== "system.restart") {
      if (error.idempotencyKey) form.dataset.idempotencyKey = error.idempotencyKey;
      saveCommandDraft(token, payload, form.dataset.idempotencyKey || idempotencyKey).catch(() => {});
      toast("Доставка не подтверждена: сохранён локальный черновик без автоповтора. Перед ручной отправкой проверьте run/evidence.", "error", 5400);
    } else {
      toast(error.message, "error", 4300);
    }
    submit.disabled = false;
  }
}

async function submitFileUpload(form) {
  const input = form.elements.namedItem("file");
  const file = input?.files?.[0];
  const destination = String(form.elements.namedItem("destination")?.value || "").trim();
  const execution = String(form.elements.namedItem("execution")?.value || "interactive");
  const submit = form.querySelector("button[type='submit']");
  if (!(file instanceof File) || !destination) {
    toast("Выберите файл и полный путь назначения", "error", 4200);
    return;
  }
  if (file.size > 64 * 1024 * 1024) {
    toast("Через PWA можно отправить не более 64 МиБ за один перенос", "error", 4400);
    return;
  }
  if (!navigator.onLine) {
    toast("Офлайн: файл не сохранён в очередь и не будет отправлен автоматически", "error", 4800);
    return;
  }
  const idempotencyKey = String(form.dataset.idempotencyKey || freshIdempotencyKey());
  form.dataset.idempotencyKey = idempotencyKey;
  const body = new FormData();
  body.set("file", file, file.name);
  body.set("destination", destination);
  body.set("execution", execution);
  body.set("idempotency_key", idempotencyKey);
  submit.disabled = true;
  try {
    const result = await apiMultipart("/files/import", body);
    delete form.dataset.idempotencyKey;
    telegram.haptic("medium");
    showCommandResult("Файл отправлен", result, model.sheetReturnFocus);
    scheduleRefresh(String(result.revision || ""));
  } catch (error) {
    if (error.network) {
      error.idempotencyKey = idempotencyKey;
      toast("Доставка не подтверждена. Файл не будет повторён автоматически; повторите вручную с тем же выбранным файлом.", "error", 5600);
    } else {
      commandError(error);
    }
    submit.disabled = false;
  }
}

// ⚠ 03.08.2026. Её карты памяти открывались в общий приёмник серверных расписок:
// весь markdown-исходник уезжал в <pre class="recap-copy"> — коробку с собственным
// max-height: min(48vh, 520px) ВНУТРИ листа, который и сам скроллится. RUNS.md на
// 35 КБ превращался в полтора десятка экранов вложенной прокрутки поверх сырых
// решёток и квадратных скобок. Карта — оглавление её памяти, её читают глазами, а не
// грепают, поэтому здесь свой разбор. showCommandResult не трогаем: для логов и
// вывода shell сырой <pre> правилен, там ему и место.
//
// Экранирование здесь получается КОНСТРУКЦИЕЙ: всё, что пришло из файла, попадает в
// документ только как textContent (через element) или createTextNode. innerHTML и
// insertAdjacentHTML в этом блоке запрещены — не потому что сегодня есть дыра (её
// нет: в app.js нет ни одного innerHTML), а потому что склейка строк — единственный
// способ её завести. За этим следит test_app_memory.py.
//
// Синтаксис взят с её живых карт: H1, шапка-цитата, списки, [текст](путь), **жирный**,
// _курсив_, голые https-ссылки. Заборы кода в картах сегодня не встречаются и сделаны
// как дешёвая страховка на её будущее письмо; таблиц нет и разбора таблиц тоже нет —
// это была бы поддержка выдуманного синтаксиса.
const MD_INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[[^\]\n]*\]\([^)\n]*\))|(_[^_\n]+_)|(\*[^*\n]+\*)|(https?:\/\/[^\s<>"')\]]+)/g;
const MD_HTTP = /^https?:\/\//i;
const MD_WORD = /[\p{L}\p{N}_]/u;

function mdLink(text, target) {
  // ⚠ Цели внутри карт относительные: «../people/егор.md». Такой файл сегодня не
  // отдаёт ни один маршрут /api/praxis/v1 и ни одна команда — открыть его аппу
  // НЕЧЕМ. Поэтому это не ссылка, а имя с адресом рядом: живой <a> обещал бы
  // переход, которого не будет. href появляется только у http(s) и только с
  // rel="noopener noreferrer" — иначе нажатие внутри Telegram WebApp уводит
  // владельца из аппа, отдав чужой вкладке window.opener.
  if (MD_HTTP.test(target)) {
    const link = element("a", "md-link md-link--web", text || target);
    link.href = target;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }
  const span = element("span", "md-link");
  span.append(element("span", "md-link__text", text || target));
  if (target) span.append(element("small", "md-link__target", target));
  return span;
}

function mdInline(source, parent) {
  const text = String(source ?? "");
  let last = 0;
  MD_INLINE.lastIndex = 0;
  let match;
  while ((match = MD_INLINE.exec(text)) !== null) {
    const token = match[0];
    // Текст ДО находки отдаём всегда и первым делом: любая ветка ниже может решить,
    // что находка — не разметка, и тогда пропуск этой строки съел бы кусок её карты.
    if (match.index > last) parent.append(document.createTextNode(text.slice(last, match.index)));
    const before = match.index > 0 ? text[match.index - 1] : "";
    last = match.index + token.length;
    if ((match[4] || match[5]) && MD_WORD.test(before)) {
      // Курсив внутри слова — не курсив, а имя вроде snake_case_name. Соседний символ
      // смотрим кодом, а не lookbehind'ом: его нет в старых WebKit, а апп живёт внутри
      // Telegram на телефоне.
      parent.append(document.createTextNode(token));
    } else if (match[1]) parent.append(element("code", "md-code", token.slice(1, -1)));
    else if (match[2]) parent.append(element("strong", "", token.slice(2, -2)));
    else if (match[3]) {
      const cut = token.indexOf("](");
      parent.append(mdLink(token.slice(1, cut), token.slice(cut + 2, -1)));
    } else if (match[4] || match[5]) parent.append(element("em", "", token.slice(1, -1)));
    else parent.append(mdLink(token, token));
  }
  if (last < text.length) parent.append(document.createTextNode(text.slice(last)));
  return parent;
}

function renderMarkdown(source) {
  const fragment = document.createDocumentFragment();
  const lines = String(source ?? "").replace(/\r\n?/g, "\n").split("\n");
  let paragraph = [];
  let quote = [];
  let list = null;
  let fence = null;
  const flushParagraph = () => {
    if (!paragraph.length) return;
    fragment.append(mdInline(paragraph.join(" "), element("p", "md-p")));
    paragraph = [];
  };
  const flushQuote = () => {
    if (!quote.length) return;
    fragment.append(mdInline(quote.join(" "), element("blockquote", "md-quote")));
    quote = [];
  };
  const flushList = () => {
    if (!list) return;
    fragment.append(list.node);
    list = null;
  };
  const flushAll = () => { flushParagraph(); flushQuote(); flushList(); };
  lines.forEach((raw) => {
    const line = raw.replace(/\s+$/, "");
    if (fence !== null) {
      if (/^\s*```/.test(line)) {
        fragment.append(element("pre", "md-pre", fence.join("\n")));
        fence = null;
      } else fence.push(raw);
      return;
    }
    if (/^\s*```/.test(line)) { flushAll(); fence = []; return; }
    if (!line.trim()) { flushAll(); return; }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushAll();
      // h1/h2 внутри листа сломали бы шкалу его собственного заголовка, поэтому вся
      // лестница карты садится на h3/h4.
      fragment.append(mdInline(heading[2], element(heading[1].length <= 2 ? "h3" : "h4", "md-h")));
      return;
    }
    const quoted = /^\s*>\s?(.*)$/.exec(line);
    if (quoted) { flushParagraph(); flushList(); quote.push(quoted[1]); return; }
    flushQuote();
    const bullet = /^(\s*)[-*+]\s+(.*)$/.exec(line);
    const ordered = bullet ? null : /^(\s*)\d{1,3}[.)]\s+(.*)$/.exec(line);
    const item = bullet || ordered;
    if (item) {
      flushParagraph();
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tag !== tag) {
        flushList();
        list = { tag, node: element(tag, `md-list md-list--${tag}`) };
      }
      const node = element("li", "md-li");
      if (item[1].length >= 2) node.classList.add("md-li--nested");
      mdInline(item[2], node);
      list.node.append(node);
      return;
    }
    flushList();
    paragraph.push(line.trim());
  });
  if (fence !== null) fragment.append(element("pre", "md-pre", fence.join("\n")));
  flushAll();
  return fragment;
}

async function openMemoryMap(name, trigger) {
  const mapName = String(name || "").toUpperCase();
  const container = element("div", "md-sheet");
  container.append(loadingState("Читаю карту"));
  // Лист открываем ДО ответа: чтение карты — это чтение, а не исполненная команда, и
  // надзаголовок говорит именно это. Раньше здесь стояло «Server receipt» — чтение её
  // памяти было подписано как совершённая над сервером операция.
  openSheet({ title: mapName, eyebrow: "Карта памяти", content: container, returnFocus: trigger });
  let result;
  try {
    result = await sendCommand("memory", "map.read", { map: mapName }, { quiet: true });
  } catch (error) {
    replace(container, errorState("Карта не открылась", boundedText(error.message, 300)));
    return;
  }
  const known = asList(memoryOf(model.snapshot || {}).maps)
    .find((entry) => String(first(entry.name, entry.id, "")).toUpperCase() === mapName) || {};
  const facts = [
    result.size !== undefined ? bytes(result.size) : "",
    known.updated_at ? `обновлена ${relativeTime(known.updated_at)}` : "",
    result.path ? String(result.path) : "",
  ].filter(Boolean).join(" · ");
  const parts = [];
  if (facts) parts.push(element("p", "md-facts", facts));
  // ⚠ Обрезка обязана говорить о себе вслух. Прежний приёмник читал только
  // result.content и молчал про result.truncated, а сверху клал ещё и свой потолок в
  // 128000 символов — НИЖЕ серверного, с многоточием, неотличимым от текста самой
  // карты. Владелец мог прочесть урезанную карту и считать её целой. Своего потолка
  // здесь нет намеренно: границу держит сервер, он же о ней и сообщает.
  if (result.truncated) {
    parts.push(element("p", "md-notice",
      `Показано начало карты: она длиннее потолка чтения, целиком — ${bytes(result.size)}.`));
  }
  const doc = element("article", "md-doc");
  doc.append(renderMarkdown(typeof result.content === "string" ? result.content : ""));
  parts.push(doc);
  replace(container, ...parts);
}

function showCommandResult(title, result, trigger) {
  const container = element("div");
  const summary = first(result.message, result.note, result.summary, result.error);
  if (summary) container.append(element("p", "detail-copy", boundedText(summary, 5000)));
  const content = typeof result.content === "string" ? result.content : "";
  if (content) {
    const section = element("section", "detail-section");
    section.append(element("pre", "recap-copy", boundedText(content, 128000)));
    container.append(section);
  }
  const directArtifact = object(first(result.download, result.browser_artifact, {}));
  if (directArtifact.download_url) {
    container.append(
      directArtifact.presentation === "image"
        ? artifactPreview(directArtifact)
        : artifactCard(directArtifact),
    );
  }
  const entries = asList(first(result.entries, result.items, result.processes, result.artifacts, []));
  if (entries.length) {
    const list = element("div", "stack-list");
    entries.slice(0, 300).forEach((entry) => {
      const card = element("div", "stack-card");
      const body = element("span", "stack-card__body");
      body.append(
        element("strong", "", first(entry.name, entry.path, entry.title, entry.id, "Элемент")),
        element("small", "", first(entry.detail, entry.kind, entry.status, entry.size !== undefined ? bytes(entry.size) : "")),
      );
      card.append(body);
      if (entry.status) card.append(statusPill(entry.status));
      list.append(card);
    });
    container.append(list);
  } else if (!summary && !content && !directArtifact.download_url) {
    const pre = element("p", "detail-copy", boundedText(JSON.stringify(result, null, 2), 12000));
    container.append(pre);
  }
  openSheet({ title, eyebrow: "Server receipt", content: container, returnFocus: trigger });
}

function openJoinForm(trigger) {
  const form = element("form");
  form.dataset.commandForm = "telegram.join";
  form.append(formField("Ссылка, @username или chat id", "target", "text", "https://t.me/+…", true));
  const note = element("p", "detail-copy", "Owner intent сначала попадёт в durable membership ledger; живой Telethon runner применит его тем же аккаунтом.");
  const actions = element("div", "action-row");
  const submit = element("button", "action-button action-button--primary", "Создать join intent");
  submit.type = "submit";
  submit.prepend(svgIcon("telegram"));
  actions.append(submit);
  form.append(note, actions);
  openSheet({ title: "Войти в Telegram", eyebrow: "Durable membership", content: form, returnFocus: trigger });
  restoreCommandDraft(form);
}

function openDeviceEnrollmentIssuer(trigger) {
  const form = element("form");
  form.dataset.deviceEnrollmentIssuer = "device";
  form.append(
    formField("Название устройства", "label", "text", "Рабочий компьютер", true),
    selectField("Срок одноразовой ссылки", "ttl_seconds", [
      ["600", "10 минут"], ["1800", "30 минут"], ["3600", "1 час"], ["86400", "24 часа"],
    ]),
  );
  const field = element("fieldset", "field");
  const legend = element("legend", "", "Точные возможности этой установки");
  const choices = element("div", "choice-grid");
  DEVICE_SCOPE_CHOICES.forEach(([scope, title]) => {
    const label = element("label", "choice");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.name = "scopes";
    checkbox.value = scope;
    checkbox.checked = true;
    label.append(checkbox, document.createTextNode(title));
    label.title = scope;
    choices.append(label);
  });
  field.append(legend, choices);
  const note = element(
    "p", "detail-copy",
    "Ссылка сработает ровно один раз. Устройство получит только отмеченные scopes, не станет владельцем, не сможет выдавать доступ другим и не получит Windows SYSTEM.",
  );
  const actions = element("div", "action-row");
  const submit = element("button", "action-button action-button--primary", "Создать одноразовую ссылку");
  submit.type = "submit";
  submit.prepend(svgIcon("plus"));
  actions.append(submit);
  form.append(field, note, actions);
  openSheet({
    title: "Привязать устройство",
    eyebrow: "Owner-issued · exact scopes",
    content: form,
    returnFocus: trigger,
  });
}

async function copyEnrollmentLink(url, control) {
  try {
    await navigator.clipboard.writeText(url);
  } catch (_) {
    control.focus();
    control.select();
    if (!document.execCommand("copy")) throw new Error("Буфер обмена недоступен");
  }
  telegram.haptic("light");
  toast("Одноразовая ссылка скопирована");
}

function showEnrollmentLink(enrollment, trigger) {
  const url = String(enrollment.enrollment_url || "");
  let parsed;
  try { parsed = new URL(url); } catch (_) { parsed = null; }
  const loopback = parsed && ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname);
  if (!parsed || (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && loopback))) {
    throw new Error("Сервер не вернул enrollment URL");
  }
  const content = element("div");
  content.append(
    element("p", "detail-copy", `Ссылка для «${boundedText(enrollment.label || "устройство", 100)}» действует до ${exactTime(enrollment.expires_at)} и исчезнет после первого успешного обмена.`),
  );
  const field = element("div", "field");
  const label = element("label", "", "Одноразовая ссылка");
  const value = element("textarea");
  value.id = "device-enrollment-url";
  label.htmlFor = value.id;
  value.readOnly = true;
  value.value = url;
  value.rows = 4;
  value.setAttribute("spellcheck", "false");
  field.append(label, value);
  const scopes = asList(enrollment.scopes);
  const scopeText = element("p", "draft-note", scopes.length ? scopes.join(" · ") : "Только базовый read scope");
  const actions = element("div", "action-row");
  const copy = element("button", "action-button action-button--primary", "Копировать");
  copy.type = "button";
  copy.prepend(svgIcon("download"));
  copy.addEventListener("click", () => copyEnrollmentLink(url, value).catch(commandError));
  actions.append(copy);
  if (typeof navigator.share === "function") {
    const share = element("button", "action-button", "Поделиться");
    share.type = "button";
    share.addEventListener("click", () => navigator.share({ title: "Praxis", url }).catch(() => {}));
    actions.append(share);
  }
  content.append(field, scopeText, actions);
  openSheet({
    title: "Ссылка готова",
    eyebrow: "Показывается только сейчас",
    content,
    returnFocus: trigger,
  });
}

async function submitDeviceEnrollmentIssuer(form) {
  const values = new FormData(form);
  const scopes = values.getAll("scopes").map(String);
  const submit = form.querySelector("button[type='submit']");
  if (!scopes.includes("praxis.snapshot")) {
    toast("Для PWA нужен scope praxis.snapshot", "error");
    return;
  }
  submit.disabled = true;
  try {
    const payload = object(await api("/device/enrollment", {
      method: "POST",
      body: {
        label: String(values.get("label") || "").trim(),
        scopes,
        ttl_seconds: Number(values.get("ttl_seconds") || 900),
      },
    }));
    showEnrollmentLink(object(payload.enrollment), model.sheetReturnFocus);
  } catch (error) {
    toast(error.message, "error", 4800);
    submit.disabled = false;
  }
}

function openEnrollmentForm(token) {
  model.enrollmentToken = String(token || "");
  ui.enroll.hidden = !model.enrollmentToken;
  const form = element("form");
  form.dataset.enrollmentForm = "device";
  const platform = boundedText(navigator.userAgentData?.platform || navigator.platform || "Windows", 80);
  form.append(
    element("p", "detail-copy", "Одноразовая ссылка привяжет эту установку к Praxis. Имя и scopes уже подписаны владельцем при создании ссылки. Долгоживущий token останется только в IndexedDB этого профиля и не попадёт в URL или localStorage."),
    element("p", "draft-note", `Платформа: ${platform}`),
  );
  const actions = element("div", "action-row");
  const submit = element("button", "action-button action-button--primary", "Привязать устройство");
  submit.type = "submit";
  submit.prepend(svgIcon("trust"));
  actions.append(submit);
  form.append(actions);
  openSheet({ title: "Новая установка Praxis", eyebrow: "One-time enrollment", content: form });
}

// The installed PWA opens without the enrollment fragment, so let the owner
// paste the one-time link (or token) and bind the device in this — the correct —
// storage partition. Same one-time exchange; the secret still only lives in the
// pasted text and the resulting bearer only in this profile's IndexedDB.
function renderEnrollmentReopen() {
  // F6: постоянная кнопка «снова открыть лист вставки ссылки». Раньше закрытый лист
  // (крестик/скрим/Esc) переоткрывался только перезагрузкой приложения — тупик.
  const summary = document.querySelector("#heroSummary");
  if (!summary || document.querySelector("#reopenEnrollButton")) return;
  const btn = element("button", "action-button action-button--primary", "Вставить ссылку привязки");
  btn.id = "reopenEnrollButton";
  btn.type = "button";
  btn.style.marginTop = "14px";
  btn.addEventListener("click", () => openEnrollmentPaste());
  summary.insertAdjacentElement("afterend", btn);
}

function openEnrollmentPaste() {
  const form = element("form");
  form.dataset.enrollmentForm = "paste";
  const platform = boundedText(navigator.userAgentData?.platform || navigator.platform || "устройство", 80);
  const field = element("div", "field");
  const input = element("input");
  input.id = "device-enrollment-paste";
  input.name = "enrollment";
  input.type = "text";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("autocapitalize", "off");
  input.placeholder = "https://…/#enroll=…  или  praxis_enroll_…";
  const label = element("label", "", "Одноразовая ссылка от владельца");
  label.htmlFor = input.id;
  field.append(label, input);
  form.append(
    element("p", "detail-copy", "Вставь одноразовую ссылку из сообщения владельца. Внутри установленного приложения устройство привяжется в правильном разделе — token останется только в IndexedDB этого профиля и не попадёт в URL или localStorage."),
    element("p", "draft-note", `Платформа: ${platform}`),
    field,
  );
  const actions = element("div", "action-row");
  const submit = element("button", "action-button action-button--primary", "Привязать устройство");
  submit.type = "submit";
  submit.prepend(svgIcon("trust"));
  actions.append(submit);
  form.append(actions);
  openSheet({ title: "Установка Praxis", eyebrow: "Вставь ссылку привязки", content: form });
}

// Opened from the browser (not the installed app): binding here would spend the
// one-time link in the wrong storage partition, and the Home-Screen app would
// still be unauthorised. Preserve the token, show how to finish inside the PWA,
// and offer the link to copy across.
function openInstallGuidance(token) {
  const link = `${location.origin}${location.pathname}#enroll=${encodeURIComponent(String(token || ""))}`;
  const content = element("div");
  const steps = element("ol", "detail-copy");
  steps.append(
    element("li", "", "Добавь Praxis на экран «Домой» (Поделиться → «На экран „Домой“»)."),
    element("li", "", "Открой Praxis с домашнего экрана — как установленное приложение."),
    element("li", "", "Там нажми «Вставить ссылку» и вставь эту ссылку, чтобы завершить привязку."),
  );
  content.append(
    element("p", "detail-copy", "Ты открыл ссылку в браузере. Привязка здесь потратит одноразовую ссылку не в том разделе, и установленное приложение останется без доступа."),
    steps,
  );
  const field = element("div", "field");
  const fieldLabel = element("label", "", "Ссылка для установленного приложения");
  const value = element("textarea");
  value.id = "device-enrollment-carry";
  fieldLabel.htmlFor = value.id;
  value.readOnly = true;
  value.value = link;
  value.rows = 4;
  value.setAttribute("spellcheck", "false");
  field.append(fieldLabel, value);
  const actions = element("div", "action-row");
  const copy = element("button", "action-button action-button--primary", "Копировать ссылку");
  copy.type = "button";
  copy.prepend(svgIcon("download"));
  copy.addEventListener("click", () => copyEnrollmentLink(link, value).catch(commandError));
  actions.append(copy);
  const anyway = element("button", "action-button", "Всё равно привязать в браузере");
  anyway.type = "button";
  anyway.addEventListener("click", () => openEnrollmentForm(token));
  actions.append(anyway);
  content.append(field, actions);
  openSheet({ title: "Заверши установку в приложении", eyebrow: "Открыто в браузере", content });
}

async function submitEnrollment(form) {
  const submit = form.querySelector("button[type='submit']");
  const platform = boundedText(navigator.userAgentData?.platform || navigator.platform || "unknown", 120);
  if (form.dataset.enrollmentForm === "paste") {
    const token = extractEnrollmentToken(new FormData(form).get("enrollment"));
    if (!token) {
      toast("Ссылку не разобрал — вставь одноразовую ссылку целиком", "error", 4800);
      return;
    }
    model.enrollmentToken = token;
  }
  if (!model.enrollmentToken) {
    toast("Одноразовый enrollment token отсутствует", "error");
    return;
  }
  if (!window.isSecureContext) {
    toast("Привязка устройства разрешена только через HTTPS", "error", 4800);
    return;
  }
  submit.disabled = true;
  try {
    const payload = object(await api("/device/enroll", {
      method: "POST",
      auth: false,
      body: {
        enrollment_token: model.enrollmentToken,
        platform,
      },
    }));
    const rawCredential = object(payload.credential);
    const rawDevice = object(first(payload.device, payload.principal, rawCredential.principal, {}));
    const credential = { ...rawDevice, ...rawCredential, ...payload };
    credential.token = first(payload.device_token, payload.bearer_token, payload.token, rawCredential.bearer_token, credential.device_token, credential.token);
    credential.label = first(payload.label, rawDevice.label, `Praxis · ${platform}`);
    await storeDeviceAuth(credential);
    model.enrollmentToken = "";
    ui.enroll.hidden = true;
    model.cachedSnapshotLoaded = false;
    telegram.haptic("medium");
    toast("Устройство привязано. Token сохранён только в IndexedDB.", "info", 4400);
    closeSheet();
    await refreshSnapshot();
    connectEvents().catch(() => {});
  } catch (error) {
    toast(error.message, "error", 4800);
    submit.disabled = false;
  }
}

function openDeviceRevokeForm(deviceId, trigger) {
  const form = element("form");
  form.dataset.deviceRevokeForm = deviceId;
  form.append(element("p", "detail-copy", `Доступ устройства ${boundedText(deviceId, 180)} будет отозван сервером. Никакого локального повтора в офлайне не существует.`));
  const actions = element("div", "action-row");
  const submit = element("button", "action-button action-button--danger", "Отозвать устройство");
  submit.type = "submit";
  submit.prepend(svgIcon("stop"));
  actions.append(submit);
  form.append(actions);
  openSheet({ title: "Отозвать устройство", eyebrow: "Owner-only authority", content: form, returnFocus: trigger });
}

async function submitDeviceRevoke(form) {
  const deviceId = form.dataset.deviceRevokeForm;
  const submit = form.querySelector("button[type='submit']");
  const idempotencyKey = String(form.dataset.idempotencyKey || freshIdempotencyKey());
  form.dataset.idempotencyKey = idempotencyKey;
  submit.disabled = true;
  try {
    await sendCommand("device", "revoke", { device_id: deviceId }, { quiet: true, idempotencyKey });
    delete form.dataset.idempotencyKey;
    const current = model.deviceAuth?.deviceId === deviceId;
    if (current) {
      await dbDelete("device-auth").catch(() => {});
      model.deviceAuth = null;
      closeEvents();
    }
    telegram.haptic("medium");
    toast("Доступ устройства отозван");
    closeSheet();
    if (!current || telegram.initData) await refreshSnapshot({ quiet: true });
    else if (model.snapshot) setSnapshotFreshness(true, model.verifiedAt || Date.now());
  } catch (error) {
    if (error.idempotencyKey) form.dataset.idempotencyKey = error.idempotencyKey;
    toast(error.message, "error", 4800);
    submit.disabled = false;
  }
}

function openGrantForm(trigger) {
  const form = element("form");
  form.dataset.accessForm = "grant";
  form.append(
    formField("Telegram user id", "telegram_id", "text", "123456789", true),
    formField("Имя", "name", "text", "Как показывать в карте доверия", true),
  );
  const field = element("fieldset", "field");
  const legend = element("legend", "", "Точные scopes");
  const choices = element("div", "choice-grid");
  ["computer.read", "computer.files", "computer.process", "computer.apps"].forEach((scope) => {
    const label = element("label", "choice");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.name = "scopes";
    checkbox.value = scope;
    label.append(checkbox, document.createTextNode(scope));
    choices.append(label);
  });
  field.append(legend, choices);
  const actions = element("div", "action-row");
  const submit = element("button", "action-button action-button--primary", "Выдать доступ");
  submit.type = "submit";
  submit.prepend(svgIcon("trust"));
  actions.append(submit);
  form.append(field, actions);
  openSheet({ title: "Новый доступ", eyebrow: "Только владелец · без делегирования", content: form, returnFocus: trigger });
}

async function submitAccessForm(form) {
  const values = new FormData(form);
  const scopes = values.getAll("scopes").map(String);
  const submit = form.querySelector("button[type='submit']");
  if (!scopes.length) {
    toast("Выберите хотя бы один scope", "error");
    return;
  }
  submit.disabled = true;
  try {
    await api("/access", {
      method: "POST",
      body: { action: "grant", telegram_id: values.get("telegram_id"), name: values.get("name"), scopes },
    });
    telegram.haptic("medium");
    toast("Grant fsync-нут владельцем");
    closeSheet();
    await refreshSnapshot({ quiet: true });
  } catch (error) {
    toast(error.message, "error", 4300);
    submit.disabled = false;
  }
}

async function revokeAccess(telegramId, trigger) {
  trigger.disabled = true;
  try {
    await api("/access", { method: "POST", body: { action: "revoke", telegram_id: telegramId, scopes: [] } });
    telegram.haptic("medium");
    toast("Доступ отозван");
    await refreshSnapshot({ quiet: true });
  } catch (error) {
    toast(error.message, "error", 4300);
  } finally {
    trigger.disabled = false;
  }
}

async function memorySearch(query) {
  const target = document.querySelector("#memoryResults");
  target.hidden = false;
  replace(target, loadingState("Ищу по provenance"));
  try {
    const result = await sendCommand("memory", "search", { query }, { quiet: true });
    const items = asList(first(result.results, result.items, []));
    replace(target, ...(items.length ? items.map((item) => {
      const card = element("div", "stack-card");
      const iconBox = element("span", "stack-card__icon");
      iconBox.append(svgIcon("memory"));
      const body = element("span", "stack-card__body");
      body.append(element("strong", "", first(item.title, item.path, item.source, "Результат")), element("small", "", first(item.snippet, item.text, item.provenance, "")));
      card.append(iconBox, body);
      if (item.score !== undefined) card.append(element("span", "quiet-label", Number(item.score).toFixed(2)));
      return card;
    }) : [emptyState("Ничего не найдено", "Запрос не совпал с каноническими источниками.", "search")]));
  } catch (error) {
    replace(target, errorState("Поиск не выполнен", error.message));
  }
}

function setupEvents() {
  document.addEventListener("click", (event) => {
    const nav = event.target.closest("[data-nav], [data-open]");
    if (nav) {
      navigate(nav.dataset.nav || nav.dataset.open);
      return;
    }
    const refresh = event.target.closest("[data-action='refresh']");
    if (refresh) {
      if (hasAuthenticatedSession()) refreshSnapshot().catch(() => {});
      return;
    }
    const deliveryTransition = event.target.closest("[data-delivery-transition]");
    if (deliveryTransition) {
      transitionDelivery(deliveryTransition);
      return;
    }
    const deliveryAction = event.target.closest("[data-delivery-action]");
    if (deliveryAction) {
      openDeliveryAction(deliveryAction.dataset.deliveryAction, deliveryAction);
      return;
    }
    const run = event.target.closest("[data-run-id]");
    if (run && !run.dataset.runControl) {
      openRun(run.dataset.runId, run);
      return;
    }
    const control = event.target.closest("[data-run-control]");
    if (control) {
      openRunControlForm(control);
      return;
    }
    const command = event.target.closest("[data-command]");
    if (command) {
      commandFromToken(command.dataset.command, command);
      return;
    }
    const stop = event.target.closest("[data-stop-process]");
    if (stop) {
      stop.disabled = true;
      const idempotencyKey = String(stop.dataset.idempotencyKey || freshIdempotencyKey());
      stop.dataset.idempotencyKey = idempotencyKey;
      sendCommand(
        "process", "cancel", { operation_id: stop.dataset.stopProcess }, { idempotencyKey },
      ).then(() => {
        delete stop.dataset.idempotencyKey;
      }).catch((error) => {
        if (error.idempotencyKey) stop.dataset.idempotencyKey = error.idempotencyKey;
        commandError(error);
      }).finally(() => { stop.disabled = false; });
      return;
    }
    const map = event.target.closest("[data-memory-map]");
    if (map) {
      openMemoryMap(map.dataset.memoryMap, map).catch(commandError);
      return;
    }
    const cancel = event.target.closest("[data-cancel-followup]");
    if (cancel) {
      cancel.disabled = true;
      const idempotencyKey = String(cancel.dataset.idempotencyKey || freshIdempotencyKey());
      cancel.dataset.idempotencyKey = idempotencyKey;
      sendCommand("telegram", "followup.cancel", { followup_id: cancel.dataset.cancelFollowup }, { idempotencyKey })
        .then(() => { delete cancel.dataset.idempotencyKey; })
        .catch((error) => {
          if (error.idempotencyKey) cancel.dataset.idempotencyKey = error.idempotencyKey;
          commandError(error);
        }).finally(() => { cancel.disabled = false; });
      return;
    }
    const leave = event.target.closest("[data-leave-room]");
    if (leave) {
      leave.disabled = true;
      const idempotencyKey = String(leave.dataset.idempotencyKey || freshIdempotencyKey());
      leave.dataset.idempotencyKey = idempotencyKey;
      sendCommand("telegram", "leave", { target: leave.dataset.leaveRoom }, { idempotencyKey })
        .then(() => { delete leave.dataset.idempotencyKey; })
        .catch((error) => {
          if (error.idempotencyKey) leave.dataset.idempotencyKey = error.idempotencyKey;
          commandError(error);
        }).finally(() => { leave.disabled = false; });
      return;
    }
    const revoke = event.target.closest("[data-revoke]");
    if (revoke) {
      revokeAccess(revoke.dataset.revoke, revoke);
      return;
    }
    const revokeDevice = event.target.closest("[data-revoke-device]");
    if (revokeDevice) {
      openDeviceRevokeForm(revokeDevice.dataset.revokeDevice, revokeDevice);
    }
  });

  document.querySelector("#runFilters").addEventListener("click", (event) => {
    const button = event.target.closest("[data-run-filter]");
    if (!button) return;
    model.runFilter = button.dataset.runFilter;
    document.querySelectorAll("[data-run-filter]").forEach((item) => item.classList.toggle("is-active", item === button));
    renderRuns(model.snapshot || {});
    telegram.haptic("light");
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    event.preventDefault();
    if (form.dataset.deviceEnrollmentIssuer) submitDeviceEnrollmentIssuer(form);
    else if (form.dataset.enrollmentForm) submitEnrollment(form);
    else if (form.dataset.deviceRevokeForm) submitDeviceRevoke(form);
    else if (form.dataset.fileUploadForm) submitFileUpload(form);
    else if (form.dataset.controlForm) submitRunControl(form);
    else if (form.dataset.commandForm) submitCommandForm(form);
    else if (form.dataset.accessForm) submitAccessForm(form);
  });

  document.querySelector("#memorySearch").addEventListener("submit", (event) => {
    event.preventDefault();
    const query = document.querySelector("#memoryQuery").value.trim();
    if (query) memorySearch(query);
  });

  ui.refresh.addEventListener("click", () => {
    if (!hasAuthenticatedSession()) return;
    ui.refresh.animate?.([{ transform: "rotate(0)" }, { transform: "rotate(360deg)" }], { duration: 520, easing: "cubic-bezier(.2,.8,.2,1)" });
    refreshSnapshot().catch(() => {});
  });
  ui.back.addEventListener("click", back);
  ui.sheetClose.addEventListener("click", closeSheet);
  ui.scrim.addEventListener("click", closeSheet);
  document.querySelector("#joinRoomButton").addEventListener("click", (event) => openJoinForm(event.currentTarget));
  document.querySelector("#grantButton").addEventListener("click", (event) => openGrantForm(event.currentTarget));
  ui.newDevice.addEventListener("click", (event) => openDeviceEnrollmentIssuer(event.currentTarget));
  ui.enroll.addEventListener("click", () => {
    // #enrollButton lives in the (auth-only) Devices section, so it is only ever
    // visible with a token present; the standalone-no-token paste sheet is opened
    // from boot() and re-opened by a reload, not from this hidden control.
    if (model.enrollmentToken) openEnrollmentForm(model.enrollmentToken);
  });
  ui.install.addEventListener("click", async () => {
    const prompt = model.installPrompt;
    if (!prompt) return;
    prompt.prompt();
    const choice = await prompt.userChoice.catch(() => null);
    model.installPrompt = null;
    ui.install.hidden = true;
    if (choice?.outcome === "accepted") toast("Praxis устанавливается как desktop app");
  });
  telegram.onBack(back);

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") back();
    if (event.key === "Tab" && model.sheetOpen) trapSheetFocus(event);
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      closeEvents();
    } else if (hasAuthenticatedSession()) {
      refreshSnapshot({ quiet: true }).catch(() => {});
      connectEvents().catch(() => {});
    }
  });

  window.addEventListener("offline", () => {
    closeEvents();
    setSync("stale", "офлайн");
    if (model.snapshot) setSnapshotFreshness(true, model.verifiedAt || Date.now());
    ambient.setState({ phase: "offline", bodyOnline: false });
  });
  window.addEventListener("online", () => {
    setSync("", "возвращаю связь");
    if (hasAuthenticatedSession()) {
      refreshSnapshot({ quiet: true }).catch(() => {});
      connectEvents().catch(() => {});
    }
  });
}

function trapSheetFocus(event) {
  const focusable = [...ui.sheet.querySelectorAll("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href]")];
  if (!focusable.length) return;
  const firstNode = focusable[0];
  const lastNode = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === firstNode) {
    event.preventDefault();
    lastNode.focus();
  } else if (!event.shiftKey && document.activeElement === lastNode) {
    event.preventDefault();
    firstNode.focus();
  }
}

function setupTilt() {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || matchMedia("(pointer: coarse)").matches) return;
  document.addEventListener("pointermove", (event) => {
    const card = event.target.closest("[data-tilt]");
    if (!card) return;
    const rect = card.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width - .5;
    const y = (event.clientY - rect.top) / rect.height - .5;
    card.classList.add("is-tilting");
    card.dataset.tiltX = x < -.16 ? "left" : x > .16 ? "right" : "center";
    card.dataset.tiltY = y < -.16 ? "top" : y > .16 ? "bottom" : "center";
  });
  document.addEventListener("pointerout", (event) => {
    const card = event.target.closest("[data-tilt]");
    if (!card || card.contains(event.relatedTarget)) return;
    card.classList.remove("is-tilting");
    delete card.dataset.tiltX;
    delete card.dataset.tiltY;
  });
}

function setupPWA() {
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    model.installPrompt = event;
    ui.install.hidden = installedStandalone();
    if (model.snapshot) renderMore(model.snapshot);
  });
  window.addEventListener("appinstalled", () => {
    model.installPrompt = null;
    ui.install.hidden = true;
    toast("Praxis установлена как desktop app");
    if (model.snapshot) renderMore(model.snapshot);
  });
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/app/static/sw.js", { scope: "/app" }).catch(() => {
      // The live surface remains usable if installation is unavailable.
    });
  }
}

async function waitForTelegramAuth(tries = 6, delayMs = 120) {
  // Дать SDK дозаполнить initData (гонка загрузки/populate), прежде чем уходить в «нужна
  // авторизация». Не Telegram-запуск (ни WebApp, ни фрагмента) — не ждём зря.
  for (let i = 0; i < tries; i++) {
    if (hasAuthenticatedSession()) return true;
    if (!telegram.webApp && !initDataFromLocation()) return false;
    telegram.ready();
    await new Promise((r) => setTimeout(r, delayMs));
  }
  return hasAuthenticatedSession();
}


async function boot() {
  const enrollmentToken = takeEnrollmentToken();
  applyTheme();
  telegram.ready();
  telegram.webApp?.onEvent?.("themeChanged", applyTheme);
  setupPWA();
  setupEvents();
  setupTilt();
  ambient.start();
  updateBack();
  await loadDeviceAuth();
  if (enrollmentToken) {
    // Bind only inside the installed app; in the browser the one-time link would
    // land in the wrong storage partition and never reach the Home-Screen app.
    if (installedStandalone()) openEnrollmentForm(enrollmentToken);
    else openInstallGuidance(enrollmentToken);
    ui.boot.classList.add("is-done");
    setSync("", "ожидаю привязку");
  } else if (await waitForTelegramAuth()) {
    try {
      await refreshSnapshot();
    } catch (_) {
      // Offline state is already rendered; polling and ticket reconnect continue.
    }
    connectEvents().catch(() => {});
  } else if (installedStandalone()) {
    ui.boot.classList.add("is-done");
    setSync("", "нужна привязка");
    scrubProtectedSurface("Вставь одноразовую ссылку владельца, чтобы привязать это устройство.");
    openEnrollmentPaste();
    renderEnrollmentReopen();  // F6: кнопка переоткрыть лист, если его закрыли
  } else {
    ui.boot.classList.add("is-done");
    setSync("", "нужна авторизация");
    scrubProtectedSurface("Добавь Praxis на экран «Домой», открой из установленного приложения и вставь одноразовую ссылку владельца.");
  }
  model.pollingTimer = window.setInterval(() => {
    if (!document.hidden && navigator.onLine && hasAuthenticatedSession()) {
      refreshSnapshot({ quiet: true }).catch(() => {});
    }
  }, 45000);
}

boot();
