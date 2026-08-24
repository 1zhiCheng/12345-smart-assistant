# 文枢后端镜像
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 系统依赖（pdfplumber 需要部分 C 库，paddleocr 场景可另行扩展）
# 国内开发环境使用阿里云镜像，避免 deb.debian.org 下载超时。
RUN sed -i \
      -e 's|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g' \
      -e 's|http://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

EXPOSE 8000

# 启动：等待依赖就绪后运行
CMD ["sh", "-c", "python -m scripts.wait_for_deps && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
