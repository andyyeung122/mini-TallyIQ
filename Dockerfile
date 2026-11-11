# One container: builds the React app, then serves it and the API with FastAPI.

# ---- 1. Build the frontend ----
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. Python API + the built frontend ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATIC_DIR=/app/static
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=web /web/dist ./static

# Hosts like Render and Cloud Run tell the app which port to use via $PORT.
CMD ["sh", "-c", "exec uvicorn app.server:root --host 0.0.0.0 --port ${PORT:-8080}"]
