FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.32.*" tzdata

COPY main.py .

# State is mounted read-only here; overlay (our own data) is a writable volume.
ENV STATE_FILE=/data/apartment_state.json \
    OVERLAY_FILE=/overlay/overlay.json

EXPOSE 8765

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]
