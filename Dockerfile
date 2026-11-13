FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DB_PATH=/data/app.db \
    MIGRATION_INTERVAL_SECONDS=2 \
    MIGRATION_BATCH_SIZE=50 \
    MIGRATION_RECORD_DELAY_MS=0 \
    SEED_DEMO=false

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
