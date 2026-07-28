"""
Praxis mailroom — толстый клиент: бот @praxis_mail_bot + mini-app (Telegram Web App).

Отдельный процесс/сервис. Поллит IMAP (mailer.fetch), ведёт mailbox.json (mailroom), на НОВОЕ
письмо пушит Егору и отдаёт Web App для удобного просмотра: список входящих → открыть письмо →
«Праксис, ответь» (compose, её голос) → увидеть черновик → Approve/Reject. Отправка человеко-
подтверждена (mailroom.send_approved по approve). Токены LLM тратятся ТОЛЬКО на compose, не на
поллинг/уведомления.

Доступ к API mini-app — только владельцу: каждый запрос несёт Telegram WebApp initData, мы
проверяем HMAC бот-токеном и сверяем user.id == PRAXIS_OWNER_ID. Публичный HTTPS даёт Caddy
(reverse_proxy на 127.0.0.1:PORT), домен — *.nip.io (стабильный, без DNS-возни).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           KeyboardButtonRequestUsers, MenuButtonWebApp, Message,
                           ReplyKeyboardMarkup, WebAppInfo)
from dotenv import load_dotenv

import agent
import hostops
import immune
import mailer
import selfdev
import mailroom
import memory_viz
import panel
import praxis_app
import praxis_device_auth
import run_manager
import rooms as _rooms  # 10.4: карточки комнат (замри/дрейф/вход в группу)

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
try:  # герметичность: любой тест-запуск (unittest/pytest/PRAXIS_TEST) не читает боевой .env
    from _sandbox import _looks_like_test_run as _under_tests
except Exception:
    _under_tests = lambda: bool(os.environ.get("PRAXIS_TEST"))
if not _under_tests():
    load_dotenv(BASE / ".env", override=True)
load_dotenv(BASE / ".deploy.env", override=False)  # PRAXIS_MAIL_BOT_TOKEN живёт здесь

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("praxis-mailbot")
import logsink  # noqa: E402  (файловый хвост для панели)
logsink.attach("mailbot")

TOKEN = os.getenv("PRAXIS_MAIL_BOT_TOKEN", "")
OWNER_ID = int(os.getenv("PRAXIS_OWNER_ID", "0") or 0)
WEBAPP_URL = os.getenv("PRAXIS_MAILAPP_URL", "https://mail.203.0.113.10.nip.io/")
# ?v= — кэш-бастер: смена значения заставляет Telegram-клиент перетянуть свежий HTML
PANEL_URL = os.getenv("PRAXIS_PANEL_URL", WEBAPP_URL.rstrip("/") + "/panel?v=11")
# Кнопка «Praxis» в боте и база enroll-ссылок ведут на чистый изолированный origin
# praxis…nip.io/px: у него нет service worker и он никогда не кэшируется (см. serve_px_*),
# поэтому «старое приложение» там воскреснуть не может. Внутри Telegram вход идёт по
# initData автоматически; по enroll-ссылке в Safari устройство привязывается в один тап.
PRAXIS_APP_URL = os.getenv("PRAXIS_APP_URL", "https://praxis.203.0.113.10.nip.io/px")
PORT = int(os.getenv("PRAXIS_MAILAPP_PORT", "8092") or 8092)
POLL_SEC = float(os.getenv("PRAXIS_MAIL_POLL_SEC", "120") or 120)
FETCH_LIMIT = int(os.getenv("PRAXIS_MAIL_FETCH", "10") or 10)
AUTH_MAX_AGE = int(os.getenv("PRAXIS_MAILAPP_AUTH_MAX_AGE", "604800") or 604800)  # сек, anti-replay (7д)
PWA_FILE_MAX_BYTES = max(
    1024 * 1024,
    min(int(os.getenv("PRAXIS_PWA_FILE_MAX_BYTES", str(64 * 1024 * 1024))), 256 * 1024 * 1024),
)

dp = Dispatcher()


# --------------------------------------------------------------------------- #
#  Авторизация Web App: проверка Telegram initData (HMAC бот-токеном)
# --------------------------------------------------------------------------- #

def check_init_data(init_data: str, token: str, max_age: int = 0) -> dict | None:
    """Проверить Telegram WebApp initData. -> dict пользователя, если подпись верна; иначе None.

    Спека Telegram: secret = HMAC_SHA256("WebAppData", token); сверяем hash от data_check_string
    (отсортированные k=v через \\n, без самого hash). max_age>0 — отвергаем устаревшее (anti-replay)."""
    if not init_data or not token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    recv = pairs.pop("hash", None)
    if not recv:
        return None
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv):
        return None
    if max_age > 0:
        try:
            if time.time() - int(pairs.get("auth_date", "0")) > max_age:
                return None
        except Exception:
            return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None


def _app_bot_tokens() -> list[str]:
    """Токены ботов, чьей Telegram-initData доверяем мини-аппу: mail-бот ВСЕГДА первый, плюс
    любые из PRAXIS_APP_BOT_TOKENS (через запятую/пробел). Так @praxis_home_bot открывает тот
    же /app без нового HTTP-контура — сервер принимает его подпись. Читаем env на каждый вызов,
    поэтому добавленный токен подхватывается без правки кода. owner-проверка не ослабляется:
    Telegram user id глобален, и user.id == OWNER_ID сверяется у вызывающего для ЛЮБОГО бота."""
    raw = (os.getenv("PRAXIS_APP_BOT_TOKENS", "") or "").replace(",", " ").split()
    seen: set[str] = set()
    out: list[str] = []
    for tok in [TOKEN, *raw]:
        tok = (tok or "").strip()
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def check_init_data_multi(init_data: str, tokens: list[str], max_age: int = 0) -> dict | None:
    """Первая валидная подпись из списка токенов (HMAC + anti-replay каждого — как в одиночном
    check_init_data). None, если ни один токен не подтвердил подпись."""
    for token in tokens:
        user = check_init_data(init_data, token, max_age)
        if user is not None:
            return user
    return None


def _owner(request: web.Request) -> bool:
    """Запрос от владельца? initData из заголовка/квери, HMAC верен, user.id == OWNER_ID."""
    init = request.headers.get("X-Telegram-Init-Data", "") or request.query.get("_auth", "")
    user = check_init_data_multi(init, _app_bot_tokens(), AUTH_MAX_AGE)
    return bool(user and OWNER_ID and user.get("id") == OWNER_ID)


def _role(request: web.Request) -> str | None:
    """'owner' | 'guest' (id в PRAXIS_PANEL_GUEST_IDS, живьём из .env) | None."""
    init = request.headers.get("X-Telegram-Init-Data", "") or request.query.get("_auth", "")
    user = check_init_data_multi(init, _app_bot_tokens(), AUTH_MAX_AGE)
    uid = user.get("id") if user else None
    if uid and OWNER_ID and uid == OWNER_ID:
        return "owner"
    if uid and uid in panel.guest_ids():
        return "guest"
    return None


def _deny():
    return web.json_response({"error": "forbidden"}, status=403)


def _unauthorized():
    return web.json_response(
        {"error": "unauthorized"}, status=401,
        headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"},
    )


_PRAXIS_APP_SERVICE: praxis_app.PraxisAppService | None = None
_PRAXIS_SSE_ACTIVE: dict[str, int] = {}
_PRAXIS_SSE_LOCK = threading.Lock()
_PRAXIS_SSE_PER_PRINCIPAL = 3


def _praxis_service() -> praxis_app.PraxisAppService:
    """Return the server projection; the mini-app never owns another runtime."""
    global _PRAXIS_APP_SERVICE
    if (_PRAXIS_APP_SERVICE is None
            or _PRAXIS_APP_SERVICE.base != BASE.resolve()
            or _PRAXIS_APP_SERVICE.owner_id != str(OWNER_ID)):
        _PRAXIS_APP_SERVICE = praxis_app.PraxisAppService(BASE, owner_id=OWNER_ID)
    return _PRAXIS_APP_SERVICE


def _praxis_viewer(request: web.Request) -> praxis_app.Viewer | None:
    # PASS 24 credentials never enter query strings.  Legacy /panel keeps its
    # old compatibility boundary; every /api/praxis route rejects `_auth`.
    if "_auth" in request.query:
        return None
    init_values = request.headers.getall("X-Telegram-Init-Data", [])
    authorization_values = request.headers.getall("Authorization", [])
    if len(init_values) > 1 or len(authorization_values) > 1:
        return None
    init = (init_values[0] if init_values else "").strip()
    authorization = (authorization_values[0] if authorization_values else "").strip()
    if init and authorization:
        return None
    if init:
        user = check_init_data_multi(init, _app_bot_tokens(), AUTH_MAX_AGE)
        return _praxis_service().viewer(user.get("id") if user else None)
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            return None
        try:
            return _praxis_service().device_viewer(parts[1].strip())
        except praxis_device_auth.DeviceAuthError as exc:
            log.error("Praxis device authentication unavailable: %s", type(exc).__name__)
            return None
    return None


# --------------------------------------------------------------------------- #
#  Legacy mail mini-app API.  The fresh /api/praxis boundary below uses
#  owner-rooted Telegram auth or exact-scope device credentials.
# --------------------------------------------------------------------------- #

def _preview(e: dict, n: int = 200) -> dict:
    """Компактная карточка письма для списка (без полного тела)."""
    return {"hash": e.get("hash"), "from": e.get("from", ""), "subject": e.get("subject", ""),
            "status": e.get("status", ""), "date": e.get("date", ""),
            "preview": (e.get("body", "") or "")[:n], "has_draft": bool((e.get("draft") or "").strip())}


async def api_inbox(request: web.Request):
    if not _owner(request):
        return _deny()
    items = await asyncio.to_thread(mailroom.list_open)
    return web.json_response({"items": [_preview(e) for e in items]})


async def api_letter(request: web.Request):
    if not _owner(request):
        return _deny()
    e = await asyncio.to_thread(mailroom.get, request.match_info["hash"])
    if not e:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({
        "hash": e.get("hash"), "from": e.get("from", ""), "subject": e.get("subject", ""),
        "date": e.get("date", ""), "body": e.get("body", ""), "draft": e.get("draft", ""),
        "status": e.get("status", ""),
    })


async def api_compose(request: web.Request):
    """«Праксис, ответь» — она читает письмо и составляет черновик (её голос). Зовёт модель."""
    if not _owner(request):
        return _deny()
    h = request.match_info["hash"]
    draft = await asyncio.to_thread(agent.compose_mail_reply, h)
    if not draft:
        return web.json_response({"error": "compose failed"}, status=502)
    return web.json_response({"hash": h, "draft": draft, "status": "drafted"})


async def api_approve(request: web.Request):
    """Approve Егора → mailroom SMTP-шлёт черновик (token-free). Это и есть гейт отправки."""
    if not _owner(request):
        return _deny()
    h = request.match_info["hash"]
    out = await asyncio.to_thread(mailroom.send_approved, h)
    ok = out.startswith("Отправлено")
    return web.json_response({"hash": h, "ok": ok, "status": out},
                             status=200 if ok else 502)


async def api_reject(request: web.Request):
    if not _owner(request):
        return _deny()
    h = request.match_info["hash"]
    ok = await asyncio.to_thread(mailroom.reject, h)
    return web.json_response({"hash": h, "ok": bool(ok)})


async def serve_app(request: web.Request):
    """Статика mini-app (HTML). Сам HTML авторизацию делает initData'ом на API-запросах."""
    html = BASE / "mailapp.html"
    if html.exists():
        # no-store: Telegram-вебвью охотно кэширует HTML — свежая панель важнее байтов
        return web.FileResponse(html, headers={"Cache-Control": "no-store"})
    return web.Response(text="mailapp.html не найден", status=500)


# --------------------------------------------------------------------------- #
#  Панель устройства (PASS 4, слой 2): /panel + /api/panel/* — owner-only,
#  read-only интроспекция (логика в panel.py), LLM только explicit (explain/ask)
# --------------------------------------------------------------------------- #

async def serve_panel(request: web.Request):
    html = BASE / "panelapp.html"
    if html.exists():
        return web.FileResponse(html, headers={"Cache-Control": "no-store"})
    return web.Response(text="panelapp.html не найден", status=500)


async def serve_panel_vendor(request: web.Request):
    """Вендоренные библиотеки панели (3d-force-graph) — whitelist по имени."""
    name = request.match_info["name"]
    if name not in ("3d-force-graph.min.js",):
        return web.Response(status=404)
    p = BASE / "panel_static" / name
    if p.exists():
        return web.FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})
    return web.Response(status=404)


# --------------------------------------------------------------------------- #
#  PASS 24 Praxis mini-app.  Fresh, versioned surface over the same runtime.
# --------------------------------------------------------------------------- #

async def serve_praxis_app(request: web.Request):
    path = BASE / "praxisapp.html"
    if path.is_file():
        return web.FileResponse(path, headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' https://telegram.org; "
                "style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
                "font-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        })
    return web.Response(text="praxisapp.html not found", status=500)


_PX_CSP = (
    "default-src 'self'; script-src 'self' https://telegram.org; "
    "style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
)


def _px_rewrite_app_js(src: str) -> str:
    """Отдаём тот же app.js, но в изолированном scope /px и без залипающего кэша:
    (1) НЕ регистрируем service worker — вместо этого сносим все прежние регистрации,
        поэтому /px всегда тянется из сети (никакой старой оболочки, физически);
    (2) в браузере ссылка #enroll= привязывает устройство сразу (одно касание вместо
        танца «добавь на экран → вставь ссылку»); установленной иконке фрагмент всё
        равно недоступен (iOS его срезает) — там остаётся честная вставка;
    (3) все /app/static/… пути → /px/static/… (ambient и т.п.)."""
    src = src.replace(
        'navigator.serviceWorker.register("/app/static/sw.js", { scope: "/app" })',
        'navigator.serviceWorker.getRegistrations().then((rs) => rs.forEach((r) => r.unregister()))',
    )
    # Авто-привязка: как только в URL есть #enroll=, сразу дожимаем POST /device/enroll
    # (тот же submitEnrollment, что и по кнопке — проверено живьём, 200), без листа и
    # без касания. «Открыл ссылку → внутри». Лист с кнопкой на его мелькающем iOS
    # терялся, поэтому убираем ручной шаг целиком.
    src = src.replace(
        "if (installedStandalone()) openEnrollmentForm(enrollmentToken);",
        "if (false) openEnrollmentForm(enrollmentToken);",
    )
    src = src.replace(
        "else openInstallGuidance(enrollmentToken);",
        ('else { model.enrollmentToken = enrollmentToken; '
         'const _pf = document.createElement("form"); '
         '_pf.dataset.enrollmentForm = "device"; '
         'const _pb = document.createElement("button"); _pb.type = "submit"; '
         '_pf.appendChild(_pb); submitEnrollment(_pf); }'),
    )
    src = src.replace("/app/static/", "/px/static/")
    return src


async def serve_praxis_app_px(request: web.Request):
    """Свежий независимый scope /px. Старый /app-PWA (его service worker + кэш) физически
    НЕ перехватывает /px, а сам /px не регистрирует SW и отдаётся no-store — поэтому здесь
    гарантированно текущий апп, без залипшей старой оболочки. Все ассеты и манифест
    перенацелены на /px/*, чтобы iOS ставил отдельное, изолированное приложение."""
    path = BASE / "praxisapp.html"
    if not path.is_file():
        return web.Response(text="praxisapp.html not found", status=500)
    html = path.read_text(encoding="utf-8")
    html = html.replace("/app/static/", "/px/static/")
    html = html.replace("?v=31", "?v=32")
    # манифест держим на своём JSON-роуте (правильный scope), не на статике
    html = html.replace("/px/static/manifest.webmanifest", "/px/manifest.webmanifest")
    return web.Response(text=html, content_type="text/html", headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": _PX_CSP,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })


async def serve_px_manifest(request: web.Request):
    manifest = {
        "id": "/px", "name": "Praxis", "short_name": "Praxis",
        "description": "Живой контур Praxis.", "lang": "ru",
        "start_url": "/px", "scope": "/px",
        "display": "standalone", "display_override": ["standalone", "minimal-ui"],
        "background_color": "#080b12", "theme_color": "#080b12",
        "icons": [
            {"src": "/px/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/px/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return web.json_response(manifest, headers={"Cache-Control": "no-cache"},
                            content_type="application/manifest+json")


def _static_font(name: str):
    """Шрифты живут в том же praxis_static, но отдаются с вечным кэшем: начертания
    неизменяемы, а /px намеренно no-store — без этого лицо приложения качалось бы
    заново при каждом открытии. Имя проверяем вручную: точки в основе запрещены,
    поэтому «..» и любой выход из каталога невозможны."""
    if not name.endswith(".woff2"):
        return None
    stem = name[:-len(".woff2")]
    if not stem or not stem.isascii():
        return None
    if not all(ch.isalnum() or ch == "-" for ch in stem):
        return None
    path = BASE / "praxis_static" / name
    if not path.is_file():
        return None
    response = web.FileResponse(path, headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    })
    response.content_type = "font/woff2"
    return response


async def serve_px_static(request: web.Request):
    """Ассеты /px/static/* — те же файлы praxis_static, но app.js переписан под /px и
    всё отдаётся no-store (никакого persistent-кэша, чтобы «старое приложение» больше
    не воскресало). sw.js отсюда не отдаём — /px намеренно без service worker."""
    name = request.match_info["name"]
    font = _static_font(name)
    if font is not None:
        return font
    content_types = {
        "app.css": "text/css",
        "app.js": "text/javascript",
        "ambient.js": "text/javascript",
        "memory_views.js": "text/javascript",
        "space3d.js": "text/javascript",
        "icon.svg": "image/svg+xml",
        "icon-192.png": "image/png",
        "icon-512.png": "image/png",
    }
    if name not in content_types:
        return web.Response(status=404)
    path = BASE / "praxis_static" / name
    if not path.is_file():
        return web.Response(status=404)
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if name == "app.js":
        body = _px_rewrite_app_js(path.read_text(encoding="utf-8"))
        return web.Response(text=body, content_type="text/javascript", headers=headers)
    response = web.FileResponse(path, headers=headers)
    response.content_type = content_types[name]
    return response


async def serve_praxis_static(request: web.Request):
    name = request.match_info["name"]
    font = _static_font(name)
    if font is not None:
        return font
    content_types = {
        "app.css": "text/css",
        "app.js": "text/javascript",
        "ambient.js": "text/javascript",
        "memory_views.js": "text/javascript",
        "space3d.js": "text/javascript",
        "manifest.webmanifest": "application/manifest+json",
        "sw.js": "text/javascript",
        "icon.svg": "image/svg+xml",
        "icon-192.png": "image/png",
        "icon-512.png": "image/png",
    }
    if name not in content_types:
        return web.Response(status=404)
    path = BASE / "praxis_static" / name
    if not path.is_file():
        return web.Response(status=404)
    headers = {
        "Cache-Control": "no-cache, max-age=0, must-revalidate",
        "X-Content-Type-Options": "nosniff",
    }
    if name == "sw.js":
        headers["Service-Worker-Allowed"] = "/app"
    response = web.FileResponse(path, headers=headers)
    response.content_type = content_types[name]
    return response


def _praxis_error(exc: BaseException):
    if isinstance(exc, praxis_device_auth.DeviceAuthPermissionError):
        return _deny()
    if isinstance(exc, praxis_device_auth.InvalidEnrollment):
        return web.json_response({"error": "invalid_enrollment"}, status=400)
    if isinstance(exc, praxis_device_auth.DeviceAuthError):
        log.error("Praxis device authority unavailable: %s", type(exc).__name__)
        return web.json_response({"error": "device_auth_unavailable"}, status=503)
    if isinstance(exc, PermissionError):
        return _deny()
    if isinstance(exc, (run_manager.RunConflict, run_manager.InvalidTransition)):
        return web.json_response({
            "error": "conflict", "message": str(exc)[:500],
        }, status=409)
    if isinstance(exc, (run_manager.RunNotFound, FileNotFoundError)):
        return web.json_response({"error": "not_found"}, status=404)
    if isinstance(exc, (ValueError, TypeError)):
        return web.json_response({
            "error": "bad_request", "message": str(exc)[:500],
        }, status=400)
    log.exception("Praxis app API failed", exc_info=exc)
    return web.json_response({"error": "internal"}, status=500)


async def api_praxis_snapshot(request: web.Request):
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    try:
        data = await asyncio.to_thread(_praxis_service().snapshot, viewer)
        return web.json_response(data, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _praxis_error(exc)


# --------------------------------------------------------------------------- #
#  Виды памяти. Две проекции её памяти только на чтение: граф людей/тем
#  (panel.memgraph, логика не тронута) и DAG сжатия жизни (memory_viz — чистый
#  читатель по memory/life/**). Гейт — praxis.snapshot, тот же, по которому
#  Viewer.public уже отдаёт секцию памяти (карты и поиск), иначе установленный
#  PWA с role="device" получал бы 403. Ни один маршрут не пишет и не пересобирает.
# --------------------------------------------------------------------------- #

async def api_praxis_memory_graph(request: web.Request):
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    if not viewer.may("praxis.snapshot"):
        return _deny()
    try:
        data = await asyncio.to_thread(panel.memgraph)
        return web.json_response(data, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _praxis_error(exc)


async def api_praxis_memory_compact_dag(request: web.Request):
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    if not viewer.may("praxis.snapshot"):
        return _deny()
    chat_values = request.query.getall("chat", [])
    if len(chat_values) > 1:
        return web.json_response(
            {"error": "bad_request", "message": "one chat parameter"}, status=400,
            headers={"Cache-Control": "no-store"},
        )
    chat = chat_values[0].strip() if chat_values else ""
    try:
        data = await asyncio.to_thread(memory_viz.compact_dag, chat or None)
        return web.json_response(data, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _praxis_error(exc)


# --------------------------------------------------------------------------- #
#  Вид кода. Третья проекция только на чтение: живой граф её репозитория
#  (panel.graph, логика не тронута) — модули, скиллы и документы как узлы,
#  импорты/вызовы/чтения как рёбра. Гейт тот же praxis.snapshot, что у видов
#  памяти. panel.graph сам кэшируется по отпечатку дерева, поэтому свой кэш тут
#  не нужен и вреден: её самоправка должна становиться видимой сразу.
# --------------------------------------------------------------------------- #

async def api_praxis_code_graph(request: web.Request):
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    if not viewer.may("praxis.snapshot"):
        return _deny()
    try:
        data = await asyncio.to_thread(panel.graph)
        return web.json_response(data, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _praxis_error(exc)


async def api_praxis_events_ticket(request: web.Request):
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    if viewer.role == "device" and not viewer.may("praxis.events"):
        return _deny()
    return web.json_response(
        praxis_app.TICKETS.issue(
            viewer, validator=_praxis_service().revalidate_event_ticket,
        ),
        headers={"Cache-Control": "no-store"},
    )


async def api_praxis_events(request: web.Request):
    # Native EventSource cannot set Telegram's auth header.  It receives only a
    # one-use opaque ticket; signed initData never appears in a URL or proxy log.
    ticket_values = request.query.getall("ticket", [])
    has_header_auth = bool(
        request.headers.getall("Authorization", [])
        or request.headers.getall("X-Telegram-Init-Data", [])
    )
    if ("_auth" in request.query or has_header_auth
            or len(ticket_values) != 1 or not ticket_values[0]):
        for presented in set(ticket_values):
            if presented:
                praxis_app.TICKETS.consume(presented)
        return _unauthorized()
    viewer = praxis_app.TICKETS.consume(ticket_values[0])
    if viewer is None:
        return _unauthorized()
    principal = viewer.principal_id
    with _PRAXIS_SSE_LOCK:
        active = _PRAXIS_SSE_ACTIVE.get(principal, 0)
        if active >= _PRAXIS_SSE_PER_PRINCIPAL:
            return web.json_response(
                {"error": "too_many_event_streams"}, status=429,
                headers={"Retry-After": "5", "Cache-Control": "no-store"},
            )
        _PRAXIS_SSE_ACTIVE[principal] = active + 1
    response = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-store",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "Referrer-Policy": "no-referrer",
    })
    try:
        await response.prepare(request)
        last = ""
        try:
            for tick in range(150):  # five-minute connection; client renews its ticket
                current = await asyncio.to_thread(
                    _praxis_service().revalidate_event_ticket, viewer,
                )
                if current is None:
                    break
                viewer = current
                revision = await asyncio.to_thread(_praxis_service().revision_hint, viewer)
                if revision != last:
                    payload = json.dumps({"revision": revision}, separators=(",", ":"))
                    await response.write(f"event: snapshot\ndata: {payload}\n\n".encode("utf-8"))
                    last = revision
                elif tick % 10 == 0:
                    await response.write(b": heartbeat\n\n")
                await asyncio.sleep(2)
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            pass
    finally:
        with _PRAXIS_SSE_LOCK:
            remaining = _PRAXIS_SSE_ACTIVE.get(principal, 1) - 1
            if remaining > 0:
                _PRAXIS_SSE_ACTIVE[principal] = remaining
            else:
                _PRAXIS_SSE_ACTIVE.pop(principal, None)
    return response


async def api_praxis_run(request: web.Request):
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    try:
        data = await asyncio.to_thread(
            _praxis_service().run_detail, viewer, request.match_info["run_id"],
        )
        return web.json_response(data, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _praxis_error(exc)


async def api_praxis_run_control(request: web.Request):
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    try:
        body = await request.json()
        data = await asyncio.to_thread(
            _praxis_service().control_run,
            viewer,
            request.match_info["run_id"],
            str(body.get("action") or ""),
            reason=str(body.get("reason") or "")[:1000],
            expected_revision=body.get("expected_revision"),
        )
        return web.json_response(data, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _praxis_error(exc)


async def api_praxis_artifact(request: web.Request):
    ticket_values = request.query.getall("ticket", [])
    init_values = request.headers.getall("X-Telegram-Init-Data", [])
    authorization_values = request.headers.getall("Authorization", [])
    ticket_mode = len(ticket_values) == 1
    header_mode = bool(init_values or authorization_values)
    invalid_shape = (
        "_auth" in request.query
        or len(ticket_values) > 1
        or len(init_values) > 1
        or len(authorization_values) > 1
        or (bool(init_values) and bool(authorization_values))
        or ticket_mode == header_mode
    )
    if invalid_shape:
        # A one-use ticket presented in an ambiguous request is still spent;
        # the server never silently ignores one credential in favour of another.
        for presented in set(ticket_values):
            if not presented:
                continue
            praxis_app.ARTIFACT_TICKETS.authorize(
                presented, request.match_info["run_id"],
                request.match_info["artifact_id"], consume=True,
            )
        return _unauthorized()

    viewer = None
    grant = None
    if ticket_mode:
        grant = praxis_app.ARTIFACT_TICKETS.authorize(
            ticket_values[0],
            request.match_info["run_id"],
            request.match_info["artifact_id"],
            consume=request.method != "HEAD",
        )
        if grant is None:
            return _unauthorized()
    else:
        viewer = _praxis_viewer(request)
        if viewer is None:
            return _unauthorized()
    try:
        if grant is not None:
            path, card = await asyncio.to_thread(
                _praxis_service().artifact_path_from_ticket, grant,
            )
        else:
            path, card = await asyncio.to_thread(
                _praxis_service().artifact_path,
                viewer,
                request.match_info["run_id"],
                request.match_info["artifact_id"],
            )
        media_type = str(card.get("media_type") or "application/octet-stream")
        inline_raster = (
            grant is not None
            and grant.presentation == "image"
            and media_type.lower().split(";", 1)[0].strip()
            in {"image/png", "image/jpeg", "image/webp", "image/gif"}
        )
        disposition = "inline" if inline_raster else "attachment"
        response = web.FileResponse(path, headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                f"{disposition}; filename*=UTF-8''"
                + quote(str(card.get("name") or path.name), safe="")
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Praxis-SHA256": str(card.get("sha256") or ""),
        })
        response.content_type = media_type
        return response
    except Exception as exc:
        return _praxis_error(exc)


async def api_praxis_access(request: web.Request):
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    try:
        body = await request.json()
        result = await asyncio.to_thread(
            _praxis_service().change_access,
            viewer,
            action=str(body.get("action") or ""),
            telegram_id=body.get("telegram_id"),
            name=str(body.get("name") or "")[:200],
            scopes=body.get("scopes") or (),
        )
        return web.json_response({"ok": True, "access": result})
    except Exception as exc:
        return _praxis_error(exc)


async def api_praxis_device_enrollment(request: web.Request):
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    if not viewer.owner:
        return _deny()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        result = await asyncio.to_thread(
            _praxis_service().issue_device_enrollment,
            viewer,
            label=str(body.get("label") or "Praxis device")[:100],
            scopes=body.get("scopes"),
            ttl_seconds=body.get("ttl_seconds", 900),
        )
        result["enrollment_url"] = praxis_device_auth.build_enrollment_url(
            PRAXIS_APP_URL, result["enrollment_token"],
        )
        return web.json_response(
            {"ok": True, "enrollment": result},
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    except Exception as exc:
        return _praxis_error(exc)


async def api_praxis_device_enroll(request: web.Request):
    # The one-time credential is accepted only in a JSON body.  It is placed in
    # the link fragment by the issuer, so neither HTTP nor reverse-proxy logs see it.
    if ("_auth" in request.query or request.headers.getall("Authorization", [])
            or request.headers.getall("X-Telegram-Init-Data", [])):
        return _deny()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        result = await asyncio.to_thread(
            _praxis_service().redeem_device,
            str(body.get("enrollment_token") or ""),
            platform=str(body.get("platform") or "unknown")[:64],
        )
        return web.json_response(
            {"ok": True, **result},
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    except Exception as exc:
        return _praxis_error(exc)


async def api_praxis_devices(request: web.Request):
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    if not viewer.owner:
        return _deny()
    try:
        devices = await asyncio.to_thread(_praxis_service().list_devices, viewer)
        return web.json_response(
            {"items": devices}, headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return _praxis_error(exc)


async def api_praxis_command(request: web.Request):
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        result = await asyncio.to_thread(_praxis_service().command, viewer, body)
        status = 200 if result.get("ok") else 502
        return web.json_response(result, status=status, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _praxis_error(exc)


async def api_praxis_file_import(request: web.Request):
    """Stream one browser file into the body CAS, then import it on Windows.

    The upload is never queued offline and never retained after the verified
    body receipt.  The client operation key binds a manual retry to the same
    Windows request/operation ids.
    """
    viewer = _praxis_viewer(request)
    if viewer is None:
        return _unauthorized()
    if not viewer.may("computer.files"):
        return _deny()
    stage_dir = BASE / "memory" / ".state" / "praxis_app" / "uploads"
    stage_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(stage_dir, 0o700)
    fd, raw_stage = tempfile.mkstemp(prefix="pwa-", suffix=".upload", dir=stage_dir)
    stage = Path(raw_stage)
    try:
        os.chmod(stage, 0o600)
    except OSError:
        pass
    try:
        with os.fdopen(fd, "wb") as output:
            reader = await request.multipart()
            values: dict[str, str] = {}
            upload_name = ""
            upload_type = "application/octet-stream"
            size = 0
            seen_file = False
            async for part in reader:
                name = str(part.name or "")
                if name == "file":
                    if seen_file or not part.filename:
                        raise ValueError("exactly one named file is required")
                    seen_file = True
                    upload_name = Path(str(part.filename).replace("\\", "/")).name
                    upload_name = "".join(
                        char for char in upload_name if char.isprintable() and char not in "\r\n\0"
                    )[:240]
                    if not upload_name or upload_name in {".", ".."}:
                        raise ValueError("upload filename is invalid")
                    upload_type = str(part.headers.get("Content-Type") or upload_type)[:200]
                    while True:
                        chunk = await part.read_chunk(size=1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > PWA_FILE_MAX_BYTES:
                            return web.json_response(
                                {"error": "file_too_large", "max_bytes": PWA_FILE_MAX_BYTES},
                                status=413, headers={"Cache-Control": "no-store"},
                            )
                        output.write(chunk)
                elif name in {"destination", "execution", "idempotency_key"}:
                    if name in values:
                        raise ValueError(f"duplicate multipart field: {name}")
                    # BodyPartReader.read() materialises the complete field.
                    # Accumulate at most one byte beyond the contract so an
                    # attacker cannot turn a scalar into a second file upload.
                    raw = bytearray()
                    while len(raw) <= 4096:
                        chunk = await part.read_chunk(size=4097)
                        if not chunk:
                            break
                        take = min(len(chunk), 4097 - len(raw))
                        raw.extend(chunk[:take])
                        if take < len(chunk) or len(raw) > 4096:
                            raise ValueError(f"multipart field is too large: {name}")
                    values[name] = bytes(raw).decode("utf-8", "strict")
                else:
                    raise ValueError(f"unsupported multipart field: {name or '<empty>'}")
            output.flush()
            os.fsync(output.fileno())
        if not seen_file:
            raise ValueError("file is required")
        execution = values.get("execution", "interactive").strip().lower()
        if execution not in {"interactive", "system"}:
            raise ValueError("execution must be interactive or system")
        if execution == "system" and not viewer.owner:
            raise PermissionError("SYSTEM execution is sovereign-only")
        result = await asyncio.to_thread(
            _praxis_service().import_uploaded_file,
            viewer,
            stage,
            name=upload_name,
            media_type=upload_type,
            destination=values.get("destination", ""),
            execution=execution,
            idempotency_key=values.get("idempotency_key", ""),
        )
        return web.json_response(
            result, status=200 if result.get("ok") else 502,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return _praxis_error(exc)
    finally:
        try:
            stage.unlink(missing_ok=True)
        except OSError:
            log.warning("failed to remove staged Praxis PWA upload", exc_info=True)


def _panel_json(data, status: int = 200):
    if data is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(data, status=status)


async def api_panel_tree(request: web.Request):
    if not _role(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.tree))


async def api_panel_graph(request: web.Request):
    if not _role(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.graph))


async def api_panel_file(request: web.Request):
    if not _role(request):
        return _deny()
    rel = request.match_info["path"]
    info = await asyncio.to_thread(panel.file_info, rel)
    if info is not None and request.query.get("src") == "1":
        info["source"] = await asyncio.to_thread(panel.file_source, rel)
    return _panel_json(info)


async def api_panel_skills(request: web.Request):
    if not _role(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.skills_map))


async def api_panel_commits(request: web.Request):
    if not _role(request):
        return _deny()
    return _panel_json({"items": await asyncio.to_thread(panel.git_log)})


async def api_panel_commit(request: web.Request):
    if not _role(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.git_show, request.match_info["hash"]))


async def api_panel_logs(request: web.Request):
    if not _owner(request):
        return _deny()
    try:
        n = int(request.query.get("n", "200"))
    except ValueError:
        n = 200
    return _panel_json(await asyncio.to_thread(panel.log_tail, request.match_info["name"], n))


async def api_panel_env_preview(request: web.Request):
    if not _owner(request):
        return _deny()
    body = await request.json()
    return _panel_json(await asyncio.to_thread(panel.env_preview, dict(body.get("changes") or {})))


async def api_panel_env_apply(request: web.Request):
    if not _owner(request):
        return _deny()
    body = await request.json()
    if not body.get("confirm"):
        return web.json_response({"ok": False, "errors": ["нет подтверждения (confirm)"]}, status=400)
    return _panel_json(await asyncio.to_thread(panel.env_apply, dict(body.get("changes") or {})))


async def api_panel_explain_get(request: web.Request):
    if not _owner(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(
        panel.explain_cached, request.match_info["path"], request.query.get("func", "")))


async def api_panel_explain_gen(request: web.Request):
    """Явная генерация объяснения (единственный дорогой GET-путь — по кнопке, кэш по blob)."""
    if not _owner(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(
        panel.explain_generate, request.match_info["path"],
        request.query.get("func", ""), request.query.get("force") == "1"))


async def api_panel_ask(request: web.Request):
    if not _owner(request):
        return _deny()
    body = await request.json()
    return _panel_json(await asyncio.to_thread(
        panel.ask, str(body.get("path", "")), str(body.get("question", ""))))


async def api_panel_task(request: web.Request):
    if not _owner(request):
        return _deny()
    body = await request.json()
    return _panel_json(await asyncio.to_thread(
        panel.file_task, str(body.get("path", "")), str(body.get("request", ""))))




# --- PASS 6 (пульт): пульс / мысли / таски / сервер / env-группы ---

async def api_panel_pulse(request: web.Request):
    role = _role(request)
    if not role:
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.pulse_scoped, role))


async def api_panel_overview(request: web.Request):
    if not _owner(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.overview))


async def api_panel_thoughts(request: web.Request):
    if not _owner(request):
        return _deny()
    days = int(request.query.get("days", "3") or 3)
    return _panel_json(await asyncio.to_thread(panel.thoughts, min(max(days, 1), 14)))


async def api_panel_server(request: web.Request):
    if not _role(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.server_state))


async def api_panel_tasks(request: web.Request):
    if not _owner(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.tasks_list))


async def api_panel_task_add(request: web.Request):
    if not _owner(request):
        return _deny()
    body = await request.json()
    return _panel_json(await asyncio.to_thread(
        panel.task_add, str(body.get("goal", "")), str(body.get("when", "") or "") or None))


async def api_panel_task_cancel(request: web.Request):
    if not _owner(request):
        return _deny()
    body = await request.json()
    return _panel_json(await asyncio.to_thread(panel.task_cancel, str(body.get("id", ""))))


async def api_panel_env_grouped(request: web.Request):
    if not _owner(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.env_grouped))


# --- PASS 10.8: раздел «Комнаты» (режимы, дрейф, кнопки владения) ---

async def api_panel_rooms(request: web.Request):
    if not _owner(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.rooms_list))


async def api_panel_room_set(request: web.Request):
    if not _owner(request):
        return _deny()
    body = await request.json()
    return _panel_json(await asyncio.to_thread(
        panel.room_set, str(body.get("id", "")), str(body.get("action", ""))))




# --- PASS 6.1: контакты + окно отсутствия (конфиг, без движка ответа) ---

async def api_panel_absence(request: web.Request):
    if not _owner(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.absence_state))


async def api_panel_absence_start(request: web.Request):
    if not _owner(request):
        return _deny()
    body = await request.json()
    return _panel_json(await asyncio.to_thread(panel.absence_start, body.get("hours")))


async def api_panel_absence_stop(request: web.Request):
    if not _owner(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.absence_stop))


async def api_panel_absence_note(request: web.Request):
    # PASS 12.1: owner-HMAC; записка-расписание для ответов в отсутствие (свободный текст)
    if not _owner(request):
        return _deny()
    b = await request.json()
    return _panel_json(await asyncio.to_thread(panel.absence_schedule_note, str(b.get("note", ""))))


async def api_panel_contacts(request: web.Request):
    if not _owner(request):
        return _deny()
    state = await asyncio.to_thread(panel.absence_state)
    sugg = await asyncio.to_thread(panel.contacts_suggest)
    return _panel_json({"state": state, "suggest": sugg})


async def api_panel_contact_add(request: web.Request):
    if not _owner(request):
        return _deny()
    b = await request.json()
    return _panel_json(await asyncio.to_thread(
        panel.contact_add, str(b.get("name", "")), str(b.get("username", "")),
        str(b.get("note", "")), str(b.get("id", ""))))


async def api_panel_contact_remove(request: web.Request):
    if not _owner(request):
        return _deny()
    b = await request.json()
    return _panel_json(await asyncio.to_thread(panel.contact_remove, str(b.get("slug", ""))))


async def api_panel_contact_note(request: web.Request):
    if not _owner(request):
        return _deny()
    b = await request.json()
    return _panel_json(await asyncio.to_thread(
        panel.contact_note, str(b.get("slug", "")), str(b.get("note", ""))))


async def api_panel_contact_family(request: web.Request):
    # PASS 12.0.a: owner-HMAC; сделать важный контакт семьёй (known_ids + role: family одним шагом)
    if not _owner(request):
        return _deny()
    b = await request.json()
    return _panel_json(await asyncio.to_thread(
        panel.contact_make_family, str(b.get("slug", ""))))


# --- PASS 6.2: читалка памяти + лента действий ---

async def api_panel_memory(request: web.Request):
    role = _role(request)
    if not role:
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.memory_tree_scoped, role))


async def api_panel_memory_read(request: web.Request):
    role = _role(request)
    if not role:
        return _deny()
    return _panel_json(await asyncio.to_thread(
        panel.memory_read_scoped, request.query.get("path", ""), role))


async def api_panel_memory_form(request: web.Request):
    # PASS 19: request only; the main Praxis clock performs the expensive run.
    if not _owner(request):
        return _deny()
    body = await request.json()
    return _panel_json(await asyncio.to_thread(panel.life_request, str(body.get("depth", "full"))))


async def api_panel_actions(request: web.Request):
    role = _role(request)
    if not role:
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.actions_scoped, role))


async def api_panel_me(request: web.Request):
    role = _role(request)
    if not role:
        return _deny()
    return _panel_json({"role": role})


async def api_panel_proposals(request: web.Request):
    if _role(request) != "owner":
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.proposals_list))


async def api_panel_proposal(request: web.Request):
    if _role(request) != "owner":
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.proposal_view, request.match_info["pid"]))


async def api_panel_proposal_apply(request: web.Request):
    if _role(request) != "owner":
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.proposal_apply, request.match_info["pid"]))


async def api_panel_proposal_reject(request: web.Request):
    if _role(request) != "owner":
        return _deny()
    try:
        data = await request.json()
    except Exception:
        data = {}
    return _panel_json(await asyncio.to_thread(
        panel.proposal_reject, request.match_info["pid"], str(data.get("reason", ""))))


# --- PASS 8.2: плитка «Мозг» (конфиг llm.json) + рестарт с пульта. Owner-only. ---

async def api_panel_memgraph(request: web.Request):
    """PASS 8.6: граф памяти — только владельцу (гостю 403; карта — часть памяти Егора)."""
    if _role(request) != "owner":
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.memgraph))


async def api_panel_llm(request: web.Request):
    if _role(request) != "owner":
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.llm_get))


async def api_panel_llm_apply(request: web.Request):
    if _role(request) != "owner":
        return _deny()
    body = await request.json()
    if not body.get("confirm"):
        return web.json_response({"ok": False, "errors": ["нет подтверждения (confirm)"]}, status=400)
    return _panel_json(await asyncio.to_thread(panel.llm_set, dict(body.get("changes") or {})))


async def api_panel_llm_ping(request: web.Request):
    if _role(request) != "owner":
        return _deny()
    body = await request.json()
    return _panel_json(await asyncio.to_thread(panel.llm_ping, str(body.get("role", ""))))


async def api_panel_llm_usage(request: web.Request):
    """PASS 9.1: расход по ролям (сегодня + 7 дней); деньги только при pricing в llm.json."""
    if _role(request) != "owner":
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.llm_usage))


async def api_panel_appetite(request: web.Request):
    """PASS 18.5: договор об аппетитах — режим, просьба/толкование, обещание, расход, отказы."""
    if _role(request) != "owner":
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.appetite_state))


async def api_panel_appetite_intent(request: web.Request):
    """PASS 18.5: слово Егора — кнопки (kind) или свободный текст; толкует Praxis, не код."""
    if _role(request) != "owner":
        return _deny()
    body = await request.json()
    return _panel_json(await asyncio.to_thread(
        panel.appetite_intent, str(body.get("kind", "")), str(body.get("text", ""))))


# --- PASS 20-22 → пульт (24·Ф3): органы самости. Owner-only. ---

async def api_panel_identity(request: web.Request):
    """PASS 20: плитка «Я» — слои/версии души, деформации, сдвиги."""
    if not _owner(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.identity_state))


async def api_panel_identity_rollback(request: web.Request):
    """PASS 20: откат Егора постфактум (confirm обязателен)."""
    if not _owner(request):
        return _deny()
    body = await request.json()
    if not body.get("confirm"):
        return web.json_response({"error": "нужен confirm"}, status=400)
    return _panel_json(await asyncio.to_thread(
        panel.identity_rollback, str(body.get("name") or ""), int(body.get("version") or 0)))


async def api_panel_perception(request: web.Request):
    """PASS 21: рычаги восприятия + журнал пропусков."""
    if not _owner(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.perception_state))


async def api_panel_perception_set(request: web.Request):
    """PASS 21: правка/сброс рычага Егором (источник честно «egor»; confirm обязателен)."""
    if not _owner(request):
        return _deny()
    body = await request.json()
    if not body.get("confirm"):
        return web.json_response({"error": "нужен confirm"}, status=400)
    return _panel_json(await asyncio.to_thread(
        panel.perception_set, str(body.get("knob") or ""), body.get("value"),
        bool(body.get("reset"))))


async def api_panel_brain_catalog(request: web.Request):
    """PASS 22: каталог моделей + наблюдаемые свойства + её свитчи."""
    if not _owner(request):
        return _deny()
    return _panel_json(await asyncio.to_thread(panel.brain_catalog))


async def api_panel_restart(request: web.Request):
    """Кнопка «Перезапустить Praxis»: мягкий запрос — часы `control` в раннере подхватят
    (exit 42, bootguard поднимет на текущем коде; llm.json перечитается свежими клиентами)."""
    if _role(request) != "owner":
        return _deny()
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str(body.get("reason", "") or "рестарт с пульта")[:120]
    await asyncio.to_thread(selfdev.request_restart, "панель: " + reason)
    return _panel_json({"ok": True,
                        "note": "Запрос принят — перезапустится на ближайшем тике часов (сек ~5)."})


def make_app() -> web.Application:
    app = web.Application(client_max_size=PWA_FILE_MAX_BYTES + 1024 * 1024)
    app.add_routes([
        web.get("/", serve_app),
        web.get("/api/inbox", api_inbox),
        web.get("/api/letter/{hash}", api_letter),
        web.post("/api/compose/{hash}", api_compose),
        web.post("/api/approve/{hash}", api_approve),
        web.post("/api/reject/{hash}", api_reject),
        # PASS 24: fresh Praxis mini-app; legacy /panel remains available.
        web.get("/app", serve_praxis_app),
        web.get("/px", serve_praxis_app_px),
        web.get("/px/manifest.webmanifest", serve_px_manifest),
        web.get("/px/static/{name}", serve_px_static),
        web.get("/app/static/{name}", serve_praxis_static),
        web.get("/api/praxis/v1/snapshot", api_praxis_snapshot),
        web.get("/api/praxis/v1/events-ticket", api_praxis_events_ticket),
        web.get("/api/praxis/v1/events", api_praxis_events),
        web.post("/api/praxis/v1/device/enrollment", api_praxis_device_enrollment),
        web.post("/api/praxis/v1/device/enroll", api_praxis_device_enroll),
        web.get("/api/praxis/v1/devices", api_praxis_devices),
        web.get("/api/praxis/v1/runs/{run_id}", api_praxis_run),
        web.post("/api/praxis/v1/runs/{run_id}/control", api_praxis_run_control),
        web.get("/api/praxis/v1/runs/{run_id}/artifacts/{artifact_id}",
                api_praxis_artifact),
        web.post("/api/praxis/v1/access", api_praxis_access),
        web.post("/api/praxis/v1/command", api_praxis_command),
        web.post("/api/praxis/v1/files/import", api_praxis_file_import),
        web.get("/api/praxis/v1/memory/graph", api_praxis_memory_graph),
        web.get("/api/praxis/v1/memory/compact-dag", api_praxis_memory_compact_dag),
        web.get("/api/praxis/v1/code/graph", api_praxis_code_graph),
        # панель устройства
        web.get("/panel", serve_panel),
        web.get("/panel/vendor/{name}", serve_panel_vendor),
        web.get("/api/panel/tree", api_panel_tree),
        web.get("/api/panel/graph", api_panel_graph),
        web.get("/api/panel/skills", api_panel_skills),
        web.get("/api/panel/commits", api_panel_commits),
        web.get("/api/panel/commit/{hash}", api_panel_commit),
        web.get("/api/panel/logs/{name}", api_panel_logs),
        web.get("/api/panel/pulse", api_panel_pulse),
        web.get("/api/panel/overview", api_panel_overview),
        web.get("/api/panel/thoughts", api_panel_thoughts),
        web.get("/api/panel/server", api_panel_server),
        web.get("/api/panel/tasks", api_panel_tasks),
        web.post("/api/panel/tasks/add", api_panel_task_add),
        web.post("/api/panel/tasks/cancel", api_panel_task_cancel),
        web.get("/api/panel/env2", api_panel_env_grouped),
        web.get("/api/panel/rooms", api_panel_rooms),
        web.post("/api/panel/rooms/set", api_panel_room_set),
        web.get("/api/panel/absence", api_panel_absence),
        web.post("/api/panel/absence/start", api_panel_absence_start),
        web.post("/api/panel/absence/stop", api_panel_absence_stop),
        web.post("/api/panel/absence/note", api_panel_absence_note),
        web.get("/api/panel/me", api_panel_me),
        web.get("/api/panel/proposals", api_panel_proposals),
        web.get("/api/panel/proposal/{pid}", api_panel_proposal),
        web.post("/api/panel/proposal/{pid}/apply", api_panel_proposal_apply),
        web.post("/api/panel/proposal/{pid}/reject", api_panel_proposal_reject),
        web.get("/api/panel/memory", api_panel_memory),
        web.get("/api/panel/memory/read", api_panel_memory_read),
        web.post("/api/panel/memory/form", api_panel_memory_form),
        web.get("/api/panel/actions", api_panel_actions),
        web.get("/api/panel/contacts", api_panel_contacts),
        web.post("/api/panel/contacts/add", api_panel_contact_add),
        web.post("/api/panel/contacts/remove", api_panel_contact_remove),
        web.post("/api/panel/contacts/note", api_panel_contact_note),
        web.post("/api/panel/contacts/family", api_panel_contact_family),
        web.post("/api/panel/env/preview", api_panel_env_preview),
        web.post("/api/panel/env/apply", api_panel_env_apply),
        web.get("/api/panel/memgraph", api_panel_memgraph),
        web.get("/api/panel/llm", api_panel_llm),
        web.post("/api/panel/llm/apply", api_panel_llm_apply),
        web.post("/api/panel/llm/ping", api_panel_llm_ping),
        web.get("/api/panel/llm/usage", api_panel_llm_usage),
        web.get("/api/panel/appetite", api_panel_appetite),           # PASS 18.5
        web.post("/api/panel/appetite/intent", api_panel_appetite_intent),
        web.get("/api/panel/identity", api_panel_identity),           # PASS 20 (24·Ф3)
        web.post("/api/panel/identity/rollback", api_panel_identity_rollback),
        web.get("/api/panel/perception", api_panel_perception),       # PASS 21 (24·Ф3)
        web.post("/api/panel/perception/set", api_panel_perception_set),
        web.get("/api/panel/brain/catalog", api_panel_brain_catalog),  # PASS 22 (24·Ф3)
        web.post("/api/panel/restart", api_panel_restart),
        web.get("/api/panel/explain/{path:.+}", api_panel_explain_get),
        web.post("/api/panel/explain/{path:.+}", api_panel_explain_gen),
        web.post("/api/panel/ask", api_panel_ask),
        web.post("/api/panel/task", api_panel_task),
        web.get("/api/panel/file/{path:.+}", api_panel_file),
    ])
    return app


# --------------------------------------------------------------------------- #
#  Бот: menu-button открывает mini-app; пуш на новое письмо
# --------------------------------------------------------------------------- #

PENDING_NOTE: dict[int, dict] = {}  # owner_id -> {"slug":…, "name":…} — ждём досье следующим сообщением


def _reply_kb() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура: приложение Praxis + нативный выбор важного из контактов (механика бота)."""
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[
        KeyboardButton(text="Praxis", web_app=WebAppInfo(url=PRAXIS_APP_URL)),
        KeyboardButton(text="👥 Добавить важного", request_users=KeyboardButtonRequestUsers(
            request_id=42, user_is_bot=False, max_quantity=1, request_name=True, request_username=True)),
    ]])


def _open_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Praxis", web_app=WebAppInfo(url=PRAXIS_APP_URL))],
    ])


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    if OWNER_ID and message.from_user and message.from_user.id != OWNER_ID:
        viewer = _praxis_service().viewer(message.from_user.id)
        if viewer is not None and viewer.role == "trusted":
            scopes = ", ".join(viewer.scopes)
            await message.answer(
                "Привет! Егор открыл тебе точные возможности Praxis: " + scopes + ". "
                "Передавать этот доступ или добавлять других людей нельзя.",
                reply_markup=ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[
                    KeyboardButton(text="Praxis", web_app=WebAppInfo(url=PRAXIS_APP_URL))]]))
            return
        if message.from_user.id in panel.guest_ids():
            await message.answer(
                "Тебе оставлен прежний read-only пульт Praxis.",
                reply_markup=ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[
                    KeyboardButton(text="Praxis", web_app=WebAppInfo(url=PANEL_URL))]]))
            return
        await message.answer("Это пульт Praxis — он не для тебя.")
        return
    await message.answer(
        "Пульт Praxis. Кнопка «Praxis» — приложение; «👥 Добавить важного» — выбор из твоих "
        "контактов (после выбора расскажи про человека — запишу ей в память; /skip — без досье).",
        reply_markup=_reply_kb())
    await message.answer("Разделы:", reply_markup=_open_kb())


@dp.message(Command("panel"))
async def on_panel(message: Message) -> None:
    if OWNER_ID and message.from_user and message.from_user.id != OWNER_ID:
        return
    await message.answer("Praxis:", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть", web_app=WebAppInfo(url=PRAXIS_APP_URL))]]))


@dp.message(F.users_shared)
async def on_user_shared(message: Message) -> None:
    if OWNER_ID and message.from_user and message.from_user.id != OWNER_ID:
        return
    us = message.users_shared
    if not us or not us.users:
        return
    u = us.users[0]
    name = " ".join(x for x in [getattr(u, "first_name", None), getattr(u, "last_name", None)] if x) \
        or (getattr(u, "username", None) or f"id{u.user_id}")
    r = await asyncio.to_thread(panel.contact_add, name, getattr(u, "username", "") or "", "", str(u.user_id))
    if r.get("error"):
        await message.answer(f"⚠️ {r['error']}")
        return
    slug = next((c["slug"] for c in panel.absence_state()["contacts"] if c.get("id") == str(u.user_id)), None)
    PENDING_NOTE[message.from_user.id] = {"slug": slug, "name": name}
    await message.answer(f"👥 {name} — в важных. Теперь расскажи про него пару слов (уйдёт ей в память, "
                         f"private) — или /skip.")


@dp.message(Command("skip"))
async def on_skip(message: Message) -> None:
    if message.from_user and PENDING_NOTE.pop(message.from_user.id, None):
        await message.answer("Ок, без досье. Дополнить можно всегда — или расскажи ей в личке.")


@dp.message(F.text & ~F.text.startswith("/"))
async def on_note_text(message: Message) -> None:
    if OWNER_ID and message.from_user and message.from_user.id != OWNER_ID:
        return
    pend = PENDING_NOTE.pop(message.from_user.id, None) if message.from_user else None
    if not pend or not pend.get("slug"):
        return  # обычный текст без контекста — не наша механика
    r = await asyncio.to_thread(panel.contact_note, pend["slug"], message.text, pend["name"])
    await message.answer("Записала ей в память ✅" if r.get("ok") else f"⚠️ {r.get('error', 'не вышло')}")


def _proposal_card(t: dict) -> str:
    tests = t.get("tests") or {}
    files = t.get("files") or []
    flist = ", ".join(files[:6]) + (f" (+{len(files) - 6})" if len(files) > 6 else "")
    zone = {"protected": "⚠️ защищённая зона (рельсы/секреты)", "review": "обычная",
            "auto": "авто"}.get(t.get("zone"), t.get("zone") or "—")
    ok = tests.get("ok")
    im = t.get("immune") or {}
    im_line = ""
    if im.get("verdict"):
        mark = {"ok": "✅", "warn": "⚠️", "red": "⛔"}.get(im["verdict"], "")
        im_line = (f"\nИммунитет: {mark} {im['verdict']}"
                   + (f" — {im.get('why')}" if im.get("why") else "")
                   + ("\n(red — сильное второе мнение, не veto)"
                      if im["verdict"] == "red" else ""))
    review = (t.get("review") or "").strip()       # PASS 16.4: её собственное ревью диффа
    checked = (t.get("checked") or "").strip()     # и чем она проверила
    rv_line = f"\nЕё ревью: {review[:600]}" if review else ""
    ck_line = f"\nПроверено: {checked[:200]}" if checked else ""
    return (f"🛠 Она предлагает изменить свой код — #{t['id']}\n"
            f"«{t.get('title') or 'без названия'}»\n"
            f"Зачем: {t.get('why') or '—'}\n"
            f"Файлы: {flist or '—'}\n"
            f"Дифф: {t.get('diffstat') or '—'}\n"
            f"Тесты: {'✅ ' if ok else '❌ '}{(tests.get('summary') or '—').splitlines()[0]}"
            f"{rv_line}{ck_line}\n"
            f"Зона: {zone}{im_line}")


def _proposal_kb(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Мёрж", callback_data=f"prop:ok:{pid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"prop:no:{pid}"),
    ]])


async def _watch_proposals(bot: Bot) -> None:
    """Токен-free вотчер: новые предложения из леджера -> карточка Егору с кнопками.
    PASS 9.2: сюда же — red-карточки иммунитета по живым самоправкам (без кнопок:
    откат не автоматический, git всё хранит)."""
    while True:
        try:
            for t in await asyncio.to_thread(selfdev.unnotified):
                try:
                    if t.get("status") == "proposed":
                        await bot.send_message(OWNER_ID, _proposal_card(t),
                                               reply_markup=_proposal_kb(t["id"]))
                    else:  # self-merged — post-factum receipt, not an approval request
                        im = t.get("immune") or {}
                        im_note = f" Иммунитет: {im['verdict']}." if im.get("verdict") else ""
                        rv = (t.get("review") or "").strip()   # PASS 16.4: ревью видно и здесь
                        rv_note = f"\nЕё ревью: {rv[:400]}" if rv else ""
                        tests = t.get("tests") or {}
                        test_note = (
                            "Тесты зелёные."
                            if tests.get("ok") else
                            f"Тесты красные: {(tests.get('summary') or 'без сводки').splitlines()[0]}."
                        )
                        override = str(t.get("override_reason") or "").strip()
                        override_note = f"\nОсознанный override: {override[:500]}" if override else ""
                        await bot.send_message(
                            OWNER_ID, f"🔧 Смёржила сама (зона {t.get('zone') or '—'}): "
                                      f"«{t.get('title') or t['id']}» ({t.get('diffstat') or '—'})."
                                      f" {test_note}{im_note} "
                                      f"Перезапустится на новом коде.{rv_note}{override_note}")
                    await asyncio.to_thread(selfdev.mark_notified, t["id"])
                except Exception:
                    log.warning("карточка предложения %s не ушла (Start у бота?)", t.get("id"))
                    break  # не помечаем notified — повторим на следующем тике
            for c in await asyncio.to_thread(immune.pending_cards):
                try:
                    await bot.send_message(
                        OWNER_ID,
                        f"🛡 Иммунитет: red на её живую самоправку {c.get('sha', '')[:8]}\n"
                        f"«{c.get('message') or '—'}»\n"
                        f"Почему: {c.get('why') or '—'}\n\n"
                        f"Дифф (git хранит всё, откат — твоё решение):\n"
                        f"{(c.get('diff') or '')[:3000]}")
                    await asyncio.to_thread(immune.mark_card_sent, c.get("id"))
                except Exception:
                    log.warning("карточка иммунитета %s не ушла", c.get("id"))
                    break
            # PASS 10.4/10.5/10.7: карточки комнат (замри/дрейф-freeze/вошла в группу)
            for c in await asyncio.to_thread(_rooms.pending_cards):
                try:
                    await bot.send_message(OWNER_ID, f"🚪 Комната {c.get('chat_id')}:\n{c.get('text')}")
                    await asyncio.to_thread(_rooms.mark_card_sent, c.get("id"))
                except Exception:
                    log.warning("карточка комнаты %s не ушла", c.get("id"))
                    break
            # PASS 17.C: заявки на правку хоста. Карточка БЕЗ кнопок — апрув идёт командой
            # на хосте (hostagent apply <id>), которую её процесс подделать не может.
            for op in await asyncio.to_thread(hostops.unnotified):
                try:
                    await bot.send_message(OWNER_ID, hostops.card_text(op))
                    await asyncio.to_thread(hostops.mark_notified, op["id"])
                except Exception:
                    log.warning("карточка правки хоста %s не ушла", op.get("id"))
                    break
        except Exception:
            log.exception("вотчер предложений упал")
        await asyncio.sleep(10)


@dp.callback_query(F.data.startswith("prop:"))
async def on_proposal_button(cq: CallbackQuery) -> None:
    if OWNER_ID and (not cq.from_user or cq.from_user.id != OWNER_ID):
        await cq.answer("Это кнопки Егора.", show_alert=False)
        return
    try:
        _, action, pid = (cq.data or "").split(":", 2)
    except ValueError:
        await cq.answer("Не поняла кнопку.")
        return
    if action == "ok":
        r = await asyncio.to_thread(panel.proposal_apply, pid)
        verdict = "✅ Смёржено — она перезапустится на новом коде." if r.get("ok") else f"⚠️ {r.get('msg')}"
    else:
        r = await asyncio.to_thread(panel.proposal_reject, pid, "")
        verdict = "❌ Отклонено — причина ушла ей в дневник." if r.get("ok") else f"⚠️ {r.get('msg')}"
    try:
        await cq.message.edit_text((cq.message.text or "") + f"\n\n{verdict}")
    except Exception:
        pass
    await cq.answer(verdict[:180])


def _exit_process() -> None:  # вынесено для тестов
    os._exit(0)


async def _check_restart_signal(bot: Bot) -> bool:
    """Один тик: если praxis попросила (agent.tool_restart_mailbot записал файл-флаг) --
    вычитать причину, снести флаг, предупредить владельца, выйти. -> True если вышли (тестам)."""
    flag = agent.RESTART_MAILBOT_FLAG
    if not await asyncio.to_thread(flag.exists):
        return False
    reason = ""
    try:
        reason = await asyncio.to_thread(flag.read_text, encoding="utf-8")
    except Exception:
        pass
    try:
        await asyncio.to_thread(flag.unlink)
    except Exception:
        pass
    log.warning("RESTART signal from praxis: %s", reason.strip() or "без причины")
    try:
        await bot.send_message(
            OWNER_ID, f"\U0001F501 Mailbot перезапускается по сигналу от Praxis: "
                      f"{reason.strip() or 'без причины'}")
    except Exception:
        log.warning("уведомление о рестарте mailbot не ушло")
    _exit_process()
    return True


async def _watch_restart_signal(bot: Bot) -> None:
    """Токен-free вотчер: сопряжённый перезапуск по сигналу от praxis (PASS 12.x) --
    её единственный способ тронуть mailbot без Docker-сокета в контейнере."""
    while True:
        try:
            await _check_restart_signal(bot)
        except Exception:
            log.exception("watch-restart тик упал")
        await asyncio.sleep(5)


async def _poll(bot: Bot) -> None:
    """Токен-free тик: тянем IMAP, заводим новые письма, пушим Егору. Модель тут НЕ зовётся."""
    while True:
        try:
            msgs = await asyncio.to_thread(mailer.fetch, FETCH_LIMIT, False)
            added = await asyncio.to_thread(mailroom.ingest, msgs)
            for e in added:
                body = (e.get("body", "") or "")[:200]
                try:
                    await bot.send_message(
                        OWNER_ID,
                        f"📬 Новое письмо `{e['hash']}`\nОт: {e['from']}\nТема: {e['subject']}\n\n{body}",
                        reply_markup=_open_kb(),
                    )
                except Exception:
                    # напр. «chat not found» — Егор ещё не нажал Start у бота. Письмо уже в ящике,
                    # он увидит его в mini-app; не роняем уведомления по остальным письмам пачки.
                    log.warning("уведомление по %s не ушло (нажал ли Егор Start у бота?)", e.get("hash"))
            if added:
                log.info("poll: новых писем %d", len(added))
        except Exception:
            log.exception("poll-тик упал")
        await asyncio.sleep(POLL_SEC)


async def main() -> None:
    if not TOKEN:
        raise SystemExit("Нет PRAXIS_MAIL_BOT_TOKEN (.deploy.env)")
    if not OWNER_ID:
        raise SystemExit("Нет PRAXIS_OWNER_ID")
    if not mailer.configured():
        log.warning("почта не настроена (нет PRAXIS_EMAIL_*) — поллинг вхолостую")
    bot = Bot(TOKEN)
    me = await bot.get_me()
    log.info("mailbot @%s на связи; mini-app=%s порт=%d поллинг=%.0fs", me.username, WEBAPP_URL, PORT, POLL_SEC)
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Praxis",
                                                                    web_app=WebAppInfo(url=PRAXIS_APP_URL)))
    except Exception:
        log.warning("set_chat_menu_button не удалось (несекретно, продолжаю)", exc_info=True)
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("mini-app слушает на 0.0.0.0:%d (за Caddy)", PORT)
    asyncio.create_task(_poll(bot))
    asyncio.create_task(_watch_proposals(bot))
    asyncio.create_task(_watch_restart_signal(bot))
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
