#!/usr/bin/env bash
# Isolate Chrome's native session restore from OmaSession's replay and
# workspace placement. This is intentionally interactive: it never kills
# Chrome and waits for the user to inspect the result before finishing.
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/bin

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OMASESSION="$ROOT/bin/omasession"
REPLAY="$ROOT/lib/replay.py"
WAIT_SECONDS="${1:-10}"

die() {
    printf 'chrome-native-restart: %s\n' "$*" >&2
    exit 1
}

[[ -t 0 && -t 1 ]] || die "execute from an interactive terminal"
[[ "$WAIT_SECONDS" =~ ^[0-9]+$ ]] || die "wait must be an integer number of seconds"
command -v hyprctl >/dev/null || die "hyprctl not found"
command -v google-chrome-stable >/dev/null || die "google-chrome-stable not found"
if systemctl --user is-active --quiet omasession-snapshot.timer 2>/dev/null; then
    die "omasession-snapshot.timer is active; stop it before this isolated test"
fi

REPORT_DIR=$(mktemp -d -p /tmp omasession-chrome-native.XXXXXX)
printf 'Relatório: %s\n' "$REPORT_DIR"

printf '\nChrome instalado: '
google-chrome-stable --version || true
printf 'Processos Chrome antes do teste: '
pgrep -c -x chrome 2>/dev/null || true

read -r -p 'Deixe as abas desejadas abertas e pressione Enter para salvar o snapshot: ' _
"$OMASESSION" save 2>&1 | tee "$REPORT_DIR/save.log"
hyprctl clients -j >"$REPORT_DIR/clients-before.json"

cat <<'EOF'

Agora feche TODAS as janelas do Chrome normalmente.
Não use Ctrl+Shift+T depois do fechamento: isso alteraria o resultado do teste.
EOF

for _ in {1..60}; do
    if ! pgrep -x chrome >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
pgrep -x chrome >/dev/null 2>&1 && die "Chrome ainda está em execução; nenhum processo foi encerrado"

printf '\nPreparando o perfil para a restauração nativa...\n'
REPLAY="$REPLAY" /usr/bin/python3 - <<'PY'
import os
import sys

sys.path.insert(0, os.path.dirname(os.environ["REPLAY"]))
import replay  # noqa: E402

if not replay.arm_browser_profile("google-chrome"):
    raise SystemExit("não foi possível preparar o perfil do Chrome")
print("perfil preparado")
PY

printf '\nIniciando somente o Chrome, sem replay do OmaSession...\n'
if command -v uwsm >/dev/null 2>&1; then
    nohup uwsm app -- google-chrome-stable \
        --restore-last-session --no-first-run --no-default-browser-check \
        >"$REPORT_DIR/chrome-launch.log" 2>&1 &
else
    nohup google-chrome-stable \
        --restore-last-session --no-first-run --no-default-browser-check \
        >"$REPORT_DIR/chrome-launch.log" 2>&1 &
fi

printf 'Aguardando %s segundos para o Chrome reabrir a sessão...\n' "$WAIT_SECONDS"
sleep "$WAIT_SECONDS"
hyprctl clients -j >"$REPORT_DIR/clients-after.json"

printf '\nJanelas Chrome observadas pelo Hyprland:\n'
/usr/bin/python3 - "$REPORT_DIR/clients-after.json" <<'PY'
import json
import sys

clients = json.loads(open(sys.argv[1], encoding="utf-8").read())
chrome = [c for c in clients if c.get("class") == "google-chrome"]
print(f"{len(chrome)} janela(s)")
for client in chrome:
    workspace = client.get("workspace", {}).get("id")
    print(f"  ws{workspace}: {client.get('title', '')}")
PY

printf '\nInspecione agora as abas visualmente. Não use Ctrl+Shift+T.\n'
read -r -p 'Quando terminar de observar, pressione Enter para encerrar o diagnóstico: ' _
printf '\nArquivos preservados em: %s\n' "$REPORT_DIR"
printf 'Este teste não executou restore do OmaSession e não reposicionou janelas.\n'
