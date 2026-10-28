#!/bin/sh
# 小智AI v2 一键部署（在 MaixCAM2 设备上执行）
#
#   sh deploy.sh            正常部署
#   sh deploy.sh --keep     只更新代码，不动 systemd / launcher
#
# 部署内容：
#   /root/xiaozhi/              后台服务（唤醒 + 对话 + 弹窗渲染）
#   /maixapp/apps/xiaozhi/      设置 App（桌面图标）
#   /root/uishim/               状态栏/弹窗注入库（LD_PRELOAD 进 launcher）
#   /etc/systemd/system/xiaozhi.service + launcher drop-in
set -e

SRC=$(cd "$(dirname "$0")" && pwd)
KEEP=0
[ "$1" = "--keep" ] && KEEP=1

# Deployment is supported from the desktop, not while a camera App owns DRM.
if ! pgrep -x launcher >/dev/null 2>&1; then
    echo "请先退出前台 App，回到 MaixCAM2 应用桌面后再部署。" >&2
    exit 1
fi
if [ -f /etc/systemd/system/xiaozhi.service ]; then
    systemctl stop xiaozhi.service
fi

echo "=== 1. 后台服务 ==="
mkdir -p /root/xiaozhi /maixapp/apps/xiaozhi/assets /root/uishim
cp -f "$SRC/service/xiaozhi_service.py" /root/xiaozhi/
cp -f "$SRC/service/ui_render.py"      /root/xiaozhi/
cp -f "$SRC/service/hardware_tools.py" /root/xiaozhi/
cp -f "$SRC/service/camera_ownership.py" /root/xiaozhi/
cp -f "$SRC/service/vision_siot.py" /root/xiaozhi/
cp -f "$SRC/service/ui_feedback.py"    /root/xiaozhi/
cp -f "$SRC/service/ui_ownership.py"   /root/xiaozhi/
cp -f "$SRC/service/peripheral_tools.py" /root/xiaozhi/
cp -f "$SRC/service/imu_adapter.py"      /root/xiaozhi/
cp -f "$SRC/service/io_tools.py"         /root/xiaozhi/
cp -f "$SRC/service/service_cleanup.py"  /root/xiaozhi/
if [ ! -e /root/xiaozhi/io_access.json ]; then
    install -m 600 "$SRC/service/io_access.example.json" /root/xiaozhi/io_access.json
fi
cp -f "$SRC/service/xiaozhi.service"   /root/xiaozhi/_pkg/xiaozhi.service 2>/dev/null || {
    mkdir -p /root/xiaozhi/_pkg; cp -f "$SRC/service/xiaozhi.service" /root/xiaozhi/_pkg/; }

echo "=== 2. 设置 App ==="
cp -f "$SRC/app/main.py"    /maixapp/apps/xiaozhi/
cp -f "$SRC/app/app.yaml"   /maixapp/apps/xiaozhi/
[ -f "$SRC/app/assets/icon.png" ] && cp -f "$SRC/app/assets/icon.png" \
    /maixapp/apps/xiaozhi/assets/icon.png
chmod -R a+rX /maixapp/apps/xiaozhi
python3 /maixapp/apps/gen_app_info.py /maixapp/apps >/dev/null 2>&1 || true

if [ "$KEEP" = "1" ]; then
    echo "=== --keep：跳过 systemd/注入层 ==="
    systemctl restart xiaozhi.service
    echo "完成"
    exit 0
fi

echo "=== 3. 编译注入库 ==="
cd /root/uishim
cp -f "$SRC/uishim/maixcam2_overlay.c" .
gcc -O2 -fPIC -shared -o libmaixcam2_overlay.so.new maixcam2_overlay.c -ldl -lpthread
# Do not truncate a library still mapped by the running desktop.
systemctl stop launcher.service
mv -f libmaixcam2_overlay.so.new libmaixcam2_overlay.so
ls -la libmaixcam2_overlay.so

echo "=== 4. systemd：服务 + launcher 注入 ==="
cp -f "$SRC/service/xiaozhi.service" /etc/systemd/system/xiaozhi.service
mkdir -p /etc/systemd/system/launcher.service.d
cat > /etc/systemd/system/launcher.service.d/zz-overlay.conf << 'EOC'
[Service]
Environment=LD_PRELOAD=/root/uishim/libmaixcam2_overlay.so
Environment=MAIXCAM2_GF=1
Environment=MAIXCAM2_GF_LOG=/tmp/statusbar.log
Environment=MAIXCAM2_OVL=/root/xiaozhi/status.rgba:10:5:0,/root/xiaozhi/popup.rgba:0:0:0
EOC
systemctl daemon-reload
systemctl enable xiaozhi.service >/dev/null 2>&1 || true

echo "=== 5. 启动 ==="
systemctl start launcher.service
# xiaozhi.service additionally waits for the desktop's DRM fd before audio init.
systemctl start xiaozhi.service

echo
echo "=== 完成 ==="
echo "服务日志 : journalctl -u xiaozhi -f"
echo "弹窗截图 : touch /tmp/ovl_dump_b  → /tmp/ovl_snap_b.raw (640x480 BGRA)"
echo "调试唤醒 : touch /tmp/xz_call"
echo "临时停用 : systemctl stop xiaozhi"
echo "关闭注入 : touch /root/uishim/DISABLE && systemctl restart launcher"
