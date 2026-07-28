#!/usr/bin/env bash
# install.sh — одноразовый bootstrap praxis-serverd (root, на хосте).
# Идемпотентен. Демон живёт в /opt/praxis-serverd (root, ВНЕ её репо /app). См. §5.
set -euo pipefail

HOME_DIR=/opt/praxis-serverd
LIB="$HOME_DIR/lib"
STATE="$HOME_DIR/state"
RUN="$HOME_DIR/run"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "== praxis-serverd install =="
[ "$(id -u)" = 0 ] || { echo "нужен root"; exit 1; }

# группа для сокета (второй фактор поверх cgroup-пина)
getent group praxis >/dev/null || groupadd praxis
echo "группа praxis: ok"

mkdir -p "$LIB" "$STATE/operations" "$STATE/requests" "$STATE/recovery" \
  "$STATE/audit-exports" "$RUN" "$STATE/backups"
chmod 0755 "$HOME_DIR"

# код демона — пришпиленная копия
for f in advisor.py auditlog.py brokerops.py hostproc.py hostrecovery.py hostverbs.py broker.py migrate_v1_tasks.py; do
  install -m 0644 "$SRC/$f" "$LIB/$f"
done
install -m 0644 "$SRC/../forge_intelligence.py" "$LIB/forge_intelligence.py"
rm -f "$LIB/hostforge.py" "$LIB/hostworker.py" "$LIB/serverd_llm.py" "$LIB/serverd.py"
# v1 duplicated the model configuration into the root daemon. V2 has no model and keeps no copy.
rm -f "$HOME_DIR/secret/llm.json"
chmod 0644 "$LIB/broker.py"
echo "lib: $(ls "$LIB" | tr '\n' ' ')"

# токен (второй фактор). 0640 root:praxis — контейнеру praxis читаем, чужому host-юзеру нет.
if [ ! -s "$RUN/token" ]; then
  head -c 32 /dev/urandom | base64 | tr -d '\n' > "$RUN/token"
fi
chown root:praxis "$RUN/token"; chmod 0640 "$RUN/token"
chown root:praxis "$RUN"; chmod 0750 "$RUN"
echo "токен: ok"

# v1 host task state остаётся только для rolling-read compatibility. Broker v2 новых задач здесь
# не создаёт: единственный task store теперь memory/.forge внутри Praxis.
[ -d "$STATE/tasks" ] && echo "legacy host tasks: $(find "$STATE/tasks" -mindepth 1 -maxdepth 1 -type d | wc -l) (read-only compatibility)" || true
python3 "$LIB/migrate_v1_tasks.py" --legacy "$STATE/tasks" \
  --forge "${PRAXIS_FORGE_STATE:-/opt/praxis/memory/.forge}"

# systemd unit
install -m 0644 "$SRC/praxis-serverd.service" /etc/systemd/system/praxis-serverd.service
# admin CLI
install -m 0755 "$SRC/serverdctl" /usr/local/bin/serverdctl

systemctl daemon-reload
systemctl enable praxis-serverd.service >/dev/null 2>&1 || true
systemctl restart praxis-serverd.service
sleep 1
systemctl --no-pager --lines=5 status praxis-serverd.service || true
echo "== готово: сокет $RUN/serverd.sock =="
echo "Дальше: bind-mount $RUN → /run/praxis-serverd в docker-compose (только praxis) + recreate."
