FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir requests jinja2 anthropic
COPY agency ./agency
COPY vault ./vault-template
RUN useradd --create-home agency && mkdir -p /data /vault && chown agency /data /vault
USER agency
ENV DB_PATH=/data/agency.db BUILD_DIR=/data/sites OBSIDIAN_VAULT=/vault PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
CMD ["sh", "-c", "cp -rn /app/vault-template/. /vault/ 2>/dev/null; exec python -m agency supervise"]
