#!/usr/bin/env bash
# Instalador del proxy en un VPS Debian/Ubuntu.
#   sudo bash install.sh            # instala en /opt/proxy + systemd
#   sudo bash install.sh /otra/ruta
set -euo pipefail

DEST="${1:-/opt/proxy}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${SERVICE_USER:-trader}"

echo "== copiando $SRC -> $DEST"
mkdir -p "$DEST"
cp -a "$SRC"/proxy.py "$SRC"/paper.py "$SRC"/classify.py "$SRC"/listener.py "$SRC"/proxy \n      "$SRC"/requirements.txt "$SRC"/proxy.service "$SRC"/listener.service "$SRC"/signals "$DEST"/
[ -f "$SRC/.env" ] && cp -a "$SRC/.env" "$DEST"/.env
[ -f "$SRC/.env.example" ] && cp -a "$SRC/.env.example" "$DEST"/.env.example
chmod 600 "$DEST/.env" 2>/dev/null || true
chmod +x "$DEST/proxy"

if [ ! -f "$DEST/.env" ]; then
    cp "$DEST/.env.example" "$DEST/.env"
    echo "!! created $DEST/.env — edita BITUNIX_API_KEY, BITUNIX_API_SECRET y WEBHOOK_TOKEN"
fi

echo "== python + dependencias"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null
if [ ! -x "$DEST/.venv/bin/python" ]; then
    python3 -m venv "$DEST/.venv"
fi
"$DEST/.venv/bin/pip" install -q --upgrade pip
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "== creando usuario $SERVICE_USER"
    useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$DEST"

echo "== instalando servicio systemd"
install -m 644 "$DEST/proxy.service" /etc/systemd/system/proxy.service
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=$DEST#; s#^ExecStart=.*#ExecStart=$DEST/proxy serve#; s#^User=.*#User=$SERVICE_USER#; s#^Group=.*#Group=$SERVICE_USER#" /etc/systemd/system/proxy.service
install -m 644 "$DEST/listener.service" /etc/systemd/system/listener.service
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=$DEST#; s#^ExecStart=.*#ExecStart=$DEST/.venv/bin/python $DEST/listener.py#; s#^User=.*#User=$SERVICE_USER#; s#^Group=.*#Group=$SERVICE_USER#" /etc/systemd/system/listener.service
systemctl daemon-reload
systemctl enable proxy.service
# el listener se habilita a mano tras `listener.py login` (necesita la sesión de Telegram)

cat <<EOF

Instalado. Siguientes pasos:

  1. Editar la configuración:   nano $DEST/.env
     - BITUNIX_API_KEY / BITUNIX_API_SECRET
     - WEBHOOK_TOKEN (valor largo y aleatorio)
     - DRY_RUN=true para probar sin operar

  2. Arrancar:                  systemctl start proxy && journalctl -u proxy -f

  3. Probar el webhook local:
     curl -X POST -H "X-Webhook-Token: \$WEBHOOK_TOKEN" \\
          --data-binary @$DEST/signals/sample.txt http://127.0.0.1:8080/webhook

  4. Listener de Telegram (TG_API_ID, TG_API_HASH y TG_CHANNEL en .env):
     sudo -u $SERVICE_USER $DEST/.venv/bin/python $DEST/listener.py login
     sudo -u $SERVICE_USER $DEST/.venv/bin/python $DEST/listener.py channels
     systemctl enable --now listener && journalctl -u listener -f

  5. TLS (recomendado): pone nginx/caddy adelante y apunta tu software a
     https://tu-dominio/webhook

EOF
