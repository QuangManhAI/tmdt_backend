#!/bin/sh
# Chạy backend trên Mac mini cho Cloudflare Tunnel: nạp mac.env (ghi đè .env) rồi khởi động uvicorn.
# Chỉ nghe 127.0.0.1, bên ngoài vào qua tunnel.
set -e
cd "$(dirname "$0")/../.."
set -a
. ./mac.env
set +a
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8100}" \
  --proxy-headers --forwarded-allow-ips 127.0.0.1 --workers 1
