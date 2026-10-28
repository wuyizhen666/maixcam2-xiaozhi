# 喇叭静音导致采集音量近零：诊断与修复

用户在指定位置长按唤醒成功后，实际说出指令，但服务始终聆听并在20秒后退出。这次不能用“用户没有说话”解释。

## 证据与定位

- 18:08:27日志记录“弹窗：静音”，state.muted=true。
- 原apply_volume把RX LEFT/RIGHT ANA/DIG GAIN四个声卡控制当作扬声器音量，静音时全部置零。
- 此后真实麦克风帧仍每秒约50块增长，但RMS长期仅1～4，VAD未触发，没有STT。
- 保持muted=true和DAC状态不变，仅将上述四项恢复40%后，后续RMS回升到52、峰值327；构成这组增益影响采集的现场对照。没有仅根据RX/TX命名推断方向，也没有新建第二个Recorder。
- Recorder.volume(40)是独立AI VQE倍率0.8，不能替代已被清零的codec硬件增益。

## 修复

`xiaozhi/service/xiaozhi_service.py`删除原RX声卡写入逻辑。apply_volume现在只更新0～1播放增益；播放线程在write之前缩放自己的16bit PCM，静音输出等长零样本。不会调整麦克风输入增益或改变音频采集通道。0%静音、50%半幅、100%原样透传。

这次恢复了受损输入增益，当前模拟增益原始值22/22、数字增益48/48（四项40%）；mic_gain仍40、配置音量仍100。最终取消喇叭静音，供现场讲话复测。

## 验证

- 新增`tests/test_audio_volume.py`，先验证旧版静音调用四次RX设置导致失败，再验证修复后不写声卡控制、静音零PCM、半幅及满幅。
- 修正测试模块导入隔离：`test_mcp_hardware.py`仅恢复两个codec stub条目，避免恢复整个sys.modules导致Python3.13的_queue.Empty类型身份分裂；`test_popup_touch.py`复用同一个测试service实例。未把测试环境错误改为生产代码的宽泛异常捕获。
- 主机及板端各153项通过。
- 已部署，备份`/root/xiaozhi_backups/audio-output-only-20261025-183019`。
- 真实运行中通过喇叭按钮路径静音，再取消静音，四项输入增益全过程保持22/22/48/48。
- 静音时注入固定PCM指令，云端完整识别“请调用获取当前音量的工具，读取扬声器音量，不要修改音量”，实际调用self.audio_speaker.get_volume成功并生成“当前扬声器音量是100”的TTS。这证明静音未阻断协议处理；注入PCM不等同于真实麦克风讲话验证。
- muted_after=false。用户已用真实声音完成“长按左上角圆标2秒→说现在音量是多少”，明确确认“文字和语音回答都正常”，该现场端到端复测通过。

证据在`.runtime/audio-silence/`：rx-probe.txt、host-tests.txt、board-tests.txt、deploy.txt、verify.py；设备结果`/tmp/xiaozhi-audio-silence/verification.json`。输入混音器恢复前快照`/tmp/xiaozhi-audio-silence/mixer-before.txt`。

未更换模型、未修改VAD阈值或静默超时，无新增依赖。音量采用软件幅度比例，不宣称与旧版混音器百分比具有相同声压；真实声学表现仍以现场验证为准。
