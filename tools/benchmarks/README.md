# 离线视觉软件路径基准

`model_probe.py` 是在MaixCAM2上执行的诊断脚本。它加载真实模型、打开摄像头；不是主机单元测试，不应在已有相机App运行时启动。

示例（设备上）：

```sh
python3 model_probe.py --models fall_current danger_current --seconds 60 --fps-limit 25 --out /tmp/vision-probe.jsonl
```

模型别名及路径在脚本开头白名单中。`fall`/`danger`是旧App引用版本，已有兼容失败记录；`fall_current`/`danger_current`是顶层当前版本，两者不能混淆。

多模型共享一帧，使用串行推理；不同输入尺寸按FIT_CONTAIN缩放。记录加载、预热、首结果、逐帧软件延迟、结果间隔、预处理和采集时间。输出文件必须不存在，避免覆盖证据。

验收必须同时检查进程成功退出、`errors=[]`、`completed_duration=true`、`camera_closed=true`，并分别统计每模型完整滑动1秒窗口的结果数及最大延迟。不能只看平均FPS、p95或“没有崩溃”。

本脚本不绘制实时画面；本地递增frame_id不是传感器时间戳，不能证明图像新鲜度。采集API调用至结果时间不包括此前曝光/ISP队列和屏幕扫描，不等于完整端到端延迟。画面显示、真实麦克风、触摸、网络分发及资源恢复必须另验。

运行时保留小智后台的CPU、内存、温度和音频日志观测；发现资源异常及时停止。不要创建第二个Recorder，不自动关闭用户App或重启整机。SSH私钥、设备配置和现场照片不属于可发布的基准附件。
