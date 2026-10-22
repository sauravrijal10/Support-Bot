FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image at build time instead of
# downloading it on every container start.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

COPY . .

EXPOSE 8000

# Production start: the port comes from $PORT (Render sets it; 8000 otherwise),
# and --proxy-headers lets the app see that requests arrived over HTTPS, which
# marks the refresh-token cookie Secure. Client IPs for rate limits are read
# separately (TRUST_PROXY, app/api/deps.py). docker-compose.yml overrides this
# with --reload for local development.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
