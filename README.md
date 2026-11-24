# proxy — webhook de señales → Bitunix (futuros USDT-M)

Recibe señales por webhook (el formato de alerta tipo Telegram/TradingView), abre la
posición en Bitunix **tal como la pide la señal** y gestiona la salida.

```
TradingView / Google Chat / lo que sea  --POST-->  proxy (VPS)  -->  Bitunix Futures
```

Qué hace con cada señal:

1. Parsea par, lado, entrada, SL y los N objetivos.
2. Deja el símbolo en **margen AISLADO (ISOLATION)** y aplica el apalancamiento
   (el de la señal si viene, si no el default **x20**, siempre con tope `MAX_LEVERAGE`).
3. Calcula el tamaño con el **5 % de la cuenta** (`POSITION_SIZE_PCT`) y envía la orden
   de entrada **LIMIT** al precio de la señal (`ENTRY_ORDER_TYPE=MARKET` si la prefieres
   a mercado).
4. En cuanto se llena, coloca el **SL** como orden de posición (se dispara a mercado sobre
   el tamaño que quede vivo) y **un TP por cada objetivo**, repartiendo la cantidad.
5. Cuando el precio llega a **Objetivo 1** mueve el **SL a break-even** (entrada +/- 0.1 %
   para cubrir comisiones). Los TP restantes siguen vivos.
6. Si la posición desaparece (SL o TP final), cancela los TP huérfanos y cierra el registro.

Sobrevive reinicios: el estado de las operaciones vivas vive en `proxy_state.json` y al
levantar el servicio el monitor retoma la gestión.

## Señal que entiende

```
#CHZ/USDT - Short 🔴

Entrada: 0.00883
Stop Loss: 0.0090066

🎯 Objetivo 1: 0.00873336
🎯 Objetivo 2: 0.00871886
🎯 Objetivo 3: 0.00870219
🎯 Objetivo 4: 0.00868302
🎯 Objetivo 5: 0.00866097

Apalancamiento: x20
```

Acepta el body como texto plano (`text/plain`, lo que manda TradingView) o como JSON con
el texto dentro de `text` / `alert` / `message` / `body`, y también JSON estructurado:

```json
{"symbol":"CHZUSDT","side":"short","entry":0.00883,"sl":0.0090066,
 "tps":[0.00873336,0.00871886],"leverage":20}
```

Ignora mayúsculas y minúsculas y el orden de los objetivos: valida que queden del lado del
profit y los ordena por precio (`Objetivo 1` siempre es el primero que se ejecuta). Detecta
`Long`/`Short` por texto o por los emojis 🔴 🟢.

## Paper trading (BROKER=paper)

Bitunix no tiene testnet, así que `paper.py` simula el exchange **sobre precios reales**:
pares, tick, tamaño mínimo, último precio y velas de 1 m salen de la API pública de Bitunix
(sin claves) y las órdenes, posiciones y saldo viven en local (`PAPER_STATE_FILE`). Todo el
ciclo del bot (fill, SL, TP parciales, break-even, cierre) corre sin cambios.

* Entrada LIMIT: se llena al tocar el precio (comisión maker); si ya es ejecutable al
  colocarla, al precio de mercado (taker). Las mechas entre consultas se detectan con el
  máximo/mínimo nuevo de la vela de 1 m.
* Conservador: si en el mismo rango caben SL y TP, o el fill y el SL, gana el SL.
* Comisiones y slippage configurables (`PAPER_*`); comprueba las de tu nivel en Bitunix.
* Cada operación cerrada va a `PAPER_JOURNAL_FILE` (JSON por línea): fills, comisiones,
  PnL neto y resultado en R sobre el SL original.

Con `SIZING_MODE=risk` el tamaño sale del SL: se arriesga `RISK_PCT` del equity por
operación (0.5 % de 1 000 USDT con entrada 6.102 y SL 6.296 → 25.7 INJ, ~157 USDT nocionales).

## Listener de Telegram

`listener.py` lee el canal con **tu cuenta** (Telethon, nunca escribe) y manda las señales
al webhook local. `classify.py` separa los mensajes en `signal`, `target_hit`
("Objetivo N Tocado"), `pump` y `other`; todos se guardan en `MESSAGES_FILE` y sólo las
señales recientes (`SIGNAL_MAX_AGE_SEC`) llegan al proxy.

```bash
python listener.py login      # una vez: teléfono, código y 2FA
python listener.py channels   # id del canal para TG_CHANNEL
python listener.py            # escuchar (unit systemd `listener`)
```

## Endpoints

| Método | Ruta | Auth | Descripción |
| --- | --- | --- | --- |
| `POST` | `/webhook` | token | señal en el body (texto o JSON) |
| `POST` | `/webhook/<token>` | token en la ruta | para servicios que no pueden mandar headers |
| `GET` | `/health` | no | live-check barato, sin datos sensibles |
| `GET` | `/status` | token | equity, config, margen usado, operaciones gestionadas, contadores |
| `GET` | `/events?after=N` | token | cola de logs, incremental por cursor |
| `POST` | `/api/cancel` | token | `{"id": "..."}` cancela la entrada de una orden sin fill |

Auth (si `WEBHOOK_TOKEN` está definido): header `X-Webhook-Token`, header `X-Token`,
`Authorization: Bearer ...`, `?token=...` o la ruta. Con `WEBHOOK_KEYWORD` además exige esa
frase dentro del mensaje. Respuestas JSON: `{"ok":true,...}` o `{"ok":false,"error":"..."}`.
`/health` es público porque no revela nada; `/status`, `/events` y `/api/cancel`
piden token. Cualquier otra ruta, incluida `/`, responde 404: no hay interfaz web, el bot
es un webhook puro.

## Integración con software externo (Discord, Telegram, n8n, TradingView…)

El bot no sirve HTML: se maneja por HTTP. Tu software (un bot de Discord que reenvíe los
mensajes de un canal, un bridge de Telegram, n8n, la alerta de TradingView…) hace un
`POST` con el texto de la señal y recibe la respuesta JSON con lo ejecutado:

```bash
curl -X POST https://tu-dominio/webhook \
  -H 'X-Webhook-Token: TU_TOKEN' \
  -H 'Content-Type: text/plain' \
  --data $'#CHZ/USDT - Short \U0001F534\nPrecio: 0.11725\nEntrada: 0.11720-0.11690\nStop Loss: 0.11850\nObjetivos: 0.11490, 0.11290, 0.11010'
```

```json
{"ok": true, "action": "short", "symbol": "CHZUSDT", "qty": 8500,
 "entry": "0.11705", "sl": "0.11850", "margin": "99.63", "tps": ["0.11490", "0.11290", "0.11010"]}
```

* La señal se parsea igual que desde TradingView: par, lado, entrada, SL y objetivos en
  texto libre (ver *Señal que entiende* arriba); si el mensaje no contiene un par
  reconocible, responde `{"ok":false}` sin tocar nada.
* Si tu software no puede mandar headers, usa `POST /webhook/<token>`.
* Para monitorizar lo que hace el bot: `GET /status` (resumen) y `GET /events?after=N`
  (cola de logs incremental). `POST /api/cancel` con `{"id":...}` o `{"symbol":...}`
  cancela una entrada aún sin fill.
* Cambios de ajuste (apalancamiento, % de cuenta, claves): se editan en `.env` y
  `systemctl restart proxy`.

Con `DRY_RUN=true` el monitor simula el ciclo completo (`DRY_RUN_SIM=true`): fill de la
entrada, objetivos que se ejecutan de uno en uno, break-even y cierre, sumando el PnL al
equity simulado. Se ve el ciclo entero en `journalctl -u proxy` y en `GET /events` sin
tocar el exchange.

Si lo sirves detrás de Caddy/nginx, el webhook comparte origen con todo lo demás, así que
no hace falta CORS.

## Multi-cuenta (varios webhooks, varias APIs)

Sin tocar código: una instancia del bot por cuenta de exchange. Cada una con su `.env`
(claves, token y `WEBHOOK_TOKEN` propios), su `STATE_FILE`/`LOG_FILE` (viven en su
`WorkingDirectory`), su puerto localhost y su unit systemd:

| Ruta pública | Instancia | Cuenta |
| --- | --- | --- |
| `https://tu-dominio/webhook/…` | `/opt/proxy` (unit `proxy`, `:8080`) | clave A |
| `https://tu-dominio/c2/webhook/…` | `/opt/proxy2` (unit `proxy2`, `:8081`) | clave B |

Cada instancia gestiona su BE/TP/SL y su tope de margen sobre la equity **de su cuenta**;
no se ven entre ellas. Los tokens no se intercambian (el de una da 401 en la otra). La ruta
se monta en Caddy con `handle_path /c2/* { reverse_proxy 127.0.0.1:8081 }`.
Requisito: que las claves sean de **cuentas Bitunix distintas**; dos keys del mismo
account se pisarían entre monitores.

## Requisitos del servidor

El bot es IO-cpu: descansa y hace 4-5 requests REST por operación cada `POLL_INTERVAL_SEC`.

| Recurso | Consumo real |
| --- | --- |
| RAM | ~50 MB (python + waitress) |
| CPU | ~0 % en reposo, picos de décimas de segundo al procesar una señal |
| Disco | ~15 MB de código + log y `proxy_state.json` (kilobytes) |
| Red | peticiones HTTPS salientes; el webhook es 1 request por señal |

Con 1 vCPU y 2 GB sobra de sobra, y caben varios bots más. Lo que de verdad importa:

* **Latencia**: escoge el datacenter más cercano a los servidores de Bitunix (mídelo con
  `curl -o /dev/null -s -w '%{time_total}\n' https://fapi.bitunix.com/api/v1/futures/market/tickers`).
  Menos RTT = el BE y el fill se detectan antes.
* **NTP** sincronizado: la firma exige estar dentro de 60 s del reloj del servidor
  (`timedatectl set-ntp true`).
* **Log**: `proxy.log` y `proxy_state.json` crecen poco, pero pon `logrotate` para el log.
* Si el plan trae cPanel/Plesk, desactívalos o no los uses: te comen RAM y puertos. Basta
  un VPS pelado + systemd; para TLS, Caddy (un binary, ~30 MB) por delante del `PROXY_PORT`.

## Instalación en el VPS

```bash
sudo bash install.sh                      # deja todo en /opt/proxy + systemd
sudo nano /opt/proxy/.env                 # API key, secret, WEBHOOK_TOKEN, DRY_RUN=true
sudo systemctl start proxy && journalctl -u proxy -f
```

Sin instalador (venv a mano):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
./proxy serve
```

Pruebas locales:

```bash
./proxy selftest           # parsing, reparto de TPs y break-even (sin red)
python3 test_monitor.py    # ciclo de vida completo contra un exchange simulado
python3 test_paper.py      # modo paper: fills, mechas, TP, BE, SL conservador, journal en R
python3 test_classify.py   # tipos de mensaje del canal
bash test_webhook.sh       # levanta el servidor en DRY_RUN y dispara 5 requests
./proxy parse signals/sample.txt
./proxy status
```

Primera vez **deja `DRY_RUN=true`**: loguea exactamente la orden y la protección que enviaría
sin tocar el exchange.

## Configuración que importa

| Variable | Default | Para qué |
| --- | --- | --- |
| `POSITION_SIZE_PCT` | `10` | % de la cuenta por operación |
| `SIZING_MODE` | `margin` | `margin`: el % es margen (exposición = % × apalancamiento). `notional`: el % es exposición |
| `MAX_TOTAL_MARGIN_PCT` | `50` | tope de margen sumado entre todas las operaciones activas (0 = sin tope) |
| `DEFAULT_LEVERAGE` | `20` | apalancamiento si la señal no lo trae |
| `USE_SIGNAL_LEVERAGE` | `true` | usa `Apalancamiento: xNN` de la señal |
| `MAX_LEVERAGE` | `20` | tope duro, aunque la señal pida más |
| `MARGIN_MODE` | `ISOLATION` | aislado, forzado por el script |
| `ENTRY_ORDER_TYPE` | `LIMIT` | `LIMIT` en el precio de la señal o `MARKET` |
| `LIMIT_ORDER_TTL_SEC` | `900` | cancela la entrada si no se llena (0 = nunca) |
| `MAX_PRICE_DEVIATION_PCT` | `5` | rechaza señales demasiado alejadas del mercado (0 = off) |
| `TP_MODE` | `partials` | `partials`: SL + un TP por objetivo. `single`: un solo TP/SL de posición |
| `TP_SPLIT` | `equal` | `equal` o `"20,20,20,20,20"` |
| `BE_ENABLED` / `BE_AFTER_TP` | `true` / `1` | break-even al objetivo N |
| `BE_OFFSET_PCT` | `0.1` | colchón de fees sobre la entrada |
| `BE_TRIGGER` | `both` | `fill`, `price` o `both` |
| `MAX_OPEN_POSITIONS` | `5` | tope de operaciones simultáneas |
| `DEDUPE_WINDOW_SEC` | `180` | ignora señales repetidas en la ventana |

`.env.example` trae la lista completa comentada.

### Ejemplo de alerta en TradingView

* URL del webhook: `https://tu-dominio/webhook/TU_TOKEN` (token en la ruta) o
  `https://tu-dominio/webhook?token=TU_TOKEN`.
* Mensaje de la alerta: el texto de la señal, tal cual el bloque de arriba.
* TradingView muestra `Sent successfully` cuando el proxy responde `200` con `ok:true`;
  si rechaza la señal devuelve `400` con el motivo en `error`.

### El tamaño, con números

Cuenta con 1 000 USDT, `POSITION_SIZE_PCT=10`, apalancamiento x20:

| `SIZING_MODE` | margen bloqueado | exposición (notional) |
| --- | --- | --- |
| `margin` (default) | 100 USDT (10 %) | 2 000 USDT (10 % × 20) |
| `notional` | 5 USDT | 100 USDT (10 %) |

A x20 el margen del 10 % son **2 veces el equity en exposición**, así que entran en juego dos
frenos: `MAX_OPEN_POSITIONS` (5 por defecto) y `MAX_TOTAL_MARGIN_PCT` (50 % por defecto, o sea
5 trades de 10 %). Cuando se alcanza el tope la señal se descarta con `ok:false` y el motivo.

El SL de la señal queda protegido por el exchange (orden nativa de posición), así que la
posición se cierra sola aunque el VPS esté caído. El break-even usa `BE_OFFSET_PCT` para que
quedar plano no signifique perder comisiones: con entrada 0.00883 y 0.1 % el SL se va a 0.00882.

## Cómo se protege la operación

* Señal duplicada (mismo par, lado, entrada, SL y TPs) dentro de `DEDUPE_WINDOW_SEC` → se ignora.
* Par con posición abierta en contra → se rechaza salvo `ALLOW_REVERSE=true`.
* Apalancamiento, tamaño mínimo del contrato, tick de precio y precisión de cantidad se leen
  de `trading_pairs` de Bitunix: la señal se redondea al tick del par.
* Si el margen del símbolo estaba en CROSS, el bot lo cambia a ISOLATION; si el exchange lo
  rechaza (hay posición/orden abierta), la señal se rechaza en vez de operar en cruzado.
* Modo de posición `HEDGE` y `ONE_WAY` soportados: en HEDGE manda `tradeSide=OPEN`.
* Los TP usan `LAST_PRICE` por defecto; `MARK_PRICE` es más conservador contra mechas.
* El bot no reinvierte ni cierra a mano: usa órdenes nativas TP/SL del exchange, así la
  posición queda protegida aunque el VPS se caiga.

## Seguridad

* API key **sin permiso de retiro** y con lista blanca de la IP del VPS.
* `chmod 600 .env` (el instalador ya lo hace).
* Webhook detrás de TLS (nginx/caddy) y con `WEBHOOK_TOKEN` largo.
* El bot corre con una cuenta/sub-cuenta dedicada, no con la principal.

## Limitaciones

* Un solo `positionId` gestionado por par: dos señales al mismo tiempo sobre el mismo símbolo
  se ignoran (una por vez).
* En `TP_MODE=partials`, si la cantidad no da para partir el tamaño en N partes sobre el
  mínimo del contrato, se coloca un único TP con toda la cantidad en el Objetivo 1.
* El tamaño se calcula con el equity del momento de la señal.

## Licencia

MIT — ver [LICENSE](LICENSE).

Software de trading con dinero real: úsalo bajo tu propia responsabilidad. Prueba primero
con `DRY_RUN=true`, arranca con `MAX_LEVERAGE` y `POSITION_SIZE_PCT` bajos, y usa una cuenta
dedicada con una API key sin permiso de retiro.
