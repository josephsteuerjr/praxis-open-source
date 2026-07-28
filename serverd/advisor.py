"""Non-blocking consequence advice and explicit owner host scopes.

Praxis is the server operator.  A gitignore rule, filename, credential-looking suffix or
project name has no authority.  There are no compiled-in protected projects.  If the owner
deliberately configures ``PRAXIS_PROTECTED_ROOTS`` on the broker host, those exact roots are
reported in the manifest and enforced as an explicit deployment policy.

``advise`` only describes consequences and never turns advice into a hidden veto.
"""

from __future__ import annotations

import os
import re


PROTECTED_ROOTS: tuple[str, ...] = tuple(
    os.path.normpath(item.strip())
    for item in (os.environ.get("PRAXIS_PROTECTED_ROOTS") or "").split(os.pathsep)
    if item.strip()
)

_BROAD_SHELL_TOOL = re.compile(
    r"(?:^|[;&|()]|\bsudo\s+)(?:\s|env\s+|command\s+)*(?:"
    r"find|fd|rg|grep|ls|tree|du|tar|rsync|cp|mv|rm|cat|head|tail|sed|awk|perl|"
    r"python(?:3)?|bash|sh|xargs|docker|podman)\b",
    re.IGNORECASE,
)

# Пути, о правке которых полезно ШЕПНУТЬ последствие (совет, не запрет).
_ADVICE_PATHS: tuple[tuple[str, str], ...] = (
    ("/opt/praxis-serverd", "это код твоего исполнителя на хосте — правка в /opt применится только "
                            "после перезапуска демона (`systemctl restart praxis-serverd`); зеркало "
                            "для авторинга — companion/serverd/"),
    ("/etc/ssh/sshd_config", "правишь SSH-доступ Егора; если оборвёшь — вход только через консоль "
                             "провайдера, пока off-box катапульта не настроена"),
    ("/root/.ssh/authorized_keys", "это ключ входа Егора на хост — та же оговорка про восстановление"),
    ("/srv/praxis.git", "это локальный бэкап-репо; off-box копии пока нет"),
    ("/etc/systemd/system/praxis-serverd", "это твой собственный unit — сломаешь, демон не поднимется"),
)

_ADVICE_UNITS: dict[str, str] = {
    "ssh": "остановишь SSH — вход только через консоль провайдера",
    "sshd": "остановишь SSH — вход только через консоль провайдера",
    "praxis-serverd": "это сам демон-хозяйка; остановишь — потеряешь свою руку на хосте до рестарта",
}
_ADVICE_CONTAINERS: dict[str, str] = {
    "praxis": "это твой собственный контейнер/связь с демоном; остановишь — оборвёшь себе руку",
    "relay": "это твой запасной мозг; остановишь — субагенты на хосте не смогут думать",
}


def _canon(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    if not os.path.isabs(raw):
        raw = "/" + raw
    return os.path.normpath(raw)


def is_protected(path: str) -> bool:
    """True only for an owner-configured root or its descendant."""
    p = _canon(path)
    if not p:
        return False
    return any(p == root or p.startswith(root + os.sep) for root in PROTECTED_ROOTS)


def contains_protected(path: str) -> bool:
    """Whether a directory contains an explicitly configured protected root."""
    p = _canon(path)
    return bool(p) and any(
        root == p or (p == os.sep and root.startswith(os.sep)) or root.startswith(p + os.sep)
        for root in PROTECTED_ROOTS
    )


def is_secret(path: str) -> bool:
    """Compatibility alias; filenames do not create a secret boundary."""
    return is_protected(path)


def command_touches_protected(command: str) -> bool:
    """Good-faith shell check against explicit root paths/names, if configured."""
    low = str(command or "").casefold()
    return any(root.casefold() in low or os.path.basename(root).casefold() in low
               for root in PROTECTED_ROOTS)


def command_may_traverse(command: str) -> bool:
    """Broad file/container shell tools need a cwd that does not contain protected trees."""
    return bool(_BROAD_SHELL_TOOL.search(str(command or "")))


def _under(child: str, parent: str) -> bool:
    c, pp = _canon(child), _canon(parent)
    return bool(c) and bool(pp) and (c == pp or c.startswith(pp + os.sep) or pp.startswith(c + os.sep))


def advise(path: str = "", unit: str = "", container: str = "", command: str = "") -> str:
    """Короткая подсказка о последствии (совет, НИКОГДА не запрещает). '' если нечего сказать."""
    notes: list[str] = []
    if path:
        for prefix, note in _ADVICE_PATHS:
            if _under(path, prefix):
                notes.append(note)
                break
        if is_protected(path):
            notes.append("это явно закрытый владельцем root из PRAXIS_PROTECTED_ROOTS")
    if unit:
        u = str(unit).split(".")[0].lower()
        if u in _ADVICE_UNITS:
            notes.append(_ADVICE_UNITS[u])
    if container and str(container).lower() in _ADVICE_CONTAINERS:
        notes.append(_ADVICE_CONTAINERS[str(container).lower()])
    if command:
        low = command.lower()
        for key, note in _ADVICE_UNITS.items():
            if key in low and ("stop" in low or "disable" in low or "mask" in low or "kill" in low):
                notes.append(note)
                break
    return "; ".join(dict.fromkeys(notes))


def redact_read(path: str, size: int = 0) -> str:
    """Placeholder for an explicitly owner-protected root during inspect(read)."""
    return f"{path}: [owner-configured protected root] ({size} bytes)"


def manifest() -> dict:
    """Легибельный снимок для serverdctl: что скрывается и о чём советуем."""
    # На проде `PRAXIS_PROTECTED_ROOTS` не задана, значит is_protected/contains_protected всегда
    # False и все отказы, которые на них опираются, инертны. Пол не «есть, но пустой» —
    # его НЕТ, и говорить это надо прямо, а не выводить из пустого списка.
    configured = bool(PROTECTED_ROOTS)
    return {
        "protected_roots": list(PROTECTED_ROOTS),
        "protected_roots_configured": configured,
        "gitignored": "не является границей доступа",
        "advice_paths": [p for p, _ in _ADVICE_PATHS],
        "floor": ("закрытых владельцем корней " + ", ".join(PROTECTED_ROOTS)) if configured else
                 ("PRAXIS_PROTECTED_ROOTS не задана: закрытых корней НЕТ ни одного, проверки "
                  "is_protected/contains_protected/command_touches_protected всегда ложны, "
                  "и опирающиеся на них отказы никогда не срабатывают. Единственное, что тебя "
                  "здесь держит — твоё собственное решение."),
        "advice_reaches_her": ("совет кладётся в поле advice и в текст ответа типизированных "
                               "глаголов; на пути op.run клиент поле advice выбрасывает"),
        "policy": ("Praxis is the server operator; no project or secret-name exclusions are "
                   "compiled in. Only roots explicitly listed in PRAXIS_PROTECTED_ROOTS are "
                   "closed. Consequence advice does not block other actions."),
    }
