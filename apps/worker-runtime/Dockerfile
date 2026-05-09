FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11vnc \
    novnc \
    websockify \
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
#
# 自动探测网络环境选择最快的源：
#   - GitHub：curl 5 秒内能通则直连，否则走 gh-proxy.com 镜像
#   - npm：curl 3 秒内能通官方源则用官方源，否则切换到 npmmirror
RUN set -eux; \
    echo "===== probe GitHub ====="; \
    if curl -fsS --max-time 5 -o /dev/null https://github.com/ 2>/dev/null; then \
      GH_URL="https://github.com/moranch/grok-register-ui.git"; \
      echo "→ GitHub direct reachable"; \
    else \
      GH_URL="https://gh-proxy.com/https://github.com/moranch/grok-register-ui.git"; \
      echo "→ GitHub unreachable, using gh-proxy.com mirror"; \
    fi; \
    echo "===== probe npm ====="; \
    if curl -fsS --max-time 3 -o /dev/null https://registry.npmjs.org/ 2>/dev/null; then \
      NPM_REG="https://registry.npmjs.org"; \
      echo "→ using npm official"; \
    else \
      NPM_REG="https://registry.npmmirror.com"; \
      echo "→ npm official unreachable, using npmmirror"; \
    fi; \
    echo "===== building frontend ====="; \
    git clone --depth 1 "$GH_URL" /tmp/frontend; \
    cd /tmp/frontend; \
    npm config set registry "$NPM_REG"; \
    npm install --legacy-peer-deps --prefer-offline --no-audit --no-fund; \
    npm run build; \
    mkdir -p /opt/webui; \
    cp dist/index.html /opt/webui/index.html; \
    if [ -d dist/static ]; then cp -r dist/static/. /opt/webui/; fi; \
    rm -rf /tmp/frontend /root/.npm

# 告诉 app.py 从这个路径读取前端资源（不会被 docker-compose volume 覆盖）
ENV WEBUI_DIR=/opt/webui

# 入口脚本：启动 Xvfb + x11vnc + noVNC + FastAPI 控制台
COPY apps/worker-runtime/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

CMD ["bash"]
