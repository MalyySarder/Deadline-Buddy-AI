FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.lock.txt /app/requirements.lock.txt
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY backend /app/backend
ENV PYTHONUNBUFFERED=1
CMD ["python", "backend/runner.py"]
