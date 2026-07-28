"""
Praxis — граф связей поверх файловой памяти («вспомнить связанное»).

Маркдаун — источник правды, как и везде в её памяти. Рёбра живут в двух местах:
  * у людей — секция `## Связи` в people/<slug>.md: `- [[vika]] — коллега _(дата)_`
    (второй конец ребра — файл, в котором строка);
  * не-люди (темы, места, проекты) — memory/graph.md, строка на ребро:
    `- [[тема]] ↔ [[другое]] — подпись _(дата)_`.

Этот модуль — производный взгляд: парсит рёбра, строит смежность в памяти и
отвечает на «что связано» (BFS до 3 шагов) и «как связаны A и B» (кратчайшая
цепочка). Корпус мал (десятки узлов) — никаких индексов и зависимостей, чистый
stdlib; каждый вызов перечитывает файлы, правда всегда свежая.

Скоуп здесь НЕ проверяется: карта «кто с кем связан» — часть карты памяти и
подаётся только владельцу; это забота agent.py (как с INDEX.md и дневником).
"""

from __future__ import annotations

import datetime as _dt
import difflib
import os
import re
from pathlib import Path

import people

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
MEM_DIR = BASE / "memory"
GRAPH_MD = MEM_DIR / "graph.md"

MAX_DEPTH = 3           # дальше 3 шагов «связанное» уже не связано
MAX_NODES_OUT = 12      # потолок узлов в выдаче — защита промпта, не индекса

_GRAPH_HEADER = "# Граф памяти — связи не-людей\n\n"

# `- [[a]] ↔ [[b]] — подпись _(дата)_` (разделитель между узлами — любой из ↔ — - ~)
_PAIR_LINE = re.compile(
    r"^[-*]\s*\[\[([^\]\n]+)\]\]\s*[↔—–~-]*\s*\[\[([^\]\n]+)\]\]\s*"
    r"(?:[—–:-]\s*)?(.*?)\s*(?:_\((\d{4}-\d{2}-\d{2})\)_)?\s*$"
)


def _today() -> str:
    return _dt.date.today().isoformat()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""


def _slug(ref: str) -> str:
    return people._slug(str(ref or ""))


def resolve(ref: str) -> str:
    """Узел по ссылке: слаг как есть, а если такого файла нет — поиск по заголовкам
    И АЛИАСАМ людей (PASS 8.6: «Егор» найдёт yegor-kosyrev.md по строке `Алиасы:`).
    Иначе — просто слаг (узел-тема)."""
    s = _slug(ref)
    if not s or people.path_for(s).exists():
        return s
    want = str(ref or "").strip().lower()
    if people.PEOPLE_DIR.exists():
        for p in people.PEOPLE_DIR.glob("*.md"):
            if p.stem.startswith("_"):
                continue
            title, _ = people.read(p.stem)
            if title and (title.strip().lower() == want or _slug(title) == s):
                return p.stem
            for a in people.aliases(p.stem):
                if a.strip().lower() == want or _slug(a) == s:
                    return p.stem
    return s


def display(slug: str) -> str:
    """Человекочитаемое имя узла: заголовок файла человека, иначе сам слаг."""
    name, _ = people.read(slug)
    return name or slug


# --------------------------------------------------------------------------- #
#  Чтение рёбер
# --------------------------------------------------------------------------- #

def edges() -> list[dict]:
    """Все рёбра графа: [{a, b, label, src}] (неориентированные)."""
    out: list[dict] = []
    if people.PEOPLE_DIR.exists():
        for p in sorted(people.PEOPLE_DIR.glob("*.md")):
            if p.stem.startswith("_"):
                continue
            for dst, label in people.links(p.stem):
                out.append({"a": p.stem, "b": dst, "label": label, "src": f"people/{p.stem}"})
    for raw in _read(GRAPH_MD).splitlines():
        m = _PAIR_LINE.match(raw.strip())
        if m:
            out.append({"a": _slug(m.group(1)), "b": _slug(m.group(2)),
                        "label": (m.group(3) or "").strip(), "src": "graph"})
    return [e for e in out if e["a"] and e["b"] and e["a"] != e["b"]]


def adjacency() -> dict[str, dict[str, list[str]]]:
    """adj[узел][сосед] = [подписи]. Неориентированно, дедуп подписей."""
    adj: dict[str, dict[str, list[str]]] = {}
    for e in edges():
        for x, y in ((e["a"], e["b"]), (e["b"], e["a"])):
            labels = adj.setdefault(x, {}).setdefault(y, [])
            if e["label"] and e["label"].lower() not in (l.lower() for l in labels):
                labels.append(e["label"])
    return adj


def _labels_close(l1: str, l2: str) -> bool:
    """Близкая ли подпись (PASS 8.6: гигиена — «коллега» и «коллеги» не плодят дубль)."""
    a, b = (l1 or "").strip().lower(), (l2 or "").strip().lower()
    if not a or not b:
        return True  # пустая подпись — про ту же связь пары
    if a == b or a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _find_edge(a: str, b: str, label: str) -> dict | None:
    key = {a, b}
    for e in edges():
        if {e["a"], e["b"]} == key and _labels_close(e["label"], label):
            return e
    return None


_DATE_MARK = re.compile(r"_\(\d{4}-\d{2}-\d{2}\)_")


def _edge_files_for(e: dict) -> list[Path]:
    if e["src"].startswith("people/"):
        return [people.path_for(e["src"].split("/", 1)[1])]
    return [GRAPH_MD]


def _touch_edge_date(e: dict) -> bool:
    """Обновить дату существующего ребра (дедуп не плодит дубль, но след живой)."""
    for path in _edge_files_for(e):
        text = _read(path)
        out, changed = [], False
        for line in text.splitlines():
            if not changed and f"[[{e['b']}]]" in line and (e["src"] == "graph") == (f"[[{e['a']}]]" in line):
                if _DATE_MARK.search(line):
                    line = _DATE_MARK.sub(f"_({_today()})_", line, count=1)
                else:
                    line = line.rstrip() + f" _({_today()})_"
                changed = True
            out.append(line)
        if changed:
            path.write_text("\n".join(out) + "\n", encoding="utf-8")
            return True
    return False


# --------------------------------------------------------------------------- #
#  Запись рёбер (в маркдаун — источник правды)
# --------------------------------------------------------------------------- #

def add_edge(a_ref: str, b_ref: str, label: str = "", source: str = "") -> str:
    """Связать два узла. Ребро пишется человеку в ## Связи (если один из концов —
    человек с файлом), иначе — строкой в memory/graph.md. Дедуп по паре + БЛИЗКОЙ
    подписи (PASS 8.6): дубль не плодится, дата существующего ребра обновляется.
    source — метка происхождения («сон» у извлечённых консолидацией)."""
    a, b = resolve(a_ref), resolve(b_ref)
    if not a or not b:
        return "Не поняла, что связывать — нужны оба узла."
    if a == b:
        return "Узел сам с собой не связываю."
    label = (label or "").strip()
    if (source or "").strip():
        label = f"{label} ({source.strip()})" if label else f"({source.strip()})"
    exist = _find_edge(a, b, label)
    if exist is not None:
        _touch_edge_date(exist)
        return f"Связь {display(a)} — {display(b)} уже есть (обновила дату)."
    # конец-человек предпочтительнее: ребро видно прямо в его файле
    if people.path_for(a).exists():
        people.add_link(a, str(a_ref), b, label)
    elif people.path_for(b).exists():
        people.add_link(b, str(b_ref), a, label)
    else:
        MEM_DIR.mkdir(parents=True, exist_ok=True)
        text = _read(GRAPH_MD)
        if not text.strip():
            text = _GRAPH_HEADER
        line = f"- [[{a}]] ↔ [[{b}]]" + (f" — {label}" if label else "") + f" _({_today()})_"
        GRAPH_MD.write_text(text.rstrip() + "\n" + line + "\n", encoding="utf-8")
    lab = f" ({label})" if label else ""
    return f"Связала: {display(a)} — {display(b)}{lab}."


def forget_connection(a_ref: str, b_ref: str) -> str:
    """Забыть связь A—B (PASS 8.6, owner): удаляет строки ребра и из `## Связи`
    обоих людей, и из memory/graph.md. -> честный отчёт."""
    a, b = resolve(a_ref), resolve(b_ref)
    if not a or not b or a == b:
        return "Нужны два разных узла."
    removed = 0
    for slug, other in ((a, b), (b, a)):
        p = people.path_for(slug)
        if not p.exists():
            continue
        nm, body = people.read(slug)
        cur = body.get(people.LINKS, "")
        keep = [l for l in cur.splitlines() if f"[[{other}]]" not in l]
        if len(keep) != len(cur.splitlines()):
            removed += len(cur.splitlines()) - len(keep)
            body[people.LINKS] = "\n".join(keep).strip()
            people.write(slug, nm, body)
    if GRAPH_MD.exists():
        lines = _read(GRAPH_MD).splitlines()
        keep = [l for l in lines if not (f"[[{a}]]" in l and f"[[{b}]]" in l)]
        if len(keep) != len(lines):
            removed += len(lines) - len(keep)
            GRAPH_MD.write_text("\n".join(keep).rstrip() + "\n", encoding="utf-8")
    if not removed:
        return f"Связи {display(a)} — {display(b)} и так нет."
    return f"Забыла связь {display(a)} — {display(b)} (строк удалено: {removed})."


# --------------------------------------------------------------------------- #
#  Обход (BFS, до MAX_DEPTH)
# --------------------------------------------------------------------------- #

def _bfs(start: str, depth: int) -> dict[str, tuple[str | None, int]]:
    """{узел: (родитель, дистанция)} в пределах depth шагов от start."""
    adj = adjacency()
    seen: dict[str, tuple[str | None, int]] = {start: (None, 0)}
    frontier = [start]
    for d in range(1, depth + 1):
        nxt: list[str] = []
        for node in frontier:
            for nb in adj.get(node, {}):
                if nb not in seen:
                    seen[nb] = (node, d)
                    nxt.append(nb)
        frontier = nxt
    return seen


def _label_between(adj: dict, a: str, b: str) -> str:
    labels = adj.get(a, {}).get(b) or []
    return labels[0] if labels else "связаны"


def _chain_text(chain: list[str], adj: dict) -> str:
    """[a, b, c] -> 'A —(как)— B —(как)— C' (имена — человекочитаемые)."""
    parts = [display(chain[0])]
    for prev, cur in zip(chain, chain[1:]):
        parts.append(f"—({_label_between(adj, prev, cur)})— {display(cur)}")
    return " ".join(parts)


def neighbors_text(ref: str, depth: int = 1) -> str:
    """Взгляд на узел: с кем связан (1 шаг — рёбра; глубже — цепочки). Для тула/промпта."""
    start = resolve(ref)
    depth = max(1, min(int(depth or 1), MAX_DEPTH))
    adj = adjacency()
    if start not in adj:
        return f"У «{display(start)}» пока нет записанных связей."
    seen = _bfs(start, depth)
    by_dist: dict[int, list[str]] = {}
    for node, (_, d) in seen.items():
        if d > 0:
            by_dist.setdefault(d, []).append(node)
    lines = [f"{display(start)}:"]
    shown = 0
    for d in sorted(by_dist):
        for node in sorted(by_dist[d]):
            if shown >= MAX_NODES_OUT:
                lines.append("  … (дальше не разворачиваю — спроси точечно)")
                return "\n".join(lines)
            chain = [node]
            while chain[-1] != start:
                chain.append(seen[chain[-1]][0])  # к родителю
            lines.append("  " + _chain_text(chain, adj))
            shown += 1
    return "\n".join(lines)


def path_text(a_ref: str, b_ref: str) -> str:
    """Как связаны A и B: кратчайшая цепочка или честное «не вижу связи»."""
    a, b = resolve(a_ref), resolve(b_ref)
    if a == b:
        return f"{display(a)} — это один и тот же узел."
    seen = _bfs(a, MAX_DEPTH)
    if b not in seen:
        return f"Связи между {display(a)} и {display(b)} не вижу (в пределах {MAX_DEPTH} шагов)."
    chain = [b]
    while chain[-1] != a:
        chain.append(seen[chain[-1]][0])
    return _chain_text(list(reversed(chain)), adjacency())


def related_lines(slugs: list[str], cap: int = 4) -> list[str]:
    """Рёбра первого шага вокруг данных узлов — короткие строки для recall-обогащения."""
    adj = adjacency()
    out: list[str] = []
    seen_pairs: set[frozenset] = set()
    for s in slugs:
        s = resolve(s)
        for nb in sorted(adj.get(s, {})):
            pair = frozenset((s, nb))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            out.append(f"{display(s)} —({_label_between(adj, s, nb)})— {display(nb)}")
            if len(out) >= cap:
                return out
    return out


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "edges":
        for e in edges():
            print(f"{e['a']} — {e['b']}" + (f" ({e['label']})" if e["label"] else "") + f"  [{e['src']}]")
    elif cmd == "around" and len(sys.argv) > 2:
        print(neighbors_text(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 1))
    elif cmd == "path" and len(sys.argv) > 3:
        print(path_text(sys.argv[2], sys.argv[3]))
    else:
        print("usage: python graph.py [edges | around <узел> [глубина] | path <a> <b>]")
