FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    wget \
    gnupg \
    ca-certificates \
    fonts-noto-cjk \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    xdg-utils \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# 安装 Node.js
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor >/usr/share/keyrings/google-linux.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" >/etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements.txt /tmp/requirements.txt
COPY apps/console/requirements.txt /tmp/console-requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/console-requirements.txt

# 拉取并构建前端
# rsbuild 产物结构：dist/index.html + dist/static/js/*.js + dist/static/css/*.css
# 后端 FastAPI 挂载 /static → apps/console/static/，HTML 里引用 /static/js/... 和 /static/css/...
# 所以需要把 dist/static/* 扁平化到 apps/console/static/，同时把 index.html 也拷过去
RUN git clone https://github.com/moranch/grok-register-ui.git /tmp/frontend \
    && cd /tmp/frontend \
    && npm install --legacy-peer-deps \
    && npm run build \
    && mkdir -p /workspace/apps/console/static \
    && cp dist/index.html /workspace/apps/console/static/index.html \
    && if [ -d dist/static ]; then cp -r dist/static/. /workspace/apps/console/static/; fi \
    && rm -rf /tmp/frontend /root/.npm

CMD ["bash"]
