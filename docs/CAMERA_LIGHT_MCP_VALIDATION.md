# 摄像头与补光灯 MCP 接入验收

日期：2026-10-25（Asia/Taipei）。设备：`maixcam2-test-device`。代码分支：`codex/camera-light-mcp`，起点 `9d7885e`。

## 已交付

- 新增 `self.camera.take_photo`、`self.camera.get_status`。
- 新增 `self.light.set_power`、`self.light.set_brightness`、`self.light.get_status`。
- 保留原有 5 个工具，总数 10 个；参数表见 [MCP 清单](MCP_CAPABILITIES.md)。
- 图像仅在收到拍照请求时采集；640×480、JPEG quality=80，最大 2 MiB，不长期保存。
- 拍摄与 HTTP 分析在工作线程执行；MCP 耗时任务由会话管理，收包协程可继续运行，回包绑定原连接。断线时取消回包、清除 vision 凭据。
- 补光灯支持 0–100%，默认 25%；用驱动回读验证结果，拒绝抢占已被外部使用的 PWM6。
- 服务停止时关灯；异常退出后的 ExecStopPost 也会关闭并 unexport 本服务拥有的通道，失败时保留所有权标记供重试。

## 实机证据

| 检查 | 结果 |
| --- | --- |
| 云端开灯 | 09:59:48 `self.light.set_power(enabled=true)`，isError=false；period=10000，duty_cycle=2500，enable=1 |
| 云端调光与查询 | 10:00:08 set_brightness(20) + get_status，均成功；duty_cycle=2000，enable=1 |
| 云端关灯 | 10:00:25 set_power(false) 成功；duty_cycle=0，enable=0 |
| 视觉配置 | 10:07:11 initialize 接收实际服务端 capability；去敏日志 configured=true |
| 云端摄像头状态 | 10:07:20 get_status 成功 |
| 真实拍照与视觉分析 | 10:07:45 take_photo 请求，10:07:47 isError=false；随后语音描述电脑、收纳包、纸卷、鼠标和喷壶等物品 |
| 拍照后的语音回归 | 10:08:08 回复“测试成功”，随后回到下一轮 listening |
| 与录音共存 | 拍照前后同一服务进程采集计数增长，KWS 保持就绪；采集/显示没有需要重启才能恢复的故障 |
| 显示管线 | 测试中的 wake、camera status、photo 和后续回复均取得 640×480 合成帧 |
| 异常退出关灯 | 独立测试进程打开 25% 灯后用 os._exit(23) 模拟无析构退出，确认通道/标记残留；运行真实停止钩子后两者消失 |
| 异常退出后重新控制 | 新实例成功再次开灯 25%，清理后通道/标记再次消失 |
| 主机与板端测试 | 最终 `python -m unittest discover -v`：Windows Python 3.9 和板端 Python 3.13 均 25 项通过 |
| 静态检查 | Python 编译、deploy.sh Bash 语法和 git diff --check 通过 |

这次摄像头验收不再只是独立 Camera 示例：运行中的小智通过真实云端 MCP 调用完成拍摄/上传/回答，并继续语音会话。指令音频采用固定 WAV 注入，不能据此估计真人唤醒率或声学识别准确率。

## 实测修正

1. 初版仅接受 HTTPS，实际官方云端下发了 HTTP vision URL，导致本地拒绝配置。先按本次会话真实提供的主机/路径测试 HTTPS：HEAD 返回 404，而携带正确身份头的 multipart POST 返回 200、success=true，并正确描述合成纯蓝测试图。因此仅为实际验证过的 `api.xiaozhi.me` 默认 HTTP 端口添加 HTTPS 升级，保留原路径/query与服务器 token；没有猜测 endpoint，也没有降级明文上传。
2. 独立审查发现 stop hook 仅熄灯未 unexport 会造成异常退出后永久占用。先用两项回归测试复现，再修复，并完成实际 sysfs 遗留/清理/重新开启测试。
3. 摄像头尚未上传时，状态现在返回 `last_upload_ok=null`，避免将“尚未尝试”误报为失败。

## 修改文件

| 文件 | 变更 |
| --- | --- |
| `xiaozhi/service/hardware_tools.py` | 新增相机、HTTPS 视觉上传、灯控制和独立停止钩子 |
| `xiaozhi/service/xiaozhi_service.py` | 工具注册、严格参数验证、异步回调、会话任务生命周期、vision 配置与去敏日志 |
| `xiaozhi/service/xiaozhi.service` | 增加 ExecStopPost 关灯兜底 |
| `xiaozhi/deploy.sh` | 部署时复制新增模块 |
| `tests/` | 25 项协议、硬件边界、失败处理、并发、凭据与恢复行为测试 |
| README 与 `docs/` | 工具清单、设计计划、部署与验证记录 |

硬件操作放入一个职责明确的模块，原语音和 UI 逻辑保持原结构，没有新增第三方依赖。

## 部署和恢复

当前设备已更新服务文件、新模块和 unit；launcher 原进程保持运行。原文件与设备配置备份位于：

`/root/xiaozhi_backups/camera-light-20261025-095845/`

备份留在设备，目录仅 root 可访问，配置副本为 0600；没有下载配置或凭据到仓库。

升级已有设备必须同时部署 `hardware_tools.py`、`xiaozhi_service.py` 和新版 `xiaozhi.service`，执行 daemon-reload 后重启小智。`deploy.sh --keep` 按既有约定不更新 systemd，因此不能用它单独完成首次接入升级。无需为本功能重新编译注入库或重启 launcher。

需要回退时先停止小智（执行新停止钩子），恢复备份中的服务源码和 unit，daemon-reload 后启动。原配置默认保留；不要用旧配置覆盖用户后续更改。新增模块可保留为未被旧服务引用的文件。

最终保持用户原设置：enabled=false、popup_enabled=true、touch_wake=true、volume=100；补光灯关闭，服务正常运行。使用前在设置 App 打开“启动小智AI”。

## 证据与限制

私有测试材料位于本地 `.runtime/camera-light-mcp/`，已被本仓库本地排除规则忽略。包括 `cloud-results-first.json`（含初次 HTTPS-only 拒绝记录）、`camera-results.json`、`crash-recovery-results.json`、部署结果与测试脚本；不随源码提交。设备临时测试目录为 `/tmp/xiaozhi-mcp-stage-20261025/` 和 `/tmp/xiaozhi-mcp-live-20261025/`。

尚未做长期连续拍摄、断电循环、所有相机型号、网络持续慢速响应或噪声环境性能测试。摄像头驱动初始化/拍摄的 GIL 持有行为无法仅靠线程保证任意情况下无延迟；本次实际共存测试通过。项目原有配置文件空闲重复写入问题仍在，未纳入本次功能修改。

协议依据：[官方 MCP 工具和 capability 处理](https://github.com/78/xiaozhi-esp32/blob/f443114fe7c3fa26b10d3630090119891cd1c4fd/main/mcp_server.cc)、[官方摄像头上传实现](https://github.com/78/xiaozhi-esp32/blob/f443114fe7c3fa26b10d3630090119891cd1c4fd/main/boards/common/esp32_camera.cc)、[py-xiaozhi v2.1.2](https://github.com/huangjunsen0406/py-xiaozhi/blob/v2.1.2/src/mcp/tools/camera/normal_camera.py)。
