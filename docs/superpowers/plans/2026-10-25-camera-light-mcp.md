# Camera and Light MCP Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline; request a fresh review of the complete diff before deployment.

**Goal:** 在当前小智服务中完成摄像头视觉问答和补光灯控制，并在已连接设备上验证。
**Architecture:** 新增 hardware_tools.py 隔离硬件/视觉传输；现有 MCP 调度支持异步回调并接受 vision 能力。原音频链路不重构。
**Tech Stack:** Python 3.9+（主机）/3.13（设备）、unittest、现有 requests、MaixPy、systemd。
**Spec:** ../specs/2026-10-25-camera-light-mcp-design.md

## Constraints / Review focus

- 不增加依赖；保留原 5 个 MCP 工具。
- 主机测试不连接设备、不加载 Maix 硬件；外部硬件和网络可用替身隔离，业务验证必须运行真实代码。
- 摄像头初始化失败、超时、无 vision 能力、服务器错误不得报告成功。
- 非法类型/范围不能写硬件；取消/断线不能跨会话使用旧 vision token。
- 灯停止清理和首次拥有通道失败必须有测试，避免灯常亮。
- 产品源码不记录摄像头内容、vision token、完整配置。

## Tasks

- [x] 写 tests/test_hardware_tools.py 和 tests/test_mcp_hardware.py；先验证新工具缺失、协议参数/结果、边界和失败场景为红。
- [x] 实现 hardware_tools.py 的相机、视觉 HTTP 和灯控制；完成单元测试红→绿。
- [x] 修改 xiaozhi_service.py：新增工具、异步回调、vision initialize/reset、退出清理。部署脚本复制新模块，unit 增加停止兜底。
- [x] 运行完整 unittest discover、Python 编译、Shell 语法和差异检查；独立审查后修复有证据的问题。
- [x] 设备预检/备份、增量部署、重启小智；验证配置保留和模型/音频/launcher 正常。
- [x] 用真实云端语音调用摄像头和灯，验证 10 个工具注册、照片分析结果、亮度回读与原功能回归；失败恢复或修复后再测。
- [x] 更新 MCP 清单与测试记录，记录未验证边界和最终设备状态。

## Execution ledger

- 起点：main@9d7885e，已有 docs/ 为本会话前序文档；产品源文件干净。
- 采用指定项目目录内功能分支，避免把设备备份和本地测试证据分散到其他检出；不自动提交/推送。
- 主机初始 18 项测试显示功能缺失及参数/异步处理缺陷；实现后追加异常和并发覆盖，共 21 项在 Windows Python 3.9 与设备 Python 3.13 均通过。
- 官方协议参照：78/xiaozhi-esp32@f443114fe7c3fa26b10d3630090119891cd1c4fd，main/mcp_server.cc 与 main/boards/common/esp32_camera.cc；兼容 py-xiaozhi v2.1.2。
- 摄像头耗时任务由会话拥有，工具回包绑定原 WebSocket/session，结束时取消；HTTP 仍使用现有 requests。
- 设备预检已确认原服务/unit 哈希、主机身份、launcher 状态和原配置，尚未覆盖产品文件；已暂存测试代码在 /tmp/xiaozhi-mcp-stage-20261025。
- 后续执行完成：独立审查的异常退出 PWM 残留问题已红→绿修复并实机复现恢复；官方 HTTP vision 地址通过同端点 HTTPS 实测后做特定主机升级；最终主机/板端各 25 项测试通过。
- 真实云端灯开关/调光/状态、摄像头状态/拍照分析均成功，拍照后语音回归通过；备份位于 /root/xiaozhi_backups/camera-light-20261025-095845/。
- 最终部署文件哈希与本地一致，10 个工具，launcher/小智 active/running；恢复 enabled=false、volume=100、popup_enabled=true、touch_wake=true，补光灯关闭且通道已释放。详见 CAMERA_LIGHT_MCP_VALIDATION.md。
