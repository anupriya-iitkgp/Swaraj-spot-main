FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY spot/ spot/

# demo-friendly grace window; override at `docker run -e ...`
ENV SPOT_GRACE_SECONDS=25 \
    SPOT_FORCE_STOP_AT=20

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ops/overview', timeout=2)"

CMD ["uvicorn", "spot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
