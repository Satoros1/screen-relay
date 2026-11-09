#!/usr/bin/env bash
# Screen Relay - установка/обновление сервера одной командой.
#   cd screen-relay && ./deploy/setup.sh
# Повторный запуск безопасен: токены из .env сохраняются, сервер пересобирается.
# Можно без вопросов: SR_DOMAIN=live.site.com SR_BOT_TOKEN=... ./deploy/setup.sh
#   (SR_DOMAIN, SR_BOT_TOKEN, SR_ALLOWED_IDS, SR_GH_TOKEN, SR_PORT)
set -euo pipefail

# own names, so tokens of other bots in your shell environment are never picked up
DOMAIN=${SR_DOMAIN:-}; BOT_TOKEN=${SR_BOT_TOKEN:-}; ALLOWED_IDS=${SR_ALLOWED_IDS:-}
GH_TOKEN=${SR_GH_TOKEN:-}; RELAY_PORT=${SR_PORT:-}; HTTPS_TRIES=${SR_HTTPS_TRIES:-12}

cd "$(dirname "$0")/.."
ROOT=$(pwd)
ENV_FILE="$ROOT/.env"

c_ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
c_err()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; }
SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null && SUDO="sudo"

get_env() { [ -f "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }
gen_token() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n' | cut -c1-40; }
ask() {   # ask VAR "question" "default"
  local var=$1 q=$2 def=${3:-} cur=${!1:-}
  if [ -n "$cur" ]; then return; fi
  if [ -t 0 ]; then
    read -r -p "$q${def:+ [$def]}: " cur || true
  fi
  printf -v "$var" '%s' "${cur:-$def}"
}

command -v docker >/dev/null || { c_err "Не найден docker. Поставь: curl -fsSL https://get.docker.com | sh"; exit 1; }
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="$SUDO docker"
$DOCKER compose version >/dev/null 2>&1 || { c_err "Нужен docker compose v2"; exit 1; }

echo "== Screen Relay: настройка сервера =="
ask DOMAIN       "Домен для трансляции (A-запись должна смотреть на этот сервер), например live.mysite.com" "$(get_env PUBLIC_URL | sed 's#https://##')"
[ -z "$DOMAIN" ] && { c_err "Домен обязателен"; exit 1; }
DOMAIN=${DOMAIN#https://}; DOMAIN=${DOMAIN%/}
ask BOT_TOKEN    "Токен бота для Mini App и уведомлений (лучше отдельный бот; Enter - без Telegram)" "$(get_env BOT_TOKEN)"
ask ALLOWED_IDS  "Твой Telegram ID (и других, кому можно смотреть, через запятую; узнать - @userinfobot)" "$(get_env ALLOWED_IDS || true)"
ALLOWED_IDS=$(printf '%s' "$ALLOWED_IDS" | tr -d ' ')
if [ -n "$BOT_TOKEN" ] && ! printf '%s' "$ALLOWED_IDS" | grep -qE '^[0-9]+(,[0-9]+)*$'; then
  c_err "Для Telegram нужен хотя бы один числовой Telegram ID (узнать у @userinfobot)"; exit 1
fi
ask GH_TOKEN     "GitHub-токен для обновлений программы (Enter - пропустить, если репозиторий публичный)" "$(get_env GH_TOKEN)"

PUSH_TOKEN=$(get_env PUSH_TOKEN); [ ${#PUSH_TOKEN} -lt 16 ] && PUSH_TOKEN=$(gen_token)
VIEW_KEY=$(get_env VIEW_KEY);     [ -z "$VIEW_KEY" ] && VIEW_KEY=$(gen_token | cut -c1-16)
[ -z "$RELAY_PORT" ] && RELAY_PORT=$(get_env RELAY_PORT)
if [ -z "$RELAY_PORT" ]; then      # first free local port from 8090
  for p in $(seq 8090 8120); do
    if ! (ss -ltnH 2>/dev/null || netstat -ltn 2>/dev/null) | awk '{print $4}' | grep -qE "[:.]$p\$"; then RELAY_PORT=$p; break; fi
  done
fi
UPSTREAM_HOST="127.0.0.1:$RELAY_PORT"

umask 077
cat > "$ENV_FILE" <<EOF
PUSH_TOKEN=$PUSH_TOKEN
VIEW_KEY=$VIEW_KEY
BOT_TOKEN=$BOT_TOKEN
ALLOWED_IDS=$ALLOWED_IDS
PUBLIC_URL=https://$DOMAIN
GH_TOKEN=$GH_TOKEN
RELAY_PORT=$RELAY_PORT
EOF
c_ok ".env записан (права 600)"

# DNS check (warning only)
MY_IP=$(curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || true)
DNS_IP=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)
if [ -z "$DNS_IP" ]; then
  c_warn "$DOMAIN пока не резолвится. Создай A-запись на ${MY_IP:-IP этого сервера}, HTTPS заработает после этого."
elif [ -n "$MY_IP" ] && [ "$DNS_IP" != "$MY_IP" ]; then
  c_warn "$DOMAIN указывает на $DNS_IP, а этот сервер $MY_IP (ок, если это Cloudflare-прокси)."
else
  c_ok "DNS: $DOMAIN → $DNS_IP"
fi

# the site must survive VPS reboots: docker starts on boot, container has restart: unless-stopped
if command -v systemctl >/dev/null && ! systemctl is-enabled --quiet docker 2>/dev/null; then
  $SUDO systemctl enable docker >/dev/null 2>&1 && c_ok "Docker включён в автозапуск" || true
fi

echo "== Сборка и запуск контейнера =="
$DOCKER compose up -d --build
for i in $(seq 1 20); do
  curl -fsS "http://$UPSTREAM_HOST/health" >/dev/null 2>&1 && break; sleep 1
done
curl -fsS "http://$UPSTREAM_HOST/health" >/dev/null 2>&1 && c_ok "Сервер работает локально (порт $RELAY_PORT)" \
  || { c_err "Сервер не отвечает. Логи: docker compose logs relay"; exit 1; }

echo "== Caddy =="
add_block() {   # add_block <Caddyfile> <upstream>
  local file=$1 upstream=$2
  if $SUDO grep -qE "^[[:space:]]*$DOMAIN[[:space:]]*\{" "$file"; then
    c_ok "Блок для $DOMAIN уже есть в $file"; return 1
  fi
  $SUDO cp "$file" "$file.bak.screen-relay"
  printf '\n%s {\n\tencode gzip\n\treverse_proxy %s\n}\n' "$DOMAIN" "$upstream" | $SUDO tee -a "$file" >/dev/null
  c_ok "Добавил $DOMAIN в $file (копия: $file.bak.screen-relay)"
}
CADDY_DONE=""
SUGGEST_UPSTREAM="$UPSTREAM_HOST"
CADDY_CT=$($DOCKER ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /caddy/ {print $1; exit}')
if command -v caddy >/dev/null && [ -f /etc/caddy/Caddyfile ] && systemctl is-active --quiet caddy 2>/dev/null; then
  if add_block /etc/caddy/Caddyfile "$UPSTREAM_HOST"; then
    if $SUDO caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
      $SUDO systemctl reload caddy && c_ok "Caddy перезагружен"
    else
      $SUDO cp /etc/caddy/Caddyfile.bak.screen-relay /etc/caddy/Caddyfile
      c_err "Caddyfile не прошёл проверку, вернул как было"; exit 1
    fi
  fi
  CADDY_DONE=1
elif [ -n "$CADDY_CT" ]; then
  SUGGEST_UPSTREAM="screen-relay:8000"
  NET=$($DOCKER inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$CADDY_CT" | awk '{print $1}')
  $DOCKER network connect "$NET" screen-relay 2>/dev/null || true
  # keep that network after the container is recreated (override file is not in git)
  cat > docker-compose.override.yml <<EOF
services:
  relay:
    networks: [default, caddy]
networks:
  caddy:
    external: true
    name: $NET
EOF
  c_ok "Caddy в контейнере $CADDY_CT, подключил screen-relay к сети $NET"
  SRC=$($DOCKER inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' "$CADDY_CT")
  [ -z "$SRC" ] && SRC=$($DOCKER inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy"}}{{.Source}}/Caddyfile{{end}}{{end}}' "$CADDY_CT")
  if [ -n "$SRC" ] && $SUDO test -f "$SRC"; then
    if add_block "$SRC" "screen-relay:8000"; then
      if $DOCKER exec "$CADDY_CT" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
        c_ok "Caddy перезагружен"
      else
        $SUDO cp "$SRC.bak.screen-relay" "$SRC"
        c_err "Caddy не принял конфиг, вернул как было"; exit 1
      fi
    fi
    CADDY_DONE=1
  fi
fi
if [ -z "$CADDY_DONE" ]; then
  c_warn "Caddy не найден автоматически. Добавь в свой Caddyfile и перезагрузи его:"
  printf '\n  %s {\n      reverse_proxy %s\n  }\n\n' "$DOMAIN" "$SUGGEST_UPSTREAM"
fi

echo "== Проверка HTTPS =="
OK=""
for i in $(seq 1 "$HTTPS_TRIES"); do
  curl -fsS --max-time 5 "https://$DOMAIN/health" >/dev/null 2>&1 && { OK=1; break; }; sleep 5
done
[ -n "$OK" ] && c_ok "https://$DOMAIN работает" || c_warn "https://$DOMAIN пока не отвечает (DNS/сертификат могут занять пару минут)"

CODE_JSON=$(printf '{"s":"wss://%s/ingest","t":"%s"}' "$DOMAIN" "$PUSH_TOKEN")
CODE="sr1:$(printf '%s' "$CODE_JSON" | base64 | tr -d '\n' | tr '+/' '-_' | tr -d '=')"

cat <<EOF

================================================================
 Готово.

 Смотреть:   https://$DOMAIN/
             (в браузере спросит ключ: $VIEW_KEY)
EOF
[ -n "$BOT_TOKEN" ] && echo " Telegram:   открой бота → кнопка «📺 Live» слева от поля ввода"
cat <<EOF

 Код подключения для программы на ПК (скопируй целиком и вставь
 в поле «Адрес» в Screen Relay - адрес и токен заполнятся сами):

$CODE

================================================================
EOF
