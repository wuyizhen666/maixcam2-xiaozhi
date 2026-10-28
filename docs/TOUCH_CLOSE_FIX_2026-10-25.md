# 小智弹窗 X 点击误唤醒修复

最终状态：指定圆标长按已实测触发；用户随后确认长按后真实讲话的文字和语音回答正常。X退出、再次语音唤醒亦已确认。下文为按时间保留的排查记录，早期“待确认”和旧命中范围不代表当前状态；当前规则为左上52×48像素热区持续2秒，普通触屏不触发。关联[音频修复验收](AUDIO_MUTE_CAPTURE_FIX_2026-10-25.md)与[日终汇总](DEVELOPMENT_SUMMARY_2026-10-25.md)。

用户现象：点击右上角 X 后看起来立即重启。

## 原因

设备小智服务 PID2835、NRestarts=0，实际日志是“弹窗：点击唤醒”随后重建语音会话，不是服务重启。

注入层绕过 Maix TouchScreen，直接把 hyn_ts 原始 evdev 坐标写成 `TAP x y`。设备轴范围X=0..480、Y=0..640；服务却按640×480横屏按钮命中。真实X点击(461,621)、(477,636)等被误判为普通区域。

官方MaixCAM2旋转公式为display_x=raw_y、display_y=maxX-raw_x-1。服务消费端现转换为(min(raw_y,639),max(0,479-raw_x))。不重复转换已经由Maix SDK处理的TouchScreen.read()。

真实顶边点击转换后y可为2，原按钮抖动容差仅覆盖y>=4；现关闭和喇叭命中区覆盖整个顶部栏高度，横向区域不重叠，绘图保持原样。

## 行为

- X取消当前会话/激活任务，清理临时显示并收起弹窗；后台服务保留。
- 关闭前已经排队的普通弹窗cmd点击，在关闭后被丢弃，避免旧事件重新打开。
- 新的语音唤醒仍允许；触摸唤醒沿用已有1.5秒抑制。
- C注入库及launcher均未更新/重启。TAP仍是原始坐标协议，调试脚本不能写横屏显示坐标。

## 验证

- 先复现实际坐标测试失败，再修复；主机与板端各135项通过。
- 覆盖真实X坐标、喇叭顶边、普通区域、越界、坐标端点和排队点击。
- 已部署，备份`/root/xiaozhi_backups/touch-close-final-20261025-171030`。
- 运行中的服务收到`TAP 461 621`后约0.42秒隐藏弹窗、0.68秒回到idle，6秒内没有重开，PID13465未变。本次自动验证在connecting阶段关闭；手指点击验证另待用户反馈。
- 证据：`.runtime/touch-close/host-tests.txt`、`board-tests.txt`、`live-test.txt`、`deploy-final.txt`。

修改文件：`xiaozhi/service/xiaozhi_service.py`、`tests/test_popup_touch.py`。复用现有会话取消与资源清理，无新增依赖。

官方依据：https://raw.githubusercontent.com/sipeed/MaixCDK/main/components/vision/port/maixcam2/maix_touchscreen_maixcam2.hpp

## 关闭后的语音重唤醒跟进

用户确认手指X可以正常退出，但随后语音无反应，手动触摸仍可唤醒。日志确认服务未重启、麦克风持续采集，不能把该现象解释为服务停止。

在排除触摸干扰后，修改前固定唤醒WAV连续3轮均可关闭后重唤醒。因此未证明现场问题是永久禁用；真实声音仍需复测。

代码及回归测试发现并修正两条漏唤醒路径：

- KWS每次decode后立即检查结果，命中即reset并退出当前decode循环，避免先命中后空结果覆盖；冷却期命中也reset。
- 本地KWS队列清理、流重置和idle/UI恢复均在等待网络close之前完成，避免网络关闭拖延本地恢复，以及旧连接晚结束覆盖新会话。
- 唤醒流重置成功耗时和失败原因写入日志，不再静默吞失败。

修改：`xiaozhi/service/xiaozhi_service.py`、新增`tests/test_rewake.py`。复用原模型、阈值、会话清理；未调整模型/音频增益、未增加依赖。主机及板端各139项测试通过。备份`/root/xiaozhi_backups/rewake-20261025-172716`。

新版真机3轮固定WAV：均记录`语音唤醒`及`wake`来源，关闭后均idle；唤醒流重置3/5/5ms，关闭至idle约0.10/0.20/0.10秒，PID16590不变。音频起点至唤醒约2.42/1.92/2.33秒，包含约1.78秒语句本身，不是说完后的延迟。触摸唤醒临时禁用以排除误算，测试后恢复。

用户已完成真实声音“唤醒→手指X关闭→松手约2秒→不触屏再次唤醒”，明确确认“第二次语音也能正常唤醒”。本次现场复测通过，关闭与再次语音唤醒闭环已验证。reset失败虽有日志，仍可能进入idle，不能把idle作为KWS可用的充分证据；本轮实际3次reset均成功。

证据：`.runtime/rewake/isolated-before.txt`、`host-tests.txt`、`board-tests.txt`、`deploy.txt`；自动复测脚本`.runtime/rewake/repeat_probe.py`，设备结果`/tmp/xiaozhi-rewake/after.json`。

## 最新触摸唤醒规则：指定圆标持续2秒

用户最终要求覆盖此前任意触屏策略：只有左上角圆形状态标志持续按住2秒才触摸唤醒。普通点击、短按圆标、区域外长按不触发；点X退出后同样可以使用指定长按或语音唤醒。弹窗正文普通点击也不再打断并重开会话。

圆标status.rgba为38×38，注入位置(10,5)，中心(29,24)；最初使用半径22像素圆形，后根据真实触点记录调整为左上52×48像素热区（见下文）。移出区域、松手或禁止唤醒时重新计时，一次持续按住只触发一次，必须松手才能开始下一次。

读取方式采用hyn_ts的只读EVIOCGKEY/EVIOCGABS快照，不消费或grab桌面事件。设备已实查支持BTN_TOUCH；此方式避免弹窗EVIOCGRAB期间服务漏收release、缓存pressed残留的问题。轮询间隔约50ms，因此2秒是最短持续条件，存在调度和采样粒度，不能宣称零误差计时。

修改`xiaozhi_service.py`、`ui_render.py`，补充`test_popup_touch.py`和`test_touch_hold.py`；沿用原touch_wake配置开关，无新增依赖。主机及板端各147项通过；部署备份`/root/xiaozhi_backups/icon-hold-20261025-174108`。服务PID19176 active，enabled/touch_wake/popup_enabled均true，唤醒引擎已就绪。手指短按/长按/退出后再长按正在等待现场反馈。

证据：`.runtime/close-touch-gate/host-tests.txt`、`board-tests.txt`、`deploy-final.txt`。

### 多点触摸槽位修正

用户实测第一版长按不能唤醒，不能将上一版147项单元测试视为物理验收通过。设备实查表明：hyn_ts有6个MT槽位（ABS_MT_SLOT范围0..5），EVIOCGABS读取X/Y当前值恒为0，实际触点位置在EVIOCGMTSLOTS中。第一版使用普通ABS快照，导致按住时被映射到屏幕(0,479)，一直被判为圆标外。

修正为EVIOCGKEY确认按压，再读取全部槽位tracking ID，必须恰有一个有效触点；从该槽位取X/Y，并复核tracking IDs在读取期间未改变。多指或松手取消计时。仍不读取/消费事件队列，不影响桌面输入。

回归测试使用普通ABS值为0、有效触点位于槽位2的数据复现旧版失败，再验证新版坐标和松手处理。主机及板端147项通过，修正版备份`/root/xiaozhi_backups/touch-mt-slots-20261025-174809`。真实触点观察记录`.runtime/close-touch-gate/physical-observe.txt`；等待新的现场长按结果后才能确认物理验收。

### 真实记录回放与边缘热区

用户仍反馈无法长按唤醒。10分钟持续记录终于覆盖真实操作：两次用户标为圆标中心的按压，实际换算为(1,0)、(1,2)，持续3.084秒、3.638秒；中央轻点为(369,305)。普通事件与MT槽位记录一致，不能再归因为计时未满或漏读按压。原半径22圆形排除了这些左上边缘点。

当前仅将圆标热区扩至0<=x<52、0<=y<48，不修改全屏坐标标定、不扩大到其他菜单区域；连续2秒、移出重置和每次按压仅一次规则保留。删除旧触摸1.5秒额外屏蔽，避免X关闭后再长按需要超过2秒。

完整719条触摸记录回放恰好产生两次命中，距两次按下分别2.021秒、2.019秒；中间滑动与中央轻点未命中。新增真实边缘点和关闭后立即开始计时回归，主机与板端各149项通过。

部署期间中央操作误进入camera App，导致既有服务启动前置等待launcher而超时。已停止自动重试；用户返回菜单后确认launcher已持有DRM，再恢复服务并部署。最终备份`/root/xiaozhi_backups/touch-edge-recovery-20261025-180512`；设备源码与本地一致，enabled/touch_wake/popup_enabled均true，唤醒引擎已就绪。尚待这版的手指长按现场反馈。

证据：`.runtime/close-touch-gate/physical-trace.jsonl`、`deploy-edge-final.txt`及最新host-tests.txt/board-tests.txt。

### 现场长按已触发，后续聆听反馈

用户确认“两次能唤醒”，但表示进入聆听后没有说话、随后自动退出。日志18:08:20和18:08:56均记录“左上角长按2秒唤醒”及`touch_hold`来源，分别在下一秒进入listen；18:08:41和18:09:17按当前idle_close_s=20结束。两轮没有STT/TTS记录，上行约各998帧，不能直接判定麦克风故障或完整语音交互通过。当前长按仅开始聆听，不主动发起问候；已询问用户是否在聆听时说过指令。指定长按触发本身已有现场及日志证据。
