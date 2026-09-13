FROM python:3.12-slim

# 安装 nginx-rtmp + ffmpeg + sqlite3
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nginx \
        libnginx-mod-rtmp \
        ffmpeg \
        sqlite3 \
        curl \
        supervisor \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 复制配置
COPY nginx-rtmp.conf /etc/nginx/nginx.conf
COPY supervisord.conf /etc/supervisor/conf.d/streamlive.conf

RUN mkdir -p /app/data/hls /app/data/streams /var/log/supervisor

# 清理 nginx 默认站点（如有）
RUN rm -f /etc/nginx/sites-enabled/default 2>/dev/null; true

EXPOSE 5200 1935

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
ENV TZ=Asia/Shanghai

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
