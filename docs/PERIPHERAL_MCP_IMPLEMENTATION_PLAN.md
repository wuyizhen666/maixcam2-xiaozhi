# 下一阶段：板载外设与扩展总线 MCP

日期：2026-10-25。范围来自用户提出的用户LED、按键、六轴IMU、RTC、GPIO、I²C、SPI、UART、ADC。**本阶段方案已实施并部署，阶段完成时为26个MCP工具；后续陀螺仪校准扩展至29个，见当前MCP清单。** 本文保留设计与范围约束，实际结果见 [阶段验收](PERIPHERAL_MCP_VALIDATION.md)。无外接设备的通信验证边界仍保留，不把控制器/保护验证改称实物外设通信通过。

## 推荐路线

先完成设备上已明确的专用板载功能，再开放按接线配置的外部I/O。不采用“任意引脚、任意总线、任意地址均可直接操作”的统一工具，因为本机触摸、RTC、电池、IMU及SDK通信存在实际共享资源。

### 第一批：已实现的专用板载工具

| 工具 | 行为 | 实现依据与边界 |
| --- | --- | --- |
| `self.led.set_power(enabled)` | 用户LED开关 | 使用`/sys/class/leds/sys-heartbeat`，不直接抢占GPIOA6；首次控制记录原trigger/brightness |
| `self.led.get_status()` | 当前亮灭、触发模式 | 本次max_brightness=255，原trigger为activity；不假设只能写0/1 |
| `self.led.restore_system()` | 恢复系统原灯模式 | 正常停止及异常退出均需恢复；不能误控制B25补光灯 |
| `self.buttons.get_state()` | 读取板载功能键状态 | 识别gpio_keys对应event节点，通过只读evdev/ioctl，不构造会替换默认退出监听的Key对象 |
| `self.buttons.get_events()` | 返回近期按键事件 | 后台非独占监听、有界缓存；不使用EVIOCGRAB、不模拟按键、不触发关机 |
| `self.imu.read()` | 返回加速度、角速度等已验证数据 | MaixCAM2为LSM6DSOWTR、I²C1/0x6B；确认实际API与量纲后接入，不运行自动落盘校准 |
| `self.rtc.get_time()` | 返回RTC时间、系统时间及差值 | 内核rtc0当前为rtc-pcf8563 1-0051；统一UTC并给时区说明，不直接校时或改闹钟 |
| `self.adc.read_raw(channel)` | 返回ADC原始计数 | Linux `soc:adc` 暴露0–3四路raw文件；没有scale依据时不输出伏特数 |

ADC需特别区分：MaixPy v4.12.5教程不支持MaixCAM2 ADC，但这台设备的Linux IIO读数实际存在。本次读取到0–3通道分别约894、510、163、2，属于即时采样而非校准结果。可以用标准库读取原始数值，但这不证明PMOD上某引脚的电压量程、映射或5V兼容性。

IMU未调用`get_imu_info()`扫描总线；执行阶段已核对实际`read_all(calib_gryo=False, radian=False)`绑定，并使用明确的lsm6dsowtr/I²C1/0x6B、2g/250dps/104Hz参数。静止与改变朝向读数已验证；未进行落盘校准，温度不伪报为0°C。

### 第二批：按已确认接线开放的外部I/O

| 类别 | 候选MCP能力 | 开放条件 |
| --- | --- | --- |
| GPIO | 读取、写入、状态/模式查询 | 仅明确引出的白名单引脚；固定允许模式和初始状态，保留退出恢复方案 |
| I²C | 指定设备寄存器读取/写入 | 配置逻辑设备名→总线/地址/允许寄存器；限制长度、超时及并发；不默认扫描内部总线 |
| SPI | 指定设备收发 | 明确bus、CS、模式、频率和外设协议；有界字节数；真实外设或回环验证 |
| UART | 指定端口读写 | 明确串口、波特率、协议及占用者；有限超时/长度，不抢系统控制台或SDK通信 |

外接功能不应在接线未知时返回“可用”。没有配置目标时只报告未配置/受保护，不把拒绝操作当作硬件实测通过。

## 当前资源冲突与候选映射

- 用户LED：A6，内核LED驱动已管理；补光灯：B25/PWM6，由当前小智模块拥有。
- I²C1已绑定触摸`0x6A`、RTC`0x51`、电池`0x62`，官方IMU为`0x6B`；只允许专用驱动访问自己的芯片，不作为通用读写/全扫描入口。
- I²C0用于相机相关驱动，保留。
- I²C8在本机是Slave adapter，已有slave-24c02；不能因存在`/dev/i2c-8`就把它当空闲主机总线。
- 官方引脚图已确认：左侧2×6插座的I²C为I²C7，A8=SCL、A9=SDA；侧面6pin的I²C为I²C6，A1=SCL、A0=SDA，与SPI1复用冲突。实际占用仍须单独核对。
- SPI2候选B18=MOSI、B19=MISO、B20=SCK、B21=CS1；本机有`/dev/spidev2.1`。SPI1的A2在官方硬件说明中有封孔限制，不优先选用。
- UART0是系统控制台，保留。UART4=A21/A22已被Maix默认通信初始化使用，不能直接抢占。UART2=B0/B1可作为待核实的外接候选。
- 用户提供的官方Pins v1.0图标注Func/boot为C2；不得直接照搬某版本源码中的D2命名。按键实现以实际gpio_keys/evdev事件为准，避免用未经确认的GPIO编号操作。
- 右侧插座A30的PWM6与B25补光灯共用PWM6控制器；本项目已使用补光灯，不同时开放A30/PWM6。
- `pinmap.get_pin_function()`只能说明复用状态，不能单凭它认定没有其他进程占用。
- GPIO信号按官方3.3V逻辑约束处理；ADC量程另行验证，不从一代资料推导。

## 实施结构

1. 使用独立`peripheral_tools.py`、`imu_adapter.py`、`io_tools.py`，避免继续扩大摄像头/补光灯模块；硬件懒初始化，不在import时触碰。
2. 第一批专用工具复用当前MCP调度和底部工具名提示，阻塞采样放到受控工作线程。
3. LED保存/恢复归该模块管理；异常退出恢复记录只写受限运行目录。按键监听不替换既有处理器。
4. 外部I/O以本地配置中的逻辑目标为入口，RPC只选择已配置目标，不接受任意设备路径。
5. 工具数量增加后验证`tools/list`真实分页和UTF-8字节预算，不能只验证本地注册列表。
6. 更新部署复制清单、停止钩子和文档，保留现有拍照、3秒预览、语音与灯控制回归。

## 验收门槛

- 单元测试先红后绿：参数类型/范围、无设备、禁止的引脚/地址、超时、短读写、并发、退出恢复及分页。
- 用户LED实际亮灭、读回并恢复activity模式；不改变补光灯状态。
- Func真实短按记录可由MCP查询，同时原有按键功能不被接管；电源键不做关机行为测试。
- IMU静止与改变朝向采样，确认轴向、单位、时间戳，不把初始化成功当成传感器验收。
- RTC两次读取时间合理前进；不写RTC/系统时间。
- ADC原始值可读取；没有已知电压源和量程依据时，只验收原始计数。
- GPIO需要电平读回/仪表或明确回环；I²C/SPI/UART需要真实目标或实际回环响应，mock测试不替代电气验证。
- 实机MCP调用后原有摄像头、预览、语音/屏幕和网络保持可用。

### 执行结果与用户调整

- 主机及板端93项Python测试通过；显示加载器和触摸筛选两组原生回归通过。
- 板载工具及io.list_resources真实云端调用成功，LED开关读回/恢复与异常退出恢复通过；RTC推进、ADC四路原始计数、IMU静止/改变朝向、Func实际短按均有设备证据。
- Func曾设置后台SDK退出标志并停采，已只移除后台进程自己的默认退出监听；遇到共享powerkey节点拒绝未经验证的清理。其他进程的原按键行为保留。原C注入层已改为仅独占触摸设备。
- 修复后在弹窗显示期间记录两次按下/释放；小智PID未变，麦克风计数继续增长。
- GPIO/I²C未知复用按预期拒绝；SPI/UART做了空接口控制器检查，但没有真实外部设备协议验收。
- 用户明确要求状态灯肉眼观察先不管（位于壳内不可见）；该项从本轮验收中排除，不声称已光学确认亮灭。
- 部署文件/运行状态最终核对通过；26工具单页返回6040 UTF-8字节，未超过8000字节预算。

## 已澄清的外接范围

用户已说明：图片中没有外接设备，提供ToF/热成像链接是作为外接设备的案例参考。本阶段按空闲扩展接口设计，不再要求用户提供一个尚未连接的模块型号或接线。

板载LED、按键、IMU、RTC和可确认的ADC读数可以独立安排实现与实测。GPIO/I²C/SPI/UART准备接口映射、资源占用检查和白名单能力；没有目标外设时，软件测试和接口可用性不能冒充外设端到端通信成功。以后实际接入模块时，再登记型号、供电、接线和协议。

## 用户图片与官方引脚图补充核对

用户提供了设备正面实物照片、MaixCAM2 Pins v1.0图，以及[官方产品页](https://wiki.sipeed.com/hardware/zh/maixcam/maixcam2.html)，并已明确图片没有外接设备。这些资料用于确认接口与扩展能力，不是现有外设清单。

| 位置 | 功能 | 官方图标注 |
| --- | --- | --- |
| 正面左侧 | I²C7 | SCL=A8，SDA=A9 |
| 正面左侧 | UART2 | TX=B0，RX=B1 |
| 正面左侧 | SPI2/CS1 | MOSI=B18，MISO=B19，SCK=B20，CS1=B21 |
| 正面右侧 | PWM4/5/6/7 | PWM4=B2，PWM5=B3，PWM6=A30，PWM7=A31 |
| 正面右侧 | UART0 | TX=A28，RX=A29，保留系统控制台用途 |
| 正面右侧 | UART1/UART3复用 | UART1 TX=A30/RX=A31；UART3 TX=B2/RX=B3 |
| 正面右侧 | 其他SPI复用 | A4=SPI1_SCK；A2=SPI1_CS0；B2/B3可复用SPI2 MOSI/MISO；A30可复用SPI2_CS3 |
| 侧面6pin | I²C6/UART4 | A1=SCL、A0=SDA；A21=TX、A22=RX |
| 板载 | 指示灯、功能键、补光灯 | A6、C2、B25/PWM6 |

同一个引脚的表列功能是互斥复用，不表示能够同时使用。官方页面明确A2默认封孔用于防呆，实物照片也标为X；不解除封孔或默认开放这个位置。

图中5V脚为电源脚：USB/5V供电时约随输入3.7–5V，单电池时约3.7–4.2V。不能从“插座有5V供电”推导信号脚支持5V输入。

## ToF／热成像参考案例

用户提供的[官方ToF／热成像模块页](https://wiki.sipeed.com/hardware/zh/maixcam/tof_thermal.html)作为未来外设适配参考：

| 模块 | 页面标明的接口 | 可参考的MCP用途 |
| --- | --- | --- |
| PMOD_Thermal32 | I²C，32×24 | 温度数据、最高/最低温度、热图 |
| PMOD_TOF100 | SPI，支持多种深度分辨率 | 距离、指定区域深度、深度图 |
| PMOD_Thermal160 | 图像UART/USB，控制I²C | 热图、温度状态及模块控制 |
| PMOD_Thermal256 | 图像SPI，控制I²C | 热图、区域温度及模块控制 |

这些是候选适配示例，未声明本机已经安装、驱动或测试了上述模块；具体供电、安装位置与驱动参数应按选定模块的官方示例核实。

## 来源与本次证据

本次只读证据在`.runtime/peripherals-mcp/preflight.json`和下载的SDK类型声明；没有改LED模式、构造按键对象、初始化IMU、改RTC或执行总线扫描/写入。

官方依据：[GPIO v4.12.5](https://github.com/sipeed/MaixPy/blob/v4.12.5/docs/doc/zh/peripheral/gpio.md)、[按键](https://github.com/sipeed/MaixPy/blob/v4.12.5/docs/doc/zh/peripheral/key.md)、[IMU](https://github.com/sipeed/MaixPy/blob/v4.12.5/docs/doc/zh/modules/ahrs.md)、[ADC](https://github.com/sipeed/MaixPy/blob/v4.12.5/docs/doc/zh/peripheral/adc.md)、[I²C](https://github.com/sipeed/MaixPy/blob/v4.12.5/docs/doc/zh/peripheral/i2c.md)、[SPI](https://github.com/sipeed/MaixPy/blob/v4.12.5/docs/doc/zh/peripheral/spi.md)、[UART](https://github.com/sipeed/MaixPy/blob/v4.12.5/docs/doc/zh/peripheral/uart.md)。
