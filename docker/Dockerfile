FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

COPY . .

RUN useradd -m -u 1001 telecom && chown -R telecom:telecom /app
USER telecom

EXPOSE 8000

CMD ["gunicorn", "main:app",
     "-k", "uvicorn.workers.UvicornWorker",
     "--workers", "4",
     "--bind", "0.0.0.0:8000",
     "--access-logfile", "-",
     "--error-logfile", "-"]