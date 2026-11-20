FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000

# Utilisateur non root, et dossier de données qui lui appartient
RUN useradd --create-home --uid 10001 mya \
    && mkdir -p /data && chown mya:mya /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mya_mcp ./mya_mcp

USER mya
VOLUME /data
EXPOSE 8000

# /health est public et ne renvoie aucune donnée sensible
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4).status == 200 else 1)"

CMD ["python", "-m", "mya_mcp"]
