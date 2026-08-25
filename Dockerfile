FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HEU_MCP_DB=/data/heu-mcp.db \
    HEU_DOWNLOAD_DIR=/tmp \
    PORT=8080

WORKDIR /app

COPY requirements.txt requirements-remote.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-remote.txt

COPY server.py remote_server.py ./

# Il volume monta /data: la directory deve esistere anche senza volume (dev locale).
RUN mkdir -p /data

EXPOSE 8080

# Un solo worker: lo stato OAuth vive in SQLite sul volume e la concorrenza è gestita
# da asyncio (I/O async + lavoro CPU su threadpool).
CMD ["uvicorn", "remote_server:app", "--host", "0.0.0.0", "--port", "8080"]
