FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py main.py ./logserver/

RUN useradd --system --uid 10001 logserver \
    && mkdir /data \
    && chown logserver /data
USER logserver
VOLUME /data

EXPOSE 5455

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5455/healthz', timeout=3)"]

CMD ["uvicorn", "logserver.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "5455"]
