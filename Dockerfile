# Imagen única para los dos servicios: `proxy` (webhook + gestión) y `listener` (Telegram).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 TZ=UTC

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy proxy.py paper.py classify.py listener.py sync.py ./
COPY signals ./signals
COPY test_paper.py test_classify.py test_sync.py ./

# Usuario sin privilegios; todo lo que se escribe (estado, journal, logs, sesión de
# Telegram) va al volumen /data.
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin trader \
    && mkdir -p /data && chown trader:trader /data
USER trader

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PROXY_PORT', '8080') + '/health', timeout=4)" || exit 1

CMD ["python", "proxy.py", "serve"]
