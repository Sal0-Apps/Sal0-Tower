FROM python:3.13-slim
LABEL org.opencontainers.image.title="Sal0 Tower" \
      org.opencontainers.image.description="Web interface for an existing Watchtower" \
      org.opencontainers.image.source="https://github.com/Sal0-Apps/Sal0-Tower" \
      org.opencontainers.image.licenses="GPL-3.0-only"
WORKDIR /opt/sal0-tower
COPY requirements.txt /opt/sal0-tower/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY panel /opt/sal0-tower
ENV SAL0_PORT=7860 SAL0_DATA=/data SAL0_ZIMA_APPS=/zima-apps PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python3", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('SAL0_PORT','7860')+'/healthz', timeout=3).read()"]
ENTRYPOINT ["python3", "-u", "/opt/sal0-tower/server.py"]
