#!/bin/sh
# Cài backend + Cloudflare Tunnel trên Mac mini (chạy một lần, sau khi đã `cloudflared tunnel login`).
# Kết quả: https://api-vo.minio-quangmanh.io.vn -> http://127.0.0.1:8100, cả hai tự chạy lại khi đăng nhập/crash.
set -e
TUNNEL=vo-backend
HOSTNAME=api-vo.minio-quangmanh.io.vn
HERE="$(cd "$(dirname "$0")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
UID_GUI="gui/$(id -u)"

[ -f "$HOME/.cloudflared/cert.pem" ] || { echo "Chưa đăng nhập: chạy 'cloudflared tunnel login' trước."; exit 1; }

# 1. Backend chạy nền bằng launchd
cp "$HERE/vn.minio-quangmanh.vo-backend.plist" "$AGENTS/"
launchctl bootout "$UID_GUI/vn.minio-quangmanh.vo-backend" 2>/dev/null || true
launchctl bootstrap "$UID_GUI" "$AGENTS/vn.minio-quangmanh.vo-backend.plist"

# 2. Tạo tunnel (bỏ qua nếu đã có) và trỏ DNS
cloudflared tunnel info "$TUNNEL" >/dev/null 2>&1 || cloudflared tunnel create "$TUNNEL"
TUNNEL_ID=$(cloudflared tunnel list -o json | /usr/bin/python3 -c "import json,sys; print(next(t['id'] for t in json.load(sys.stdin) if t['name']=='$TUNNEL'))")
cloudflared tunnel route dns --overwrite-dns "$TUNNEL" "$HOSTNAME"

cat > "$HOME/.cloudflared/config.yml" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $HOME/.cloudflared/$TUNNEL_ID.json
ingress:
  - hostname: $HOSTNAME
    service: http://127.0.0.1:8100
  - service: http_status:404
EOF

# 3. cloudflared chạy nền bằng launchd (đọc ~/.cloudflared/config.yml)
cloudflared service install 2>/dev/null || launchctl kickstart -k "$UID_GUI/com.cloudflare.cloudflared"

echo "Xong. Kiểm tra: curl https://$HOSTNAME/"
