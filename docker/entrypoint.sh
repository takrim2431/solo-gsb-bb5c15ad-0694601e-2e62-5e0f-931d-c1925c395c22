#!/bin/sh
# 容器启动时安装/核对依赖，随后 exec 传入的启动命令。
set -e

echo "[entrypoint] 安装依赖 (requirements.txt) ..."
pip install -r /app/requirements.txt

echo "[entrypoint] 启动: $*"
exec "$@"
