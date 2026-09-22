FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# uvicorn serves the ASGI app; startup waits for the database to become
# reachable via the connection pool retry configuration.
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
