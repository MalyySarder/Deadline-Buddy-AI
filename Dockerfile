FROM node:22-alpine AS frontend
WORKDIR /build
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN corepack prepare pnpm@11.25.0 --activate && pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.lock.txt ./backend/requirements.lock.txt
RUN pip install --no-cache-dir -r backend/requirements.lock.txt
COPY backend/ ./backend/
COPY --from=frontend /build/dist ./frontend/dist
RUN useradd --create-home app && mkdir /data && chown app:app /data
USER app
ENV DATABASE_PATH=/data/deadline_buddy.sqlite3
EXPOSE 8000
CMD ["uvicorn", "main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
