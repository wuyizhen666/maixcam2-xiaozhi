# 板载外设 MCP 阶段验收

日期：2026-10-25；设备：MaixCAM2 `maixcam2-test-device`。

本阶段完成当前无外接设备条件下的接入和验证：MCP 注册由 10 项增加到 26 项，板载 LED、按键、IMU、RTC、ADC 与外部 I/O 资源查询已有真实云端调用证据。外部总线接口完成默认拒绝策略、软件测试及有限控制器检查；没有外部设备通信验收，不能据此宣称全硬件所有功能实测通过。

## 实现与证据层次

新增 `peripheral_tools.py`、`imu_adapter.py`、`io_tools.py`、`service_cleanup.py`，注册入口仍为 `xiaozhi/service/xiaozhi_service.py`。工具名称与参数见 [MCP 功能清单](MCP_CAPABILITIES.md)，外部接口策略见 [外部 I/O 开放规则](IO_ACCESS.md)。

| 层次 | 结果 | 证明范围 |
| --- | --- | --- |
| 主机与板端 Python 测试 | 93 项通过 | 参数、权限、生命周期及模拟边界回归；不能替代物理外设验证 |
| 原生测试 | loader、touch 两组通过 | 注入加载与触摸筛选回归 |
| 真实云端 MCP | LED 3 项、buttons 2 项、IMU、RTC、ADC、io.list_resources 成功 | 云端请求、设备回调和返回链路 |
| 板载手动操作 | 2 次 press、2 次 release；IMU XYZ 均有变化 | 真实按键事件与姿态响应，不代表 IMU 标定或精度验收 |
| 按键与语音共存 | 弹窗显示时 2 次 press、2 次 release；PID 63154 保持；mic 1464 → 4380 | 修复后功能键未使该轮服务退出或麦克风停采 |
| LED 异常退出恢复 | 通过 | 本轮异常退出后的恢复路径；不是断电全场景保证 |
| 原功能回归 | 摄像头拍照及语音问答成功 | 本轮原有拍照、语音链路仍可工作 |

本地证据位于忽略目录 `.runtime/peripherals-mcp/`：`cloud-part1.json`、`cloud-part2.json`、`manual-results.json`、`key-results.json`、`led-crash-results.json`、`controller-results.json`。文档仅保留摘要，不纳入凭据、原始用户语音或照片。云端记录包含失败/跳过轮次，不能把整份记录概括为每轮均成功；成功结论取对应工具的成功调用以及重测结果。

## 发现的问题与修复边界

首次测试中 USB 松动由用户确认，失败轮次随后重测。另一次用户观察到瞬时熄屏，随后确认恢复桌面；该观察不等于已定位所有显示异常的原因。

真实按键测试暴露 SDK 默认 Func 退出监听会设置退出标志，导致麦克风停止采集并触发 watchdog 重启。服务在主线程执行 `prepare_background_keys()`，仅移除本进程 SDK 默认退出监听；若存在共享 `/dev/input/powerkey` 节点则拒绝该未经验证配置，避免 SDK 析构影响共享节点。其他进程的按键监听不作修改。板载按键使用独立只读 evdev 监听，不 grab、不注入事件；C 注入层仅 grab 真正的触摸设备。修复后的 `key-results.json` 为 `passed=true`，同时保留 PID 和麦克风计数作为持续运行证据。

LED 控制前保存原 trigger/brightness，开关状态读回、恢复系统模式和异常退出恢复已通过。用户确认状态灯位于内部、无法观察，本轮明确不验收 LED 肉眼亮灭；寄存状态及恢复证据不等于光学亮灭验证。ADC 返回单位 `count`，没有量程/参考电压标定，不换算电压。IMU 返回加速度 m/s²、角速度 °/s，`gyro_calibrated=false`、`temperature_c=null`；RTC 只读 UTC 与系统时间差，不校时。

## 外部 I/O 的实测边界

部署默认策略为 `targets={}`。`io.list_resources` 空列表表示未开放，`hardware_verified=false` 不代表发现或验证了外设。

`controller-results.json` 记录：GPIO A4、I2C A8 当前 pinmux 无法验证，按预期拒绝；SPI 控制器收发 1 字节返回 `ff`；UART 控制器接受写入 1 字节，无对端时 100 ms 读取以短读错误失败。SPI 的 `ff` 和 UART 的写入计数只证明本轮控制器调用结果，不能证明外部器件存在、接线正确或应用协议成功。记录明确标记 `external_device_communication_verified=false`。

后续需接入已确认电气参数的具体外设，在人工确认原有 pinmux、资源占用和协议后配置逻辑目标，再做对端读写/回环和异常测试。本阶段不自动改 mux，不把这些待验证项改写为已通过。

## 保留状态与回退

备份位于设备 `/root/xiaozhi_backups/peripherals-20261025-140911`，按键修复备份位于其 `key-exit-fix` 子目录。

最终快照见 `.runtime/peripherals-mcp/final-verification.json`：注册 26 个工具，列表单页线上编码大小为 6040 bytes；`launcher.service` 与 `xiaozhi.service` 均为 `active/running`、`NRestarts=0`。此计数仅描述最终服务状态，不代表历史失败轮次没有退出或重启。

保留用户设置 `enabled=true`、`popup_enabled=true`、`touch_wake=true`、音量 100；用户 LED 恢复 `activity`，恢复标记已清除，补光灯关闭（PWM `exported=false`）。外部 I/O 策略仍为 `targets={}`，权限为 `0o600`。

最终 11 份部署文件一致性核对通过：10 份源码/配置按部署时 UTF-8 编码、CRLF→LF 规范化后，其 SHA-256 均与最终快照一致；动态库与设备上的部署暂存产物 `/tmp/xiaozhi-peripherals-stage-20261025/libmaixcam2_overlay.so` 逐字节比对一致。该结论不表示本地 CRLF 文本与部署 LF 文本原始字节相同。
