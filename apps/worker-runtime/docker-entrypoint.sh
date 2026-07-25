#!/usr/bin/env bash
# Console 容器入口脚本：
#   1. 启动 Xvfb 虚拟显示器（DISPLAY=:99）
#   2. 启动 x11vnc（把 :99 屏幕暴露成 VNC 5900）
#   3. 启动 noVNC（把 VNC 5900 → WebSocket 6080，浏览器直接访问）
#   4. 启动 FastAPI Console
#
# 所有"调试模式"任务的 Chrome 都会渲染到 :99 屏幕上，
# 用户通过 http://host:6080/vnc.html 即可实时看到浏览器。
set -e

# 只有 GROK_ENABLE_NOVNC=1（默认开启）才拉起 VNC 组件
ENABLE_NOVNC="${GROK_ENABLE_NOVNC:-1}"

if [ "$ENABLE_NOVNC" = "1" ]; then
    # docker restart 不会清理 /tmp，上一次的 X 锁文件会残留，
    # 导致新的 Xvfb 启动时报 "Server is already active for display 99" 并退出，
    # x11vnc 连不上也会变僵尸，前端 noVNC 显示 "connection is closed"。
    # 强制清理 display 99 的残留。
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

    echo "[entrypoint] starting Xvfb on :99"
    Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset > /tmp/xvfb.log 2>&1 &

    # 等 Xvfb socket 真正就绪（最多 5 秒）
    for _ in $(seq 1 10); do
        if [ -e /tmp/.X11-unix/X99 ]; then
            break
        fi
        sleep 0.5
    done

    # 把 :99 变成持续输出的 VNC 源
    # -bg 让 x11vnc 以 daemon 方式自行 fork，避免作为 app.py 子进程退出时变僵尸
    echo "[entrypoint] starting x11vnc on :5900"
    x11vnc -display :99 -forever -shared -nopw -quiet -noxdamage \
        -rfbport 5900 -bg -o /tmp/x11vnc.log

    # Do not expose a WebSocket endpoint before x11vnc has completed its RFB
    # startup.  A process can exist while its VNC socket is not yet ready,
    # which leaves noVNC permanently showing "Connecting".
    _vnc_ready=0
    for _ in $(seq 1 20); do
        if python - <<'PY'
import socket

try:
    with socket.create_connection(("127.0.0.1", 5900), timeout=1) as sock:
        sock.settimeout(1)
        greeting = sock.recv(12)
except OSError:
    raise SystemExit(1)
raise SystemExit(0 if greeting.startswith(b"RFB ") else 1)
PY
        then
            _vnc_ready=1
            break
        fi
        sleep 0.5
    done

    if [ "$_vnc_ready" != "1" ]; then
        echo "[entrypoint] x11vnc failed to produce an RFB greeting; see /tmp/x11vnc.log"
    fi

    # noVNC：把 VNC 流转成 WebSocket，让浏览器能看
    # Debian 12 里 novnc 装在 /usr/share/novnc
    if [ -d /usr/share/novnc ]; then
        echo "[entrypoint] starting noVNC on :6080 -> ws://:5900"
        websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 > /tmp/novnc.log 2>&1 &
    else
        echo "[entrypoint] /usr/share/novnc not found, skip noVNC"
    fi

    # 子进程以后启动的 chrome 会自动用这个 DISPLAY
    export DISPLAY=:99
fi

echo "[entrypoint] starting console"
exec python /workspace/apps/console/app.py
