FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-full.txt ./
RUN pip install --no-cache-dir -r requirements-full.txt && python -m playwright install --with-deps chromium
COPY . .
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host $HOST --port $PORT"]
