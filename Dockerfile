FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# OCR language data is installed in the image so a deployment can detect it honestly.
# The application still degrades safely when OCR is unavailable or a page has no text layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       tesseract-ocr \
       tesseract-ocr-eng \
       tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt constraints.txt ./
RUN python -m pip install --no-cache-dir "pip==26.2.1" \
    && python -m pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin veris
COPY --chown=veris:veris . .
USER veris

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()"

# The hosted Compose proxy has the fixed address 172.30.0.2. Trust forwarding only
# from that internal peer; Nginx overwrites client-forwarding headers before proxying.
CMD ["python", "-m", "uvicorn", "proven.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=172.30.0.2"]
