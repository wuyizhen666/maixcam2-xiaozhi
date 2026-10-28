# IMU 实时显示与陀螺仪零偏校准

本文对应 2026-10-25 的已部署实现。已完成主机/板端测试、板端真实传感器 MCP 分发验证及语音经云端触发校准的端到端验证。

## MCP 接口

原有 26 个工具新增 3 个校准工具，总数为 29。以下 4 个 IMU 接口均无需参数。

| 工具 | 行为 |
|---|---|
| `self.imu.read` | 返回一次六轴采样，并启动当前会话内的实时屏幕显示 |
| `self.imu.calibrate_gyro` | 静置约 3 秒，计算并保存三轴角速度零偏；成功后启动实时显示 |
| `self.imu.get_calibration` | 查询是否校准、三轴零偏、校准时间及样本数，不初始化传感器 |
| `self.imu.clear_calibration` | 删除应用保存的零偏并恢复原始角速度；成功后启动实时显示 |

可向小智表达“读取 IMU 数据”“校准陀螺仪”“查询陀螺仪校准状态”或“清除陀螺仪校准”。是否触发对应工具应以实际 MCP 日志为准。

`read` 返回 `acceleration_m_s2`、`angular_velocity_deg_s`、`raw_angular_velocity_deg_s`、`gyro_calibrated`、`timestamp_unix` 及型号。当前驱动温度不可用，`temperature_c` 为 `null`，不会把驱动占位值解释为真实温度。校准状态包含 `model` 和 `gyro_calibrated`；已校准时另含 `bias_deg_s`、`calibrated_at_unix`、`sample_count`。

## 屏幕与实时读取

第一次面板使用 MCP 返回的同一个样本。随后读取循环以 0.2 秒等待间隔采样，目标约 5 Hz；界面按 0.5 秒目标间隔刷新，约 2 Hz。两者均受实际读取和绘制耗时影响，不能当作实测性能。

大面板保留 3 秒，期间数值更新不会延长计时。收起后显示聊天内容，底栏继续显示加速度 XYZ、角速度 XYZ 与单位，并保留 MCP 工具名。再次调用读取会重新展开 3 秒。照片预览优先，原拍照预览时长不变。

关闭弹窗、禁用显示或服务、会话结束或采样失败后，实时读取停止，旧数据显示被清除。校准与清除操作开始前，会先停止实时读取并等待在途读取结束，避免同时访问传感器。

## 校准方法与持久化

将设备放在稳定桌面，静止约 3 秒，再调用校准。实现使用原始角速度均值作为三轴零偏；后续角速度为原始值减零偏，加速度不作校准。SDK 的 `calib_gryo` 始终为 `False`，避免重复扣除。

校准需要至少 100 个有效样本，尝试上限为 400 次。当前静止判定条件为：

- 每个样本的加速度模长距 9.80665 m/s² 不超过 1.0 m/s²。
- 各加速度轴标准差不超过 0.2 m/s²。
- 各角速度轴标准差不超过 0.3 °/s。
- 各角速度轴均值绝对值不超过 10 °/s。

这些条件用于拒绝明显晃动；恒速绕重力方向旋转不能完全与零偏区分，因此仍需实际静置设备。样本不足、明显运动或保存失败不会替换原有校准。

文件为 `/root/xiaozhi/imu_gyro_calibration.json`，使用临时文件、`fsync` 和原子替换保存，权限为 0600。记录绑定型号与设备主机名，启动后首次读取或查询时严格校验加载。无效、过大、非有限数值或不匹配的记录不会启用。清除仅操作此应用自有文件；删除失败保留内存中的旧零偏。该设备专属文件不应提交到仓库。

## 验证结果（2026-10-25）

- 主机及板端各 129 项测试通过；独立并发审查确认最多一个实时循环，关闭后全部退出。新增并发测试绕过锁时失败、恢复锁时通过。
- 板端以真实 MCP 分发器和真实 IMU 调用了读取、校准、查询、清除；校准 221 个有效样本。清除后再校准，最终保留有效校准。
- 静止原始角速度约 (+0.788,+0.009,-2.223) °/s；校准后五秒观测平均约 (+0.010,-0.017,-0.019) °/s。这是本次桌面静止结果，不是全温度精度保证。
- 板端大面板 3.179 秒收起，五秒内取得 10 个不同显示样本；关闭标志后数据清空、循环停止。
- 语音“校准陀螺仪”实际触发云端 `self.imu.calibrate_gyro`，返回 `isError=false`，语音回复校准完成。最新版大面板 3.185 秒收起，约七秒收集 14 个不同显示样本，均为已校准数据。
- 已查看真实显示帧：全量六轴、单位、已校准状态正常；收起后底栏显示六轴简值和 MCP 名称，聊天提示从“校准中”变为“校准完成”。
- 新进程重新加载保存零偏成功，文件权限 0600；测试结束后服务与桌面均 active、IMU 实时数据已清空，原 enabled/popup_enabled/touch_wake=true、volume=100 保持。
- 部署文件与本地四份实现逐一校验一致。最新增量备份 `/root/xiaozhi_backups/imu-live-cal-20261025-160227`。

证据位于本地忽略目录 `.runtime/imu-live-cal/`：`bench-results.json`、`cloud-results.json`、`cloud-full.png`、`cloud-compact.png`、`host-tests-final.txt`、`board-tests-final.txt`、`final-verification.json`。

运动拒绝、保存失败保留旧值和无效文件拒绝有模拟测试；未要求用户再次晃动设备做本轮运动拒绝实测。外接 I/O 设备验证仍按此前约定延期。

涉及源文件：`xiaozhi/service/imu_adapter.py`、`xiaozhi/service/ui_feedback.py`、`xiaozhi/service/ui_render.py`、`xiaozhi/service/xiaozhi_service.py`。沿用现有渲染与反馈通道，无新增依赖。相关测试：`tests/test_imu_adapter.py`、`tests/test_ui_feedback.py`、`tests/test_imu_panel_render.py`、`tests/test_mcp_hardware.py`。
