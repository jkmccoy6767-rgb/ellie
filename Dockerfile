FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ELLIE_DB_PATH=/data/ellie.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ellie ./ellie
COPY data/sp500_constituents.csv ./data/sp500_constituents.csv

VOLUME ["/data"]
EXPOSE 8000

# Build the warehouse on first start if it is empty, then serve.
CMD ["sh", "-c", "[ -s \"$ELLIE_DB_PATH\" ] || python -m ellie ingest; python -m ellie serve --host 0.0.0.0 --port 8000"]
