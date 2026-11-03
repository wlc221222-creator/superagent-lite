#!/usr/bin/env bash
# Restore the superagent-lite deployment on THIS cloud VM after a platform
# replacement wiped everything outside /home/hatch (/opt, /etc, systemd).
# Idempotent. The HTTP token is kept in ~/dl/superagent-token.txt (persisted
# home) so it survives wipes and Ward's clients keep working.
set -euo pipefail

PROJECT="$HOME/workspace/projects/superagent-lite"
TOKEN_FILE="$HOME/dl/superagent-token.txt"
mkdir -p "$HOME/dl"

if [ ! -f "$TOKEN_FILE" ]; then
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi
TOKEN="$(cat "$TOKEN_FILE")"

cd "$PROJECT"
./deploy/install.sh

printf 'HOME=%s\nSUPERAGENT_HTTP_TOKEN=%s\n' "$HOME" "$TOKEN" > /etc/superagent/superagent-http.env
printf 'HOME=%s\n' "$HOME" > /etc/superagent/superagentd.env
for v in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy NO_PROXY no_proxy; do
  val="$(printenv "$v" || true)"
  if [ -n "${val:-}" ]; then
    echo "$v=$val" >> /etc/superagent/superagentd.env
    echo "$v=$val" >> /etc/superagent/superagent-http.env
  fi
done
chmod 600 /etc/superagent/*.env

python3 - <<'EOF'
import json
p = '/opt/superagent-lite/config.json'
cfg = json.load(open(p))
cfg['review_mode'] = 'shadow'
cfg['review_model'] = 'cn:kimi-k3.1-pro'
json.dump(cfg, open(p, 'w'), indent=1)
print('review:', cfg['review_mode'], cfg['review_model'])
EOF

systemctl restart superagentd superagent-http
sleep 2
systemctl is-active superagentd superagent-http
curl -s http://127.0.0.1:8765/health && echo
echo "restore OK"
