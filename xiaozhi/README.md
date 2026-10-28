# 小智AI for MaixCAM2 v2

本地扩展现有29个MCP工具：包含摄像头、补光灯、板载外设、IMU实时显示/陀螺仪校准与受控I/O。摄像头按需拍摄并预览3秒；补光灯使用B25/PWM6；外部目标默认关闭。接口与证据见 [MCP 清单](../docs/MCP_CAPABILITIES.md)、[开发汇总](../docs/DEVELOPMENT_SUMMARY_2026-10-25.md) 和 [访问配置](../docs/IO_ACCESS.md)。

当前触摸唤醒为左上圆标持续2秒，普通触屏不触发；X结束当前会话。静音只影响播放PCM，麦克风继续工作。以下为上游历史设计记录，与上述变更冲突时以当前代码及当日验收为准。

设备：`root@<device-ip>`（maixcam2-test-device，Ubuntu 22.04 / MaixPy 4.12.5 / Python 3.13）

## 一、这一版修好了什么

旧版「不能用」的根因（已用抓包 + 对照实验定位）：

| 症状 | 根因 | 修复 |
|---|---|---|
| 唤醒后一直「聆听中」，服务器从不回话 | 旧版发 `listen start mode="auto"` 后**只上行、从不断句**。实测 `api.tenclass.net` 的 WS 端**不会**自己做 VAD——连续上行 5 分钟也不返回 `stt`/`tts` | 改为 `mode="manual"` + **本地能量 VAD**：检测到说话结束后发 `listen stop`，服务器立刻返回 `stt/llm/tts` |
| 上行序号错乱、声音卡顿 | 旧版用列表绝对下标消费环形缓冲，裁剪后永久卡死 | 改为**单调序号 + 生产者/消费者同步**，积压超 1s 自动跳帧到准实时 |
| 采集线程死掉（`mic_blocks` 冻在 63 万） | `Recorder` 偶发失效无自救 | 加采集看门狗（**不在运行期重建** `Recorder`，见第四节） |
| TTS 无声 | 服务器固定 24k 输出，codec 只支持 16k/48k | 按 **Opus TOC 字节**解析每包帧长（20/40/60ms 自适应），再 24k→48k 重采样播放 |
| 打断 / 多轮不可用 | 没有轮次状态机 | `LISTENING / WAITING / SPEAKING` 三态机 + `tts stop` 后自动 `listen start`，支持连续多轮 |
| **开机黑屏（可复现）** | `xiaozhi.service` 抢在 `launcher.service` 之前初始化了 maix 多媒体栈，launcher 首帧 DRM atomic commit 永久阻塞在 `drm_atomic_helper_wait_for_vblanks` | unit 加 `After=launcher.service`，并用 `ExecStartPre` 等到 launcher 打开 `/dev/dri/card0` 后再启动 |
| **TTS 播放一顿一顿** | **根因：`maix.audio.Recorder` 阻塞模式的 pybind 绑定持 GIL 约 20ms**，把整个进程量化成 20ms 一跳 → 事件循环收包成簇 → 播放被抽干。A/B 实测：`block=True` 主线程 10ms 定时超出 **10.0ms**；`block=False` 仅 **0.1ms**，采集量不变 | `Recorder(..., block=False)`；播放线程成块写 100ms；播放期间跳过上行编码；抖动缓冲按「虚拟缓冲」实测需求自适应（350~1000ms） |
| **叉掉弹窗后自己又弹出来** | 关闭弹窗的那次触摸同时被 overlay 和 `maix.touchscreen` 看到，后者当成「触摸唤醒」又把窗口拉起来 | 历史版本先通过来源标记和独占触摸防重触；当前已改为坐标转换、X结束会话及圆标长按2秒，普通触屏不唤醒 |
| **弹窗盖住了桌面却还能滑动** | overlay 只拦截自己 `open()` 的那几个 evdev fd，launcher 自己开的 fd 号不同 → 完全漏网 | 按设备号识别（`fstat` + `major==13`）而非 fd 号；并加 `EVIOCGRAB` 独占触摸设备，与读取方式无关 |


### 2026-10-25 后续补充修复

完整根因和验证见[主README“踩过的坑”](../README.md#四踩过的坑已修复问题与验证边界)，避免将历史防抖方案误认为当前行为。

| 已解决问题 | 当前处理 |
|---|---|
| X点不到、被误判为唤醒 | 原始竖屏坐标转横屏；关闭与喇叭热区分开，X结束当前会话 |
| 全屏误唤醒、指定长按无效 | 左上圆标热区连续2秒；读取实际MT槽位和按压状态，覆盖已记录的边缘触点 |
| 关闭后的再次语音唤醒 | 解码命中立即reset，本地KWS恢复先于网络关闭；真实声音复测通过 |
| 静音喇叭后麦克风近乎无声 | 不再修改RX输入增益，仅缩放播放PCM；真实对话复测通过 |
| 拍照偏色及预览不刷新 | AE/AWB预热；缓存检查纳秒mtime及inode；同一照片预览3秒 |
| Func影响后台、按键被误拦截 | 移除后台进程默认退出监听，只读按键；拦截限定为真正的触摸设备 |
| IMU零偏、温度占位值、并行轮询 | 静止校准并保存；温度不可用返回null；操作串行化，轮询随界面/会话清理 |

双模型实时性、优化后的实显和外接I/O仍按[后续清单](../docs/NEXT_STEPS.md)验证，不计入上述已完成结论。

## 二、目录结构

```
/root/xiaozhi/                 后台服务（systemd 开机自启）
├── xiaozhi_service.py         OTA激活 / WS协议 / VAD / MCP / 音频 / 主循环
├── ui_render.py               状态栏 + 聊天气泡渲染 → popup.rgba / status.rgba
├── config.json                配置（设置 App 读写，服务 0.25s 轮询热生效）
├── state.json                 运行状态（设置 App 只读显示）
├── models/zh/                 sherpa-onnx KWS 唤醒模型（zipformer-wenetspeech-3.3M）
└── sherpa_rt/lib/             libsherpa-onnx + libonnxruntime

/maixapp/apps/xiaozhi/         设置 App（桌面图标「小智AI」）
/root/uishim/                  libmaixcam2_overlay.so —— LD_PRELOAD 注入 launcher，
                               在 VO::push 里 alpha 混合 status.rgba / popup.rgba，
                               并在弹窗可见时吞掉触摸、把 TAP 写入 /tmp/xz_cmd

/root/xiaozhi_src/             可部署源码（deploy.sh 所在处）
/root/xiaozhi_legacy_<日期>/   改动前的旧版（冻结备份，原样保留）
```

## 三、两个部分的分工

1. **设置 App**（`/maixapp/apps/xiaozhi/main.py`）
   只做「显示状态 + 改配置」：开关「启动小智AI / 弹出聊天窗口」、显示网络状态 /
   激活码 / 控制台 / 唤醒词。写 `config.json`，服务轮询到变更立即生效。

2. **后台服务 + 状态栏/唤醒弹窗**（`systemctl start xiaozhi`）
   开机自启。sherpa-onnx 常驻听唤醒词；命中后弹窗浮现、连服务器、本地 VAD 断句、
   TTS 边收边播；`tts stop` 后自动开下一轮，静默 20s 自动收尾、弹窗消失。

## 四、协议对齐（py-xiaozhi v2.1.2）

* **OTA**：`POST {ota_url}`，头 `Device-Id / Client-Id / Activation-Version: 2`；
  读 `websocket.url` / `websocket.token` / `activation.challenge|code`。
  激活 `POST {ota_url}/activate`，`hmac-sha256(key=hmac_key, msg=challenge)`，
  `200` 成功 / `202` 等用户在 xiaozhi.me 输码（5s 轮询）。
* **WS**：`Authorization: Bearer <token>` / `Protocol-Version: 1`；
  `ping_interval=20 ping_timeout=20 close_timeout=10 open_timeout=5
  max_size=10MB compression=None`。
* **hello**：`{"type":"hello","version":1,"transport":"websocket",
  "features":{"mcp":true},"audio_params":{"format":"opus","sample_rate":16000,
  "channels":1,"frame_duration":20}}`
* **上行**：Opus 16k 单声道 20ms（320 样点），`APPLICATION_VOIP`。
* **下行**：Opus 24k 单声道，帧长由 TOC 解析。
* **MCP**：JSON-RPC 2.0；`initialize` / `tools/list`（按 name 游标分页，8000B 预算）/
  `tools/call`；结果包 `{"content":[{"type":"text","text":...}],"isError":false}`。
  已注册工具：`self.audio_speaker.set_volume` / `get_volume` / `get_volume_status`、
  `self.get_device_status`、`self.display.show_message`。

## 五、常用运维

```sh
journalctl -u xiaozhi -f              # 服务日志
systemctl restart xiaozhi             # 重启服务
systemctl stop xiaozhi                # 临时停用（桌面图标与设置 App 仍在）
touch /root/uishim/DISABLE && systemctl restart launcher   # 关闭弹窗注入
rm /root/uishim/DISABLE && systemctl restart launcher      # 恢复

touch /tmp/xz_call                    # 调试：直接开一轮会话（等同唤醒）
touch /tmp/xz_stop                    # 调试：结束当前会话
echo /path/x.wav > /tmp/xz_playwav    # 调试：用扬声器播放 wav
echo /path/x.wav > /tmp/xz_inwav      # 调试：用 wav 顶替麦克风（含 2s 尾静音）
touch /tmp/ovl_dump_b                 # 抓混合后整屏帧 → /tmp/ovl_snap_b.raw (640x480 BGRA)
```

## 六、重新部署

```sh
scp -r xiaozhi/ root@<设备IP>:/root/xiaozhi_src/
ssh root@<设备IP> 'cd /root/xiaozhi_src && sh deploy.sh'
```

## 七、排障速查

| 现象 | 先看什么 |
|---|---|
| 开机黑屏 | `journalctl -b \| grep "Started \(Launcher\|XiaoZhi\)"` —— launcher 必须在前。若顺序反了，说明 unit 被覆盖了 |
| 黑屏后 launcher 杀不掉 | 该进程有线程卡在 `drm_atomic_helper_wait_for_vblanks`（D 状态，不可中断），**只能重启设备** |
| 屏幕是否真的在刷新 | `touch /tmp/ovl_dump_b`，5s 内出现 `/tmp/ovl_snap_b.raw` = launcher 在推帧。注意：进程里长期存在一个 `drm_atomic_helper_wait_for_vblanks` 的 D 状态线程属正常现象，**不是**黑屏判据 |
| 播放卡顿 | `journalctl -u xiaozhi \| grep -E "本句下行\|播放欠载"`：`本句下行 …× 实时` < 1.0 说明瓶颈在服务端下发节奏；`播放欠载` 计数增长说明客户端缓冲不够（会自动加大） |
| 语音识别不准 | `grep "本地断句"` 看本段上行时长与峰值 rms；再用 `/tmp/xz_playwav` 放固定音频做声学回环对照 |
| 上行被丢 | stats 行里 `丢=` 是**异常**指标（积压来不及发），`主动弃=` 是正常（播放中/非聆听态本来就该丢） |

## 八、已知问题

* 常驻 KWS 唤醒让服务 CPU 约占 1.6 核（A53×4）。功能正常，但发热/功耗偏高，
  后续可把 `KWS_PARAMS.num_threads` 调低或改为分级唤醒策略。
* 服务器偶尔在 `stt/llm` 后不回 `tts`；已加 8s 无响应看门狗自动重开一轮。
* 排查播放卡顿时一度误判为「服务端下发只有 0.6× 实时」——那是被自身的 GIL 阻塞污染
  的测量结果。用**独立最小客户端**（无麦克风/无唤醒/PIL，只连 WS）复测，服务端下行
  其实是 19.9ms 平均、最大 49ms、0.97× 实时，完全正常。
  **结论：怀疑链路问题时，先用最小复现端对照，别在复杂进程里量。**
* 设备 RTC 与系统时间不一致（`date` 比 `hwclock -r` 快 8h），journal 时间戳会误导。
  与本项目无关，但排障时注意按 `uptime`/顺序判断而不是绝对时间。

