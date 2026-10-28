# 当前设备的小智 MCP 功能清单

核对日期：2026-10-25。设备：MaixCAM2 `maixcam2-test-device`。板载外设接入版本已部署，当前共 29 个工具。

这29项尚未全部完成物理外设验证，也未覆盖整机所有硬件能力，详见 [硬件MCP覆盖检查](HARDWARE_MCP_COVERAGE.md)。

## 调用方式

云端小智通过现有 WebSocket 连接发送 `type: mcp` 消息，设备上的 `xiaozhi_service.py` 内置 `McpServer` 解析 JSON-RPC 请求，再调用注册工具的回调。

这不是另外启动的通用 HTTP MCP 服务。当前程序支持 `initialize`、`tools/list`、`tools/call`；`initialize` 中声明的协议版本为 `2024-11-05`、服务名为 `xiaozhi-maixcam2`。

注册位置：`xiaozhi/service/xiaozhi_service.py` 的 `MCP_TOOLS`；分发位置：同文件 `McpServer`。硬件与视觉上传逻辑在 `xiaozhi/service/hardware_tools.py`。

## 原有 10 个工具

下表保留前轮 10 项工具的实测记录；其中音量 35/40 为当时测试值，当前保留用户设置音量 100。拍照后普通语音问答回归通过。

| 工具名 | 输入 | 当前实现与返回含义 | 本轮验证 |
| --- | --- | --- | --- |
| `self.audio_speaker.set_volume` | 必填整数 `volume`，0–100 | 保存配置、取消静音，仅缩放待播放PCM，不修改声卡输入增益 | 云端设置/查询通过；后续静音隔离及真实讲话复测通过，见音频修复记录 |
| `self.audio_speaker.get_volume` | 无，`{}` | 返回当前配置中的音量数值 | 云端实际调用，回复当前音量 35% |
| `self.audio_speaker.get_volume_status` | 无，`{}` | 返回 `volume`、`muted`、`available`；其中 `available` 是实现固定返回的 true，不是实时硬件自检 | 云端实际调用，回复未静音、音量 40 |
| `self.get_device_status` | 无，`{}` | 返回 `state`、`network`、`activated`、`volume`、`muted`、`wake_word`、`uptime` | 云端实际调用，回复网络可用、设备已激活 |
| `self.display.show_message` | 必填字符串 `message` | 在聊天弹窗追加系统消息，触发 RGBA 重绘；返回 `ok` | 云端实际调用 `{ "message": "硬件测试成功" }`，合成帧及用户现场确认一致 |
| `self.camera.take_photo` | 必填字符串 `question`，1–2000 字符，不能全空白 | 按需拍摄 640×480 JPEG；同一张照片在屏幕预览 3 秒，同时经 HTTPS 视觉分析，返回结果 | 实际拍照、上传、识别成功；预览不等待上传完成 |
| `self.camera.get_status` | 无，`{}` | 返回视觉服务配置状态、拍摄是否进行中、最近图片字节数及上传状态；`last_upload_ok=null` 表示尚未上传 | 实际调用成功，确认视觉服务已配置 |
| `self.light.set_power` | 必填布尔值 `enabled` | 开/关补光灯；首次开启 25%，随后恢复最近非零亮度 | 实际开灯/关灯成功，PWM 回读一致 |
| `self.light.set_brightness` | 必填整数 `brightness`，0–100 | 调整补光灯，0 关闭；与屏幕背光无关 | 实际调到 20%，duty_cycle/period=2000/10000 |
| `self.light.get_status` | 无，`{}` | 回读补光灯开关、亮度和通道所有权；外部占用时不假报关闭 | 实际调用成功，返回已开启、亮度 20 |

前轮工具定义通过 AST 提取，本轮另有 10 项工具注册的离线测试与部署文件哈希比对。新增工具的 `[mcp] call ...` 和 `done ... isError=False` 日志，以及驱动回读/视觉语音回复构成运行证据。

`tools/call` 的 JSON-RPC payload 示例（实际由既有 WebSocket 传输，不是可直接访问的 HTTP 地址）：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "self.display.show_message",
    "arguments": { "message": "硬件测试成功" }
  }
}
```

## 后续新增 19 个工具（含3项陀螺仪校准接口）

详细证据见 [板载外设阶段验收](PERIPHERAL_MCP_VALIDATION.md)。下表中“云端成功”指实际云端 MCP 调用返回成功，不等于每个物理量均已标定。

| 工具名 | 输入 | 返回含义与验证边界 |
| --- | --- | --- |
| `self.led.set_power` | `enabled` 布尔 | 用户 LED 开关，保存原状态；云端开/关成功 |
| `self.led.get_status` | 无 | 亮度、trigger、控制状态；云端成功 |
| `self.led.restore_system` | 无 | 恢复原系统指示状态；云端成功，异常退出恢复另有实测 |
| `self.buttons.get_state` | 无 | 支持的按键码、当前按下码；云端成功，只读 |
| `self.buttons.get_events` | `limit` 整数 1–64，默认 10 | 最近事件、序号、溢出/同步状态；云端成功，真实 2 次按下/释放通过 |
| `self.imu.read` | 无 | XYZ 加速度 m/s²、角速度 °/s；云端及姿态变化通过；实时大面板3秒后转底栏，支持零偏校准，温度 null |
| `self.imu.calibrate_gyro` | 无 | 静止3秒校准并保存；板端及云端语音调用通过 |
| `self.imu.get_calibration` | 无 | 查询零偏、时间、样本数；真实板端MCP分发通过 |
| `self.imu.clear_calibration` | 无 | 清除自有零偏；真实板端MCP分发通过，最终已重新校准 |
| `self.rtc.get_time` | 无 | RTC UTC、epoch、系统时间差；云端成功，不校时 |
| `self.adc.read_raw` | `channel` 整数 0–3 | ADC 原始 count；云端成功，不换算电压 |
| `self.io.list_resources` | 无 | 策略逻辑目标列表及配置状态；云端成功，当前为空，hardware_verified=false |
| `self.gpio.read` | `target` 字符串 | 读取策略目标 0/1；当前未开放，未知 mux 拒绝已验证 |
| `self.gpio.write` | `target`、`value` 整数 0/1 | 写入已配置输出目标；当前未开放，无外接设备验收 |
| `self.i2c.read_register` | `target`、`register` 0–255、`length` 1–256 | 白名单连续寄存器读取，不能越过 8-bit 地址范围；当前未开放，未知 mux 拒绝已验证 |
| `self.i2c.write_register` | `target`、`register` 0–255、`data_hex` | 白名单写入 1–256 字节，不自动重试；当前未开放，无外接设备验收 |
| `self.spi.transfer` | `target`、`data_hex` | 1–256 字节同步收发；仅 SPI2.1，有限控制器测试返回 ff，无外设通信验收 |
| `self.uart.read` | `target`、`length` 1–256、`timeout_ms` 1–1000（默认 200） | 仅 UART2，短读/超时报错；无对端 100 ms 读取正确失败 |
| `self.uart.write` | `target`、`data_hex` | 仅 UART2，返回接受字节数；控制器接受 1 字节，无对端接收证明 |

新增逻辑位于 `peripheral_tools.py`、`imu_adapter.py`、`io_tools.py`，清理入口为 `service_cleanup.py`。外部 I/O 默认拒绝、配置示例和安全边界见 [IO_ACCESS](IO_ACCESS.md)。SPI/UART 控制器探测使用临时测试目标，不表示部署策略已开放。`tools/list` 支持 `nextCursor` 分页，客户端应读完分页取得全部 29 项。
## MCP 与内部硬件调用的区别

| 功能 | 当前调用路径 |
| --- | --- |
| 麦克风采集、唤醒词检测、本地 VAD | 服务内部直接调用 MaixPy / sherpa-onnx / 音频处理，没有对应 MCP 工具 |
| 云端语音播放 | WebSocket 下行音频 → Opus 解码 → 重采样 → PyAudio，不需要额外 MCP 工具 |
| 音量调整 | MCP 回调 → 配置 → 播放PCM幅度缩放；麦克风增益独立 |
| 聊天文字/状态显示 | 内部状态更新或 MCP 显示回调 → Pillow RGBA → launcher 注入层 |
| 弹窗触摸、静音、关闭 | Linux 触摸事件 → 注入层 → `/tmp/xz_cmd` → 服务回调，不是 MCP 工具 |
| 摄像头 | MCP → 同进程工作线程按需拍摄/关闭摄像头 → HTTPS 视觉分析 → MCP 文本结果 → 小智语音回复 |
| 补光灯/闪光灯 | MCP → MaixPy B25/PWM6 → 驱动回读；服务停止时关闭并释放通道 |

拍摄只在工具调用时发生，不常驻采集或保存照片。服务端 `initialize.params.capabilities.vision` 提供 URL/token；token 仅在内存中存在。官方云端本次提供 HTTP 地址，经实测确认同主机 HTTPS 可用后，仅对 `api.xiaozhi.me` 的默认 HTTP 端口升级 HTTPS；其他明文 HTTP 地址仍拒绝，保留证书验证且不跟随重定向。

成像稳定修复：每次打开相机后启用自动曝光/白平衡，至少读取30帧且等待2秒再保存，避免启动初期照片发白或偏色。取图会增加约1–2秒等待，详见 [拍照色彩修复记录](CAMERA_COLOR_FIX.md)。

语音示例：唤醒后说“打开补光灯”“补光灯调到百分之二十”“关闭补光灯”“看看眼前有什么”。当前设备按用户最新设置保持 `enabled=true`。

显示反馈扩展：`tools/call` 执行时，底部显示完整 MCP 工具名称；结束后保留 3 秒。照片预览等比例显示、3 秒后恢复聊天；连续拍照重新计时。它们遵守现有弹窗开关，关闭弹窗会立即收起。详见 [显示反馈验收记录](PHOTO_PREVIEW_MCP_FEEDBACK.md)。

接入与验收见 [摄像头/补光灯接入记录](CAMERA_LIGHT_MCP_VALIDATION.md)；此前的 [硬件基础测试](HARDWARE_TEST_2026-10-25.md) 是接入前快照。


实时显示与校准验收见 [IMU 实时与校准](IMU_LIVE_CALIBRATION.md)。
