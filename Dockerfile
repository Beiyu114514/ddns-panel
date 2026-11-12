FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# 固定 UID/GID 便于宿主机为挂载目录授权，避免应用以 root 身份读取其他文件。
RUN groupadd --gid 10001 ddns && useradd --uid 10001 --gid ddns --no-create-home ddns \
    && mkdir -p /app/data && chown -R ddns:ddns /app && chmod 700 /app/data
USER 10001:10001

EXPOSE 8300
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300", "--workers", "1", "--no-proxy-headers"]
