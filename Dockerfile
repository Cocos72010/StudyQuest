FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

RUN mkdir -p /app/data && useradd --system --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser
CMD ["python", "app.py"]
