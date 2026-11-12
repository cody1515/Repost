FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
# yt-dlp updates often to keep up with TikTok/Instagram changes; grab the latest on each start
CMD ["sh", "-c", "pip install -q -U yt-dlp || true; python app.py"]
