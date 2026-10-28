# MaixCAM2 离线视觉模型与已安装 App MCP：盘点及实施方案

日期：2026-10-25，Asia/Taipei。状态：历史方案与只读盘点；后续已经完成部分容量和可视测试，最新状态见 [日终汇总](DEVELOPMENT_SUMMARY_2026-10-25.md)，本方案不代表模型MCP已经实现。

用户边界：先详细思考和规划，不修改代码。第一批先检测已有 App/模型能力；考虑两项及以上同时加载的实际性能。UI MCP 保持暂停。

本轮仅通过 SSH 读取应用注册表、源码、模型元数据和 /proc 状态；没有运行模型、启动/切换 App、重启服务或修改设备配置。只新增本计划文档。文件存在和静态适配信息不等于运行验证。

## 1. 当前设备事实

- `/maixapp/apps/app.info` 有 49 个非 basic 条目，对应入口文件全部存在。目录中的旧版小智备份不自动作为可调用应用。
- 常规模型为 `.mud` 元数据及其引用的 `.axmodel`。复合模型还可能引用分片、字典、embedding、本地 tokenizer 服务，不能用“单个文件存在”判为完整。
- `easyyolo_fall/main.py:3-4,68-69` 实际使用 `/root/models/easyyolo/shuaidao/model.mud`；`easyyolo_danger` 使用 `/root/models/easyyolo/weixianwupin/model.mud`。
- `/root/models/fall_yolo11.mud`、`security_yolo11.mud`、fall_rawheads、fall_taskcalib 也存在；这些版本不可静默替代 App 当前引用版本。
- 两个 EasyYolo App 在 `main.py:88-105` 读取相机、推理、绘屏；日志只含 FPS 和目标数，没有完整结构化结果 IPC。
- OCR App 在 `main.py:18-21,30-38` 使用 PP_OCR、相机并绘制文字，没有给小智返回结果的接口。
- local_vlm 有 SmolVLM、InternVL、Qwen3-VL 选择分支。前两者模型目录和分片存在；Qwen3-VL 分支指定的 model.mud 不存在。不能把菜单选项当作已安装完整能力。
- 扫描到的其他缺失路径可能属于其他板型分支或动态模板，必须结合分支条件解释，不能用正则扫描直接判定 App 损坏。

### 49 个已注册 App

| 类别 | App ID |
|---|---|
| 目标检测与分类 | detector、classifier、easyyolo_fall、easyyolo_danger、yoloworld |
| 人脸 | face_emotion、face_landmarks、face_recognizer、face_tracking |
| 人体与手势 | human_pose、human_pose_classifier、hand_landmarks、gesture_classifier |
| 文字与传统视觉 | ocr、scan_qrcode、find_blobs、line_tracking |
| 分割、旋转框、深度、跟踪 | yolo11_seg、yolo_obb、mono_depth_estimation、tracker_counter、self_learn_classifier、self_learn_tracker |
| 本地生成式模型 | local_vlm、local_chat、image_generation |
| 摄影与传输 | camera、photos、rtmp_live、rtsp_stream、webrtc_stream |
| 依赖外接硬件 | thermal_camera、thermal160_camera、thermal256_camera、thermal256_night_vision、tof_camera |
| 语音与传感器 | speech、imu_ahrs |
| USB 控制类 | mouse、usb_hand_ctrl、usb_pose_mario |
| 系统及辅助 | app_store、settings、xiaozhi、benchmark、desktop_monitor、http_file_browser、maixhub、picoclaw |

系统/USB控制/云台/外接传感器 App 只纳入清单，不因已安装而自动开放启动权限。外接 I/O 验证仍延期。

### 已发现模型能力

| 能力 | 已发现资源 | 可返回的数据 / 接入注意 |
|---|---|---|
| 通用检测 | yolo11n、yolo11s、yolov5s、yolo26n | 标签、置信度、框；YOLO26 需确认设备 SDK 支持，不能只看文件 |
| 自定义姿态检测 | easyyolo/shuaidao 及多个其他版本 | standing/sitting/lying；lying 不等于已发生摔倒，事件判断需时序规则 |
| 自定义物品检测 | easyyolo/weixianwupin 及顶层 security 版本 | knife/scissors/safe_handheld/person；保留标签原义 |
| OCR | pp_ocr、pp_ocr_320x224、pp_ocr_en；中英文识别子模型与字典 | 文字、区域；需验证完整检测→识别依赖链，BGR 输入 |
| 分类 | mobilenetv2、mobilenet_v2_no_top | 类别分数；自学习能力另需已训练样本/状态 |
| 人脸 | yolo11s_face、yolov8n_face、face_landmarks、face_emotion、insghtface_webface_r50 | 框、关键点、分类或特征；身份识别还需要登记库 |
| 人体姿态 | yolo11n_pose、yolo11s_pose、yolov8n_pose | 关键点；类别/输出语义须由对应适配器核对 |
| 手部 | hand_detector、hand_landmarks | 手部框、左右手与关键点，可能是复合流水线 |
| 分割/旋转框 | yolo11n_seg、yolo11n_obb、yolov8_qr_det_seg_320 | mask/旋转框；不能把大掩码直接塞入 MCP 文本 |
| 跟踪 | mixformer_v2；YOLO检测+tracker_counter | 目标轨迹、计数；需要持续状态，非一次模型输出 |
| 深度 | depth_anything_v2_vits | 相对深度；未经标定不能输出绝对米数 |
| 开放词汇检测 | yolo-world_1_class、yolo-world_4_class | 依赖文本特征；不能保证任意新词可离线即时使用 |
| 离线视觉问答 | SmolVLM-256M、InternVL2.5-1B | 模型目录存在；需验证全部分片、embedding、tokenizer、资源与加载 |
| 其他大模型/超分 | Qwen2.5-0.5B、SenseVoice、MeloTTS、LCM-LoRA、ESPCN、SR系列 | 分别为语言/语音/生成/超分，不能全部归为视觉识别 |

## 2. 性能现状与并发决策

只读瞬时数据，不是推理基准：

- Linux MemTotal 978540 KiB（约 956 MiB），MemAvailable 689324 KiB（约 673 MiB）；Swap=0。
- CMM 分区 3072 MiB，当前使用 64380 KiB，剩余 3081348 KiB（约 3009 MiB）。Linux RAM 与 CMM 必须分别预算；不能用模型磁盘大小推算运行内存。
- CPU 当前 1.2 GHz、温度约 46.35°C。
- 2秒 CPU 观察窗口总体 busy 99.8%，iowait 0%；另一个3秒窗口小智 PID2835 约156.5%单核口径，launcher约27.6%。当时 state=idle，imu_live=false。
- 短窗口不足以判定长期原因，但说明当前不能假定 CPU 空闲。后续多模型性能实验前，需先定位并复测小智空闲CPU基线；本轮不修复。

并发分为不同维度：

| 情况 | 初始策略 | 后续开放条件 |
|---|---|---|
| 小智后台 + 一个视觉任务 | 目标支持 | 语音/采样/显示共存实测通过 |
| 两个摄像头/显示 App 同时运行 | 不开放 | 任意App缺乏资源合作，不能靠进程并存认为兼容 |
| 两个轻量模型同时驻留 | 架构预留，默认不开启 | 两种内存峰值、切换、卸载和语音延迟通过 |
| 同一帧依次交给两个模型 | 推荐的多模型扩展路径 | 单一采集源、明确frame_id、分别预处理、轮流推理通过 |
| 两模型真正同时执行 NPU | 不作为默认承诺 | 当前SDK/调度证据和同条件A/B性能证明有收益 |
| OCR/人脸/手部内部多模型 | 视为一个复合任务 | 资源预算包含全部子模型及中间张量 |
| 大 VLM 与其他视觉模型共驻 | 初期独占 | 大模型内存、tokenizer、语音共存与恢复专项验证 |

默认一个活动视觉任务槽；后续可让一个“组合任务”内部持有两个轻量模型，而不是启动两个互不协调的 App。请求并发不等于硬件并行；MCP 主服务现有4任务并发额度不能直接用于4路NPU推理。

例：同时观察姿态和危险物品时，统一采一帧→姿态模型→物品模型→合并结果。模型可在通过测量后常驻，省去反复加载；轮流执行避免首版引入不确定的NPU并发。连续监测按模型配置不同采样频率，不强制每个模型处理每帧。

串行组合帧耗时估算式：采集+各模型预处理/推理/后处理之和+合并/显示；实际有异步重叠时单独测量，不能凭公式宣称FPS。

## 3. 统一目录、两类执行后端

MCP → 能力目录/参数校验 → 作业与资源管理 → 离线推理worker 或 App启动桥 → 结构化结果/固定预览。

### 能力目录

- 扫描产生候选，不自动加载、不自动注册成可运行模型。
- 固定能力ID映射可信路径、任务类型、适配器、预处理、后处理、标签、模型及完整依赖版本。
- 状态分为 discovered、dependencies_checked、load_verified、inference_verified、mcp_verified；缺依赖、未适配、需外设分别给原因。
- App另标 launch_only 或 result_capable；普通 GUI App 不假装能提供识别结果。
- 同名多版本显式选择并记录hash，不扫描backup目录自动选“最新”。不接受任意代码、shell或文件路径。

### 离线模型后端

- 使用新解释器启动受管worker，避免fork继承已初始化Maix/音频状态；控制环境、工作目录、参数和IPC权限。
- 首版每个任务一个进程，按需加载；通过基准后再决定保活缓存，避免提前加入复杂常驻管理。
- 复用现有曝光/白平衡稳定要求；模型输入按自身尺寸/颜色格式生成，避免借用现有云端take_photo上传链路。
- 单次检测dual_buff=False，使结果、frame_id、标注图匹配。复合/连续模式如启用双缓冲，必须有帧对应关系。
- 错误和空检测分开；返回model_id/version、job_id、frame_id、采样时间、输入尺寸、标签/分数/框或文字、耗时。
- 框明确原图坐标及变换；mask、图像、轨迹等大数据用有大小和时效限制的本地产物引用。首版不把图片上传云端；JSON识别结果仍会经现有小智云端对话通道传递。
- 原生调用卡死：stopping→recovering→idle/degraded。进程退出后仍需确认硬件健康，不能提前释放锁或自动重启整机。

### App后端

- 基于app.info/官方App ID解析固定入口，遵守launcher生命周期，逐App控制继承环境。
- 官方switch_app会设置调用进程exit flag，再通过launcher接续应用；不能在小智后台直接调用后当作通用远程启动成功。
- 先研究当前launcher_daemon版本的控制方式和返回路径；在可验证启动桥建立前，apps.open只作为设计接口，不承诺可运行。
- 状态区分请求接受、进程存在、界面就绪。无ready握手的App只能报告进程状态，不能宣称识别正常。
- apps.close只操作本次启动记录拥有的实例，核对PID/启动时间/App ID；不按名称杀用户手动打开的App。
- EasyYolo/OCR如需返回结果，可复用模型直接推理，或后续增加各App专用结果协议；不依赖屏幕OCR或随意解析日志猜结果。
- 应用启动不等于识别能力启用。普通App会话可跨短暂语音会话存在，但必须有可用返回桌面/关闭路径；模型单次作业则默认绑定当前请求会话。

## 4. 拟议 MCP 接口

| 接口 | 职责 |
|---|---|
| self.vision.list_models | 分页列能力、支持状态、输入输出、资源需求及已测性能 |
| self.vision.run | 使用model_id发起单次任务；后续通过已登记pipeline_id支持组合 |
| self.vision.get_result | 查询job状态及有时间戳的结果 |
| self.vision.stop | 停止本工具拥有的作业，确认资源回收 |
| self.apps.list | 分页列已装App及launch_only/result_capable状态 |
| self.apps.open | 请求打开白名单App，报告真实启动阶段 |
| self.apps.get_status | 查询本次启动实例及前台占用 |
| self.apps.close | 关闭本工具启动的App并核实恢复 |

首版使用字符串ID、布尔和有范围的整数参数，兼容当前Property框架。需要enum/数组/对象时先完善schema及运行时校验，不允许隐式透传。

视觉run可短时间等待结果，超过等待窗口返回pending+job_id；必须实际验证小智会继续查询，不能假设云端自动轮询或支持后台主动播报。建议待基准确定等待窗口和硬截止值；单次任务有界、结果保留有TTL、重复请求有去重。当前会话关闭默认取消其单次任务并拒绝迟到渲染。

连续监测后续单独定义显式运行时长、最大TTL、停止方式、跨会话策略及事件去抖。此阶段不承诺无人值守告警。

## 5. 资源与现有代码接入

- `hardware_tools.py:50,88,131`：现有相机预热、对象内锁和云端上传；需接入统一相机所有权，保留原接口行为。
- `xiaozhi_service.py:467,501,751,770,827`：参数校验、并发、会话关闭和8000B工具分页。新增工具只做协调，不把全部进程/模型逻辑堆进主文件。
- `xiaozhi/app/main.py:13-32`：已有App继承LD_PRELOAD导致DRM阻塞的记录与环境清理。新App启动必须对此逐项验证。
- `ui_feedback.py`、`ui_render.py`：复用固定3秒结果预览与工具名，UI MCP继续暂停。
- 拟新增模块：vision_catalog、vision_runtime/worker、resource_manager、app_registry/app_launcher；仅在需要接入App结果时增加app_result_bridge。
- 仲裁至少包含camera、display/touch、NPU、audio资源声明；首版视觉工作进程尽量不直接拥有显示和音频。
- 自有任务可以协调；非合作前台App不能靠逻辑锁强制管住。检测到占用则BUSY，无法判定则RESOURCE_STATE_UNKNOWN；不主动抢相机或杀用户App。

## 6. 分阶段执行与验收

1. **现有能力盘点**：完成49个App清单与模型族映射；下一步补分支级依赖校验、版本清单，不先承诺全量接入。逐项输出“已发现/已加载/已识别/已MCP验证”。
2. **资源和性能基线**：先定位空闲CPU占用；分别测小智关闭/开启与待唤醒/对话时资源，记录Linux/CMM内存、CPU/NPU、温度、冷加载、暖推理和语音响应。关闭服务等运行操作须属于后续实施验证阶段，本轮未执行。
3. **单能力纵向验证**：从依赖完整且适配器明确的现有模型中选择一个；完整走语音→MCP→本地采集推理→结构化结果→显示/语音。先不改已有App和原模型。
4. **扩展适配器与App桥**：逐个验证OCR/自定义检测等；独立验证launcher桥，先开放一个白名单App的打开/退出，再扩充。
5. **双模型试验**：同一分辨率/场景对比A单独、B单独、AB驻留串行、AB卸载切换；有SDK依据时才增加真并发对照。未达到共存目标则保持串行或降低采样频率。
6. **连续与大模型**：只有前述资源回收/共存稳定后，再评估跟踪计数、VLM、持续事件。

测试矩阵：

- 正确性：固定有目标/无目标图片、中文/英文文字；类别、框映射、文本、空结果、损坏/缺文件/不支持任务均验证。模型标签不能升级为未经验证的业务事件。
- 帧一致性：结果和显示同frame_id；不能用上一帧结果标注当前帧。
- 稳定性：至少20次运行/退出循环，至少20次A/B切换，观察内存是否持续增长、是否有孤儿进程或相机占用；双模型连续场景建议30分钟专项测试。
- 故障：加载失败、worker崩溃、超时、会话断开、用户关闭、App手动启动、结果过期；未确认硬件恢复时必须degraded而非ready。
- 共存：麦克风计数持续增长，语音唤醒/对话/播放无新增中断，触摸可用，拍照预览、灯、IMU保持正常。
- 性能：分别记录冷加载、预热、推理p50/p95、整次调用时延、显示FPS、结果年龄、音频缺帧/欠载、RAM/CMM峰值与温度。不得把NPU纯推理FPS当作端到端FPS。
- 并发准入：以实际业务所需更新频率和语音延迟为目标，再设置硬阈值；没有测量前不承诺双模型达到某个FPS。双模型驻留与双模型真正并行分开报告。

## 7. 决策及边界

推荐统一目录+独立推理作业+受控App桥。先完成已有能力盘点，再选择经过依赖核对的能力做小规模纵向验证。

不采用“每个App/模型注册一个MCP工具”：增加schema和选择负担；用少量通用工具加分页目录。
不采用“直接启动两个现有GUI App”：摄像头、显示、触摸和生命周期缺乏合作，进程隔离不等于硬件隔离。
不采用“文件找到即自动开放”：同名版本、分片、缺模型、外设依赖和标签语义都可能造成误报。

最终并发方向：允许一个任务组合多个模型，优先共享帧、受控驻留、串行/分时调度；真正并行只对通过性能与恢复测试的具体组合开放。当前高CPU基线是先决问题，不能由空闲NPU内存掩盖。

## 8. 官方参考

- App API与switch_app语义：https://wiki.sipeed.com/maixpy/api/maix/app.html
- 应用开发、安装清单与源码：https://wiki.sipeed.com/maixpy/doc/en/basic/app.html
- MaixCDK switch_app实现：https://github.com/sipeed/MaixCDK/blob/main/components/basic/src/maix_app.cpp
- MaixCAM2模型格式与转换：https://wiki.sipeed.com/maixpy/doc/en/ai_model_converter/maixcam2.html
- 双缓冲与上一帧结果：https://wiki.sipeed.com/maixpy/doc/en/vision/dual_buff.html
- Linux/CMM内存划分：https://wiki.sipeed.com/maixpy/doc/en/pro/memory.html

这些页面是当前上游文档；部署设备SDK版本仍需在实施阶段逐接口核对。静态审查由源码检查与独立方案审查并行完成，已纳入原生调用取消、非合作App、资源恢复、模型复合依赖和结果语义方面的建议。
