FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

RUN useradd --system --uid 10001 --create-home appuser \
    && mkdir -p /var/lib/slimes-marketing \
    && chown -R appuser:appuser /app /var/lib/slimes-marketing

USER 10001:10001

EXPOSE 8765

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]
