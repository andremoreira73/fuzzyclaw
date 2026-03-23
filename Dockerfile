FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g 1000 -m appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8200

CMD ["uvicorn", "fuzzyclaw.asgi:application", "--host", "0.0.0.0", "--port", "8200"]
