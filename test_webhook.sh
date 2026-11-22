#!/usr/bin/env bash
# Prueba local del webhook en modo dry-run (no envía nada a Bitunix).
set -u
cd "$(dirname "$0")"
export DRY_RUN=true DRY_RUN_EQUITY=2000 PROXY_PORT=18080 WEBHOOK_TOKEN=secret123
export STATE_FILE=/tmp/opencode/pstate.json LOG_FILE=/tmp/opencode/proxy.log
rm -f /tmp/opencode/pstate.json

python3 proxy.py serve > /tmp/opencode/proxy_stdout.txt 2>&1 &
SERVER=$!
sleep 3

echo "--- GET /health"
curl -s localhost:18080/health; echo

echo "--- POST /webhook/<token> (señal de ejemplo)"
curl -s -X POST --data-binary @signals/sample.txt localhost:18080/webhook/secret123; echo

echo "--- POST /webhook sin token (espera 401)"
curl -s -o /dev/null -w "%{http_code}\n" -X POST --data-binary @signals/sample.txt localhost:18080/webhook

echo "--- POST /webhook con header (misma señal, espera 'duplicada')"
curl -s -X POST -H "X-Webhook-Token: secret123" --data-binary @signals/sample.txt localhost:18080/webhook; echo

echo "--- POST con basura (espera error de parsing)"
curl -s -X POST -H "X-Webhook-Token: secret123" -d 'hola sin señal' localhost:18080/webhook; echo

echo "--- POST JSON estructurado"
curl -s -X POST -H "Content-Type: application/json" -H "X-Webhook-Token: secret123" \
  -d '{"symbol":"ETHUSDT","side":"long","entry":1700,"sl":1650,"tps":[1750,1800],"leverage":10}' \
  localhost:18080/webhook; echo

echo "--- GET /status (extracto)"
curl -s -H "X-Webhook-Token: secret123" localhost:18080/status | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({'ok':d['ok'],'error':d['error'],'config':d['config'],'managed':[{'symbol':m['symbol'],'status':m['status']} for m in d['managed']],'recent':[{k:r[k] for k in ('symbol','status','note')} for r in d['recent']]}, indent=2, ensure_ascii=False))"

echo "--- GET /status SIN token (espera 401)"
curl -s -o /dev/null -w "%{http_code}\n" localhost:18080/status

echo "--- GET /health (siempre publico)"
curl -s -o /dev/null -w "%{http_code}\n" localhost:18080/health

echo "--- GET / (panel)"
curl -s localhost:18080/ | grep -c "byline\|composer" | sed 's/^/lineas del panel: /'

echo "--- GET /events?after=0"
curl -s -H "X-Webhook-Token: secret123" "localhost:18080/events?after=0" | python3 -c "import json,sys; d=json.load(sys.stdin); print('eventos:', len(d['events']), 'cursor:', d['cursor'])"

echo "--- POST /api/cancel inexistente (espera 404)"
curl -s -X POST -H "X-Webhook-Token: secret123" -H "Content-Type: application/json" \
  -d '{"id":"no-existe"}' localhost:18080/api/cancel; echo

kill $SERVER 2>/dev/null
wait $SERVER 2>/dev/null
echo "--- stdout del servidor"
cat /tmp/opencode/proxy_stdout.txt
