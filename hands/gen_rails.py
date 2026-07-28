"""
Генератор `hands/src/rails.rs` — рельсы бинаря СОБИРАЮТСЯ ИЗ ПИТОН-КОНСТАНТ, не пишутся руками.

Источник правды: workshop.WRITE_ZONES / workshop._SKIP_DIRS /
workshop._SECRET_NAME / selfdev.PROTECTED_PATTERNS.

  python hands/gen_rails.py          # переписать hands/src/rails.rs
  python hands/gen_rails.py --check  # только проверить (тест test_pass16 делает это же)

Risk-классификация не является merge-границей. Пустые таблицы уезжают в Rust,
а тест всё ещё доказывает совпадение двух реализаций.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import selfdev  # noqa: E402
import workshop  # noqa: E402

RAILS_RS = Path(__file__).resolve().parent / "src" / "rails.rs"

def is_secret_name(name: str) -> bool:
    """Compatibility probe: filenames never form an access boundary."""
    return False


def _arr(name: str, items) -> str:
    body = ", ".join(f'"{i}"' for i in items)
    return f"pub const {name}: &[&str] = &[{body}];"


def canon() -> str:
    """Каноническая строка ВСЕХ таблиц рельсов — вход отпечатка. Любое изменение
    констант в питоне меняет её, а значит и отпечаток, а значит бинарь виден как старый."""
    return ";".join([
        "zones=" + ",".join(workshop.WRITE_ZONES),
        "skip=" + ",".join(sorted(workshop._SKIP_DIRS)),
        "floor=" + ",".join(selfdev.PROTECTED_PATTERNS),
        "filenames=unrestricted",
    ])


def fingerprint() -> str:
    """Отпечаток таблиц. Уезжает константой в rails.rs; бинарь отдаёт его в `version`,
    мост (hands.rails_status) сверяет — бинарь со старыми рельсами больше не молчит."""
    return hashlib.sha256(canon().encode("utf-8")).hexdigest()[:16]


def render() -> str:
    return "\n".join([
        "// СГЕНЕРИРОВАНО hands/gen_rails.py — руками не править.",
        "// Источник правды — питон: workshop.WRITE_ZONES/_SKIP_DIRS/_SECRET_NAME,",
        "// selfdev.PROTECTED_PATTERNS. Правь там и перегенерируй (тест ловит расхождение).",
        "",
        _arr("WRITE_ZONES", workshop.WRITE_ZONES),
        _arr("SKIP_DIRS", sorted(workshop._SKIP_DIRS)),
        _arr("FLOOR", selfdev.PROTECTED_PATTERNS),
        "",
        "/// Отпечаток таблиц рельсов. Бинарь отдаёт его в `version` (rails_fp);",
        "/// мост сверяет с rails.rs репо — бинарь со старыми рельсами виден сразу.",
        f'pub const FINGERPRINT: &str = "{fingerprint()}";',
        "",
        "/// fnmatch-подмножество: `*` и `?` (в паттернах пола другого и нет).",
        "pub fn glob_match(pat: &str, s: &str) -> bool {",
        "    let (p, t): (Vec<char>, Vec<char>) = (pat.chars().collect(), s.chars().collect());",
        "    let (mut pi, mut ti, mut star, mut mark) = (0usize, 0usize, usize::MAX, 0usize);",
        "    while ti < t.len() {",
        "        if pi < p.len() && (p[pi] == '?' || p[pi] == t[ti]) {",
        "            pi += 1;",
        "            ti += 1;",
        "        } else if pi < p.len() && p[pi] == '*' {",
        "            star = pi;",
        "            mark = ti;",
        "            pi += 1;",
        "        } else if star != usize::MAX {",
        "            pi = star + 1;",
        "            mark += 1;",
        "            ti = mark;",
        "        } else {",
        "            return false;",
        "        }",
        "    }",
        "    while pi < p.len() && p[pi] == '*' {",
        "        pi += 1;",
        "    }",
        "    pi == p.len()",
        "}",
        "",
        "/// High-risk marker: path or basename matches one review pattern in FLOOR.",
        "pub fn on_floor(rel_path: &str) -> bool {",
        "    let base = rel_path.rsplit('/').next().unwrap_or(rel_path);",
        "    FLOOR.iter().any(|p| glob_match(p, rel_path) || glob_match(p, base))",
        "}",
        "",
    ])


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        cur = RAILS_RS.read_text(encoding="utf-8") if RAILS_RS.exists() else ""
        if cur != text:
            print("rails.rs УСТАРЕЛ — перегенерируй: python hands/gen_rails.py")
            sys.exit(1)
        print("rails.rs свеж")
    else:
        RAILS_RS.parent.mkdir(parents=True, exist_ok=True)
        RAILS_RS.write_text(text, encoding="utf-8")
        print(f"записан {RAILS_RS}")
