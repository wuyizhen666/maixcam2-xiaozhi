# SIoT 图像、视频与 MaixCAM2 摄像头传输

更新：2026-10-26。本文描述当前实现与已取得的证据。本机摄像头、MQTT 连续 JPEG、小智拍照/云识别转发及 RTSP 接收已有真机数据，用户已确认当前 Mind+ 版本的 Data URL 连续图片正常显示。没有第二台 MaixCAM2 LCD 或 ESP32 显示联调，也尚未证明达到端到端延迟不超过 100 ms 的验收线。

## 页面入口与三种来源

主监控页右侧底部保留四个按钮：“消息”“图表”“图像/视频”“编辑/发送”。媒体入口打开二级页，同一时刻只选择一种来源：

| 来源 | 接收内容 | 实现边界 |
| --- | --- | --- |
| 所选 MQTT 主题 | JPEG/PNG 原始二进制、图片 data URL、裸 Base64、约定 JSON/base64 | 自动识别支持的格式，只解码当前选定主题；采用最新帧队列，长度为 1 |
| 局域网收发 | 接收 HTTP/HTTPS 单张 JPEG/PNG、HTTP MJPEG、RTSP；发送端共享本机摄像头 HTTP/MJPEG | 进入三级页选择接收端或发送端；HTTP 读取在可终止子进程，RTSP 使用设备已有 ffmpeg |
| 本机摄像头 | MaixCAM2 320×240 RGB 画面与 JPEG | 独立采集进程，自动曝光/白平衡预热至少 30 帧且 2 秒；目标 10 fps |

HTTP 单张图片不会自动变成视频；连续画面需要 MJPEG、RTSP 或不断到达的主题图片。RTSP 不播放音频，解码输出限制在适合界面的尺寸与帧率。切换来源或退出媒体页时先停止向外发送，再停止接收/解码并释放摄像头，避免旧来源继续占用资源。

进入“本机摄像头”后先选择 JSON 或 Base64 格式，以及 2 / 5 / 10 fps 按钮，默认目标为 5 fps。两种格式均可独立配置当前发送会话的速率；切换格式或速率先停止旧会话，再应用新设置。本机摄像头页面可修改发送主题、发送单帧或开启/停止连续发送，默认主题为 `账号/camera`。修改主题也会停止连续发送；未连接或画面未就绪时显示状态，不把空帧当作成功。

“Base64：Mind+ 网络图片”发送的是完整 Data URL：`data:image/jpeg;base64,` 加 JPEG 的 Base64 字符串，并非裸 Base64。当前用户使用的 Mind+ 版本实测裸 Base64 破图，带前缀单帧实显后，用户确认连续画面正常。MaixCAM2 接收器仍能自动识别裸 Base64，这是接收兼容能力，不代表该 Mind+ 版本接受裸格式。

## 局域网接收端与 HTTP 发送端

依次进入“图像/视频 → 局域网收发”：

- **接收端**：填写完整 HTTP 图片、MJPEG 或 RTSP URL。使用另一台设备 A 的共享画面时，填写 A 的真实 LAN 地址，例如 `http://192.168.1.20:8090/stream.mjpg`；不能把接收设备自己的 `127.0.0.1` 当作 A。
- **发送端**：显式点击“共享本机摄像头”后才启动 HTTP 服务与摄像头，默认端口 `8090`。页面显示可复制使用的完整地址；`/etc/siot/monitor.json` 的 `media_http_port` 可调整端口。退出来源、停止共享或退出 App 时停止服务器与摄像头。

| 路径 | 用途 |
| --- | --- |
| `/` | 普通浏览器可打开的实时预览网页 |
| `/snapshot.jpg` | 当前原始 JPEG 快照，不经过网页截图或重新编码 |
| `/stream.mjpg` | 连续 JPEG 的 MJPEG 流，可给另一台监控接收端 |
| `/status.json` | 是否就绪、帧序号、尺寸、最近画面时间等状态 |

服务只读，不提供远程控制、录像、持久存储或 RTSP server；最多 4 个媒体请求、8 个总连接，连续帧上限 10 fps。它复用现有摄像头快照，不再额外打开第二个摄像头。默认监听可达的网络接口，开启后同网络设备可访问画面；它不复用 SIoT 登录认证，因此应只在需要共享时显式开启。

浏览器的 `http://A地址:8090/` 是实时媒体页面；`http://A地址:8080/` 是 SIoT 管理和数据库历史页面，不能用其历史列表判断 QoS 0 连续帧是否正在发送。局域网 HTTP 共享不需要 MQTT 主题；MQTT 摄像头发送与云识别转发是另外两条通道。

## A 发、B 收，无需 PC 服务器

两端必须连接**同一个 MQTT broker**并使用完全相同的主题。SIoT 服务器可以部署在 MaixCAM2 A 上：

1. A 的监控配置连接自身 `127.0.0.1:1883`，在“本机摄像头”选择主题，例如 `alice/camera`，再开启发送。
2. B 的 `/etc/siot/monitor.json` 中 `host` 改为 A 的实际局域网 IP，端口保持 `1883`，填写该服务器有效凭据。B 不能使用 `127.0.0.1` 指向 A。
3. B 在主题列表选择同一个 `alice/camera`，进入“图像/视频 → 所选主题”，等待 JPEG 帧到达。需要时刷新主题列表或在服务器登记该主题；媒体发布器本身不调用主题登记或历史保存 API。

此链路只要求两台设备同局域网、A 持续供电并运行 broker，不依赖 PC 或互联网。云端小智对话仍需要外网。设备 B 可以是其他 MQTT 客户端，但必须实现下述图片解析与显示；普通 `siot.py` 字符串消息客户端不会天然显示 JPEG，ESP32 还需自行适配内存、JPEG 解码器和屏幕驱动。

主题必须与来源一致：若账号为 `siot`，本机 MQTT 连续摄像头默认发 `siot/camera`，小智云识别图片默认发 `siot/vision/image`，识别文字结果发 `siot/vision/result`。打开后两个主题不会收到前一个主题的连续帧，反之亦然；QoS 0 图片帧也不请求 SIoT 历史保存。

## 摄像头 JSON 信封

`media_publisher.py` 使用独立的 Paho MQTT 连接。选择 JSON 模式时发送 UTF-8 JSON，完整字段为：

| 字段 | 含义 |
| --- | --- |
| `type` | 固定 `image` |
| `mime` | 摄像头发送固定 `image/jpeg`；接收器也接受 `image/png` |
| `encoding` | 固定 `base64` |
| `sequence` | 当前采集会话内帧序号；不是跨重启永久消息 ID |
| `timestamp` | 采集端 Unix 秒时间，可带小数；不是接收端时间 |
| `width` / `height` | JPEG 原图尺寸，目前为 320 / 240 |
| `data` | 原始 JPEG 字节的 base64 ASCII 字符串 |

以下仅展示格式，`data` 是占位文字，不能作为有效图片发送：

```json
{
  "type": "image",
  "mime": "image/jpeg",
  "encoding": "base64",
  "sequence": 42,
  "timestamp": 1700000000.25,
  "width": 320,
  "height": 240,
  "data": "<base64-encoded JPEG bytes>"
}
```

发送前会检查 JPEG 格式与描述尺寸一致。接收器校验图片内容、MIME、格式、像素数及编码，不把 `data` 当作文件路径，也不根据任意 JSON 字段执行操作。data URL 形式为 `data:image/jpeg;base64,...` 或 `data:image/png;base64,...`；原始二进制形式则直接以 MQTT payload 携带 JPEG/PNG 字节。

## 限流、丢帧与大小限制

- 摄像头预览目标 10 fps；界面可选发送目标 2 / 5 / 10 fps，默认 5 fps。实际速度取决于采集、编码、网络和接收端解码。`MediaPublisher` 是唯一限速者；`MediaSession` 每遇新帧即更新最新帧槽，不再重复限速。
- `MediaPublisher` 使用独立 MQTT 客户端，固定 QoS 0、`retain=False`，不请求历史保存。它与文本编辑器的“保存”开关无关。
- 发送端最多保留一个正在提交的包与一个可替换的最新帧。新帧覆盖待发旧帧，断线丢弃待发帧，停止后重开使用新的客户端，不补发上一会话积压画面。
- 接收端媒体队列长度为 1，忙时丢旧帧保留新帧。此策略优先当前画面，不保证每帧送达；不适合作为录像或逐帧取证通道。
- 连续摄像头发布器的原始 JPEG 上限为 **512 KiB**；小智拍照桥接允许原始 JPEG 最大 **2 MiB**、完整图片信封最大 **3 MiB**。base64 会额外增加约三分之一体积。媒体接收器允许完整 MQTT payload 最大 **3 MiB**，包含 JSON/data URL 包装开销；解码后图片仍不能超过 2 MiB、400 万像素，且仅接受单帧 JPEG/PNG。
- 接收图像按比例缩小至不超过 400×280 以适配界面。发送端“已提交”计数仅表示本地 QoS 0 socket 提交完成，不能代替另一台设备收到或屏幕显示的证明。

实时摄像头 JPEG 始终为 320×240、quality 75，本轮优化没有降低发送图像质量。本机 LCD 放大预览改用 NEAREST，仅影响屏幕缩放，不改变发出的 JPEG；小智拍照仍是 640×480，识别前后发送同一份原 JPEG。

## 小智拍照与云识别转发

`xiaozhi/service/vision_siot.py` 已接入原有拍照 MCP。拍到原始 JPEG 后，先向图片主题发布一次；云端识别结束后，再用**同一 `capture_id`、同一份原始 JPEG 字节**发布带 `recognition` 的图片更新，同时向结果主题发布 `vision_result`。网络发布由独立工作线程完成，原拍照 MCP 不等待 SIoT 发送确认，也不改变原来的云识别返回值；配置缺失或 SIoT 失败不应使原云识别调用失败。

默认主题为 `username/vision/image` 与 `username/vision/result`，可在 `/etc/siot/monitor.json` 配置 `vision_image_topic`、`vision_result_topic`。小智服务配置 `siot_vision_publish: false` 可关闭该转发。不要将此功能误认为已经开放了通用 SIoT 发布/订阅 MCP。

图片信封沿用 `type`、`mime`、`encoding`、`timestamp`、`width`、`height`、`data`，另带 `capture_id`、`question`、`status`；云端完成后的更新新增 `recognition`。它使用 `capture_id` 关联照片，不使用连续摄像头的 `sequence` 字段。结果主题格式如下：

```json
{
  "type": "vision_result",
  "capture_id": "example-capture-id",
  "question": "请描述画面",
  "text": "云端返回的完整识别文字",
  "status": "complete"
}
```

拍摄时图片状态为 `captured`，云端结束后为 `complete` 或 `error`。图片用 QoS 0、结果用 QoS 1，均为 `retain=False`；结果的 broker ACK 不代表数据库保存或另一台设备已经显示。桥接是有界的尽力转发，离线、队列满、图片缓存过期等情况可能导致缺少某次更新，接收端应按 `capture_id` 和状态处理，而非假设三条消息必然全部到达。

识别文字最大 **256 KiB UTF-8**，合法范围内完整进入 MQTT，不因屏幕显示限制而截断。媒体页预览使用前 512 个字符并最多显示三行，“识别详情”最多显示 4096 UTF-8 字节，超限有截断提示。需要完整内容的客户端应读取原 MQTT 字段；屏幕缩略图与文字预览不是原始数据的替代品。

## 复用接收解码器

设备已经具备 Pillow、Paho MQTT 2.x，可直接复用 App 内的 `decode_payload`。下面仅演示已有 MQTT 回调如何解析，不安装依赖、不展示凭据，也不自动写入图片文件：

```python
import sys
sys.path.insert(0, "/maixapp/apps/siot_monitor")
from media_viewer import decode_payload

def on_message(client, userdata, message):
    try:
        frame = decode_payload(message.payload)  # Pillow RGB 图像，按界面尺寸缩小
    except ValueError:
        print("收到的消息不是可用图片")
        return
    print("已解码尺寸:", frame.size)
    # 将 frame 交给接收设备自己的显示线程。
```

该小例子只证明解码入口，不是完整的视频播放器。连续流应采用 App 已有的 `MediaViewer.start_topic()` / `submit_topic()` 与独立解码线程，避免在 MQTT 网络回调中执行耗时解码或直接绘屏。

## 摄像头共存与隐私

`camera_ownership.py` 通过 `/run/maixcam2-camera.lock` 协作互斥本机预览和小智拍照；进程退出后内核释放锁。被占用时报告忙，需要先停止另一项采集。第三方原生相机 App 没有参与该锁，不能假设它们自动协调，也没有增加系统级相机复用能力。

摄像头帧保存在私有、易失的 `/run/siot-camera-*` 目录，正常停止后清理。界面预览仍可本地暂存于 `/tmp/siot-monitor/preview.jpg`。相机帧、截图、实际主题内容、HTTP/RTSP URL 中的账号密码及连接凭据均不纳入 Git 或公开日志。网络媒体错误仅对用户显示概要，不打印完整地址；含凭据地址仍可能存在于本机进程参数中，因此也不要公开原始进程诊断输出。

可选渲染诊断仅在本机 `/tmp/siot-monitor-profile` 标记存在时采集 30 帧，输出本地 `/tmp/siot-monitor/render-profile.json` 摘要后移除标记。默认不持续采样，也不上传截图或帧内容。

## 清空数据与部署

“清空数据”经完整主题名二次确认后调用历史删除接口，保留主题 ID，清掉已有历史与旧缓存，新消息继续接收。它不关闭发布端、不停止后续数据，也不会删除主题。媒体流自身不请求历史存储，持续发送时不需要频繁清空历史。

安装器现在要求同时核对服务与硬件适配 SHA-256，并同步 `xiaozhi_service.py`、`hardware_tools.py`、`ui_ownership.py`、`camera_ownership.py`、`vision_siot.py` 以及完整媒体 App 文件（含新增 `lan_camera_server.py`）；`xiaozhi/deploy.sh` 也包含桥接模块。回滚需恢复同次备份的配套文件，不能只回退一侧摄像头锁或留下缺少桥接模块的服务。安装命令、备份和启动顺序见 [部署说明](SIOT_DEPLOYMENT.md)。

## 当前实测与仍待验证

- 本机 320×240 摄像头初始观察窗口中，报告采集帧率为 9.92–10.92 fps；独立 MQTT 订阅者收到 25 帧，收包速率约 4.914 fps，解码尺寸均为 320×240。不是两台屏幕间的显示帧率或光学延迟测量。
- 拍照锁阻止与本机预览冲突；停止后锁释放，再次打开成功，小智服务 PID 保持不变。真人语音唤醒与连续画面的共存仍需现场验证。
- 服务中已有 `photo.wav` 被注入现有语音路径，实际经过 MCP、摄像头和云识别：JPEG 为 640×480、59098 字节，前后两次图片 SHA 相同、`capture_id` 一致；118 字识别结果在图片的 `recognition` 与结果主题的 `text` 中匹配。这不是第二台 MaixCAM2 LCD 联调，也不能代替真人麦克风测试。
- HTTP 图片与 MJPEG 的板端测试已通过。RTSP 已使用 Maix RTSP 摄像头源 `127.0.0.1:18554` 经 ffmpeg 解码，成功得到 10 次 373×280 JPEG；这是本机实际源测试，不是第二台设备显示联调。板端 ffmpeg 不支持 `rw_timeout`，接收命令已移除该选项，仍由父进程 watchdog 限定超时并终止失效连接。
- Mind+ 当前版本的完整 Data URL 单帧与连续图片已由用户确认显示正常。当前设备配置为 Base64 模式、10 fps 目标连续发送；8 秒独立订阅窗口收到 71 帧，约 8.889 fps，无坏帧。另一 5 fps 目标窗口实收约 4.861 fps。这些是有限窗口的收包结果，不是长期保证或 LCD 帧率。
- LAN 发送端已通过真实摄像头到 HTTP 验证：快照为 320×240，MJPEG 接收解码连续更新 15 次并进入 streaming，停止后端口与摄像头均释放。实测地址为 `http://192.168.50.120:8090/snapshot.jpg` 与 `/stream.mjpg`，地址随设备网络配置变化。
- 已在原生界面实际点击“图像/视频 → 局域网收发 → 发送端”，真机页面显示现场画面及三个 URL。PC 跨 LAN 请求 `/status.json` 得到 `ready=true`，请求快照得到 16132 字节、320×240 JPEG。这是发送端及单 PC 跨 LAN 取图证据。
- 原生接收端手工输入 URL 的完整界面流程尚未独立验证。浏览器打开请求仅返回 queued，不算网页视觉验收；上述工具取图和原生发送页也不能代替第二台 MaixCAM2 LCD 联调。

### 性能测量的层次

UI 已加入字体适配结果缓存、静态中文标签拆分、快速缩放；帧率门控以绘制开始时刻计时，媒体循环等待为 10 ms。不同优化阶段的绘制均值约为 128 → 87 → 36.9 ms，最后观察到转换约 11.8 ms、`show` 调用约 6.9 ms，软件提交速率从约 4.02 提升到 9.30 fps。这些是软件计时，不是 LCD 实际刷新率或光学延迟。

用户同屏秒表提供了不同设置下的单个样本：2 fps 约 500 ms、5 fps 约 310 ms、10 fps 约 210 ms。它们不是平均值或 p95，不足以证明稳定分布，更没有证明 ≤100 ms。随后一次 20 秒自动光学采样收到 0 帧，当时存在停发/摆放变化，因此没有形成可用延迟统计，不能当作有效性能样本。

相机已部署有界 drain helper：每轮最多发起 4 次非阻塞读取，并以 8 ms 预算限制是否发起下一次，选择最后可用帧；预算不保证 SDK 调用本身在 8 ms 内结束。真机兼容窗口得到 3 秒 31 帧、约 9.926 fps，停止后释放成功。该改动没有调整 camera fps、`set_fps` 或 `buff_num`，尚未独立证明光学延迟收益。

### 回归快照与剩余验证

最新主机全套 391 项：389 通过、2 项平台专用跳过。板端历次有 232 项集成、195 项 SIoT，以及定向 publisher 20 / session 13 / UI 19 / camera 20 项通过；这些来自不同代码快照和测试范围，不能相加，也不能称为 391 项已全部在板端运行。

一次恢复摄像头时出现 `Buffer empty`；改为 30 fps 或使用旧 640 路径也失败。停止全部多媒体进程后按“桌面先、音频后”重建，观察到 pools 从 57 清为 0，桌面启动后为 7、摄像头结束后为 8，采集恢复。这支持资源累积相关的诊断方向。退出生命周期已增加 GC 清理，但尚未证明永久解决 SDK 资源累积或长期 pool 回收问题，不能把重建后恢复写成彻底修复。

原生 LAN 接收端手工输入 URL 的完整流程、浏览器网页视觉、清空数据的最终板端闭环、图片识别 UI、新版真人语音共存，以及第二台 MaixCAM2/ESP32 显示仍保留验收边界。断电启动、长期负载与 pool 回收、稳定端到端延迟也未验收，当前不标记全部完成。
