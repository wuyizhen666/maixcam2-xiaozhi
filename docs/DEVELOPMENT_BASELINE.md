# MaixCAM2 小智项目开发基线

记录日期：2026-10-25（Asia/Taipei）。本轮范围：克隆、源码梳理、主机静态验证与后续开发入口建立。

## 1. 仓库与版本

- 本地目录：`F:\Git\Maixcam2\projects\maixcam2-xiaozhi`
- 上游 / origin：<https://github.com/wuyizhen666/maixcam2-xiaozhi>
- 分支：`main`，跟踪 `origin/main`。
- 基线提交：`9d7885ee46329ac5a5daebd418e3a62f8c2f4f39`。
- 提交主题：随仓库附带唤醒模型与预编译运行库。
- 完整克隆包含 2 个提交；本次 `ls-remote origin HEAD` 与本地 HEAD 一致。
- 本轮新增本文件；运行代码、部署脚本与上游快照保持原样，未提交、推送或部署。

GitHub 直连本次失败，通过本机已监听的 HTTP 代理 `127.0.0.1:7078` 成功克隆和核对远端。代理参数只用于单条 Git 命令，没有修改全局配置。以后使用前应重新确认代理可用。

## 2. 代码入口与数据流

| 路径 | 作用与开发定位 |
| --- | --- |
| `xiaozhi/service/xiaozhi_service.py` | 当前后台服务，1851 行；配置、OTA 激活、WebSocket、MCP 工具、音频、唤醒与对话状态机 |
| `xiaozhi/service/ui_render.py` | Pillow 绘制状态图标与聊天弹窗，输出 RGBA 图层 |
| `xiaozhi/app/main.py` | MaixPy 设置界面；显示状态、修改开关和唤醒词；导入 maix 前清理注入环境并重新执行自身 |
| `xiaozhi/uishim/maixcam2_overlay.c` | Linux C 注入库；hook launcher 的显示及触摸接口，混合图层并处理弹窗触摸 |
| `xiaozhi/service/xiaozhi.service` | systemd 服务及 launcher 启动顺序约束 |
| `xiaozhi/deploy.sh` | 在设备上复制代码、编译注入库、修改 systemd 并重启服务 |
| `diag/` | 设备协议探测、音频下行对照及麦克风标定；不是可直接在主机运行的单元测试 |
| `device_snapshot/` | 历史代码和模型/运行库；源码仅作对照，当前开发入口是 `xiaozhi/` |
| `ref/`、`shots/` | 上游参考图片和历史设备截图，不构成本机实测证据 |

主要数据流：麦克风 → sherpa-onnx 唤醒 → OTA / WebSocket 会话 → Opus 上行 + 本地 VAD → 服务端文字/音频 → 重采样播放 + 弹窗显示。

设置 App 与后台通过 `/root/xiaozhi/config.json` 和 `state.json` 交换配置/状态；后台输出 `status.rgba`、`popup.rgba`，注入层通过 `/tmp/xz_cmd` 回传触摸命令。

服务源码入口：`load_cfg()`、`ota_poll()`、`try_activate()`、`McpServer`、`WakeWordDetector`、`mic_thread()`、`kws_thread()`、`play_thread()`、`Vad`、`session_run()`、`main_loop()`、`main()`。

## 3. 环境与依赖边界

上游记录的环境为 MaixCAM2、Ubuntu 22.04、MaixPy 4.12.5、Python 3.13、640×480 屏幕。这是上游自述，尚未核对用户设备；README 中的 IP 和设备名不能直接作为本项目连接目标。

源码涉及的第三方 Python 模块：`maix`（MaixPy）、`numpy`、`requests`、`websockets`、`pyaudio`、`opuslib`、`PIL`（Pillow）、`sherpa_onnx`。其中 `sherpa_onnx` 在唤醒初始化时动态导入，不能仅凭附带的 C API 库判定 Python 绑定已满足。

设备还需要 Linux/systemd、GCC、音频系统及其动态库、Maix launcher、显示与触摸设备，以及中文字体 `/maixapp/share/font/SourceHanSansCN-Regular.otf`。实际依赖版本、ABI 和动态链接闭包需在目标设备核查。

本地主机为 Windows，检查解释器为 `D:\Apps\Anaconda3\python.exe`（Python 3.9.13）。本轮未安装依赖，未导入或执行服务、设置 App、设备诊断脚本。Python 3.9 的语法检查不能证明 Python 3.13 / MaixPy 运行兼容性。

仓库未发现依赖锁文件、通用自动化单元测试套件、CI 工作流或 lint/typecheck 配置；本轮不将 `diag/proto_test*.py` 当作单元测试运行，因为它们读取设备配置并连接外部服务。

## 4. 已完成验证

| 检查 | 本次结果 | 证明范围 |
| --- | --- | --- |
| 本地 HEAD 对照远端 HEAD | 一致 | 克隆时版本一致性 |
| `git fsck --full` | 退出 0 | Git 对象完整性 |
| 所有 Git 跟踪的 `.py` 用 `compile(..., 'exec')` 检查 | 11/11 通过 | 语法编译，不导入、不执行、不生成 pyc |
| Git 跟踪的 JSON 解析 | 1/1 通过 | 配置模板语法 |
| Git Bash `-n xiaozhi/deploy.sh` | 退出 0 | Bash 语法解析；不是目标 `/bin/sh` 运行验证 |
| `sherpa_rt/lib/` 三个 `.so` 文件头检查 | ELF64，e_machine=183（AArch64） | 文件头架构；未加载、未验证依赖或 ABI |
| Windows 检出属性检查 | `core.autocrlf=true`，`core.symlinks=false` | 当前工作树的跨平台传输风险 |

尚未执行：C 注入库编译、目标 Python 导入检查、模型加载、设备部署、显示/触摸/麦克风/扬声器运行、云端激活与多轮对话、冷启动及长时间稳定性测试。

### 可复现的主机检查（PowerShell）

以下命令只检查本地源文件，不运行应用：

```powershell
Set-Location 'F:\Git\Maixcam2\projects\maixcam2-xiaozhi'
git fsck --full
@'
import json
import pathlib
import subprocess
paths = subprocess.check_output(['git', 'ls-files'], text=True).splitlines()
py_count = json_count = 0
for name in paths:
    path = pathlib.Path(name)
    if name.endswith('.py'):
        compile(path.read_bytes(), name, 'exec')
        py_count += 1
    elif name.endswith('.json'):
        json.loads(path.read_text(encoding='utf-8'))
        json_count += 1
print(f'PASS: {py_count} Python files, {json_count} JSON files')
'@ | & 'D:\Apps\Anaconda3\python.exe' -
& 'D:\Apps\Git\bin\bash.exe' -n xiaozhi/deploy.sh
git diff --check
git status --short --branch
```

## 5. 接手时发现的问题

以下为源码/文件检查结果与待验证风险，本轮未修改运行行为。

| 优先级 | 发现与依据 | 后续处理及验收 |
| --- | --- | --- |
| 高 | 模型与运行库位于 `device_snapshot/root_xiaozhi/`，`deploy.sh` 不复制它们，也不安装 Python 依赖；README 只复制 `xiaozhi/` 的部署方式不足以完成空白设备安装 | 区分全新安装和已有设备升级；建立资源清单、依赖预检与版本记录，验证缺资源时在写入设备前失败 |
| 高 | 本机 `deploy.sh` 为 69 行 CRLF；`bash -n` 通过不能排除目标 shell 执行时的回车问题 | 发布前统一文本 LF，并检查交付包实际字节；不能直接把 Windows 工作树视作设备可执行包 |
| 高 | `sherpa_lib/libonnxruntime.so` 的 Git 模式为软链接 120000，本机检出成 81 字节普通文本；目标为上游设备 Python 3.13 的绝对路径 | 不直接把该文本当动态库复制；确定是否需要此历史链接，在目标运行时重建或排除，验证最终链接可解析 |
| 高 | `deploy.sh:60-61` 先重启小智服务再重启 launcher；unit 的 `After=launcher.service` 不自动覆盖两个独立 restart 命令的全过程 | 结合上游黑屏记录验证升级启动顺序、显示资源释放和恢复流程；当前只能判定存在顺序风险，不能宣称已复现黑屏 |
| 高 | `xiaozhi_service.py:365,407` 的 OTA/激活请求使用 `verify=False` | 恢复证书校验并验证设备时钟/CA；补充失败处理验证 |
| 高 | `xiaozhi/app/main.py:91` 将完整 cfg 写入日志，配置可含 `hmac_key` 等凭据 | 日志只输出允许字段或脱敏字段，验证不泄露凭据；本轮未读取用户设备配置 |
| 中 | unit 的 ExecStartPre 最多轮询 60 次后仍会继续启动，即使没有找到 launcher 的 DRM fd | 设计明确的就绪失败行为，测试正常启动、超时和 launcher 缺失 |
| 中 | 没有自动化回归测试和依赖版本锁定；第三方说明中的 pip 依赖清单遗漏动态导入的 `sherpa_onnx` | 先为协议封包、VAD、Opus 帧长、重采样等可隔离逻辑建立测试，再修改行为；从真实设备导出可复现依赖版本 |
| 中 | 上游文档记录 KWS CPU 占用较高与偶发无 TTS | 先建立实际设备测量与复现基线，再决定线程/算法调整；不引用上游数值作为当前设备表现 |

第三方来源及许可见 `THIRD_PARTY.md`。根目录未见项目整体 LICENSE 文件；如果后续要分发派生版本，需要进一步核对自写代码的授权范围。

## 6. 下一阶段开发顺序

1. 先处理可复现部署：资源归属、Python 依赖、LF/软链接、设备预检、备份与恢复、启动顺序。准备测试后再修改部署脚本。
2. 建立离线行为测试，再处理证书校验和配置日志等源码问题；保持修改小且可回退。
3. 获取用户实际设备地址与目标环境，在明确的设备阶段核对版本、现有服务、模型/库、音频和显示配置；避免使用 README 的示例 IP。
4. 部署后分别验证冷启动桌面、设置 App、唤醒、单轮/多轮对话、弹窗关闭与触摸、音频连续播放、断网恢复、重启恢复。

本轮完成状态为“仓库与开发基线就绪”。设备功能、性能及长期稳定性均未验证。
