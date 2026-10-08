# ---------- 阶段 1：构建前端 ----------
FROM node:24-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------- 阶段 2：运行时 ----------
FROM python:3.11-slim
LABEL org.opencontainers.image.source="https://github.com/oldxiaoxiao/xiao-pan-auto-save" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data TZ=Asia/Shanghai

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY desktop ./desktop
RUN pip install --no-cache-dir .

COPY --from=frontend /build/dist ./frontend/dist

VOLUME ["/app/data"]
EXPOSE 8432

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8432/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8432"]
