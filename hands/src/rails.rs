// СГЕНЕРИРОВАНО hands/gen_rails.py — руками не править.
// Источник правды — питон: workshop.WRITE_ZONES/_SKIP_DIRS/_SECRET_NAME,
// selfdev.PROTECTED_PATTERNS. Правь там и перегенерируй (тест ловит расхождение).

pub const WRITE_ZONES: &[&str] = &["workspace", "soul", "memory"];
pub const SKIP_DIRS: &[&str] = &[".git", ".proposals", ".vectors", ".venv", "__pycache__", "node_modules"];
pub const FLOOR: &[&str] = &["bootguard.py", "selfgit.py", "selfdev.py", "Dockerfile", "docker-compose*", "services.py", "hostops.py", "hostagent.py"];

/// Отпечаток таблиц рельсов. Бинарь отдаёт его в `version` (rails_fp);
/// мост сверяет с rails.rs репо — бинарь со старыми рельсами виден сразу.
pub const FINGERPRINT: &str = "52126387d6d8700a";

/// fnmatch-подмножество: `*` и `?` (в паттернах пола другого и нет).
pub fn glob_match(pat: &str, s: &str) -> bool {
    let (p, t): (Vec<char>, Vec<char>) = (pat.chars().collect(), s.chars().collect());
    let (mut pi, mut ti, mut star, mut mark) = (0usize, 0usize, usize::MAX, 0usize);
    while ti < t.len() {
        if pi < p.len() && (p[pi] == '?' || p[pi] == t[ti]) {
            pi += 1;
            ti += 1;
        } else if pi < p.len() && p[pi] == '*' {
            star = pi;
            mark = ti;
            pi += 1;
        } else if star != usize::MAX {
            pi = star + 1;
            mark += 1;
            ti = mark;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' {
        pi += 1;
    }
    pi == p.len()
}

/// High-risk marker: path or basename matches one review pattern in FLOOR.
pub fn on_floor(rel_path: &str) -> bool {
    let base = rel_path.rsplit('/').next().unwrap_or(rel_path);
    FLOOR.iter().any(|p| glob_match(p, rel_path) || glob_match(p, base))
}
