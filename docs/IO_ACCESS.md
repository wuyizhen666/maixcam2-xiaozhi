# 外部 I/O 开放规则

`xiaozhi/service/io_tools.py` 提供 GPIO、I2C、SPI、UART 的受限逻辑目标接口。策略默认路径为设备 `/root/xiaozhi/io_access.json`，首次构造接口时加载一次；修改策略后须重新初始化服务才能生效。默认部署内容：

```json
{"version": 1, "targets": {}}
```

缺失策略文件视为未配置，拒绝所有目标；已有但不合法的策略报错，不静默放行。策略必须是普通本地文件，拒绝符号链接；Linux 上要求 root 或服务用户拥有，且不能 group/world writable。文件上限 64 KiB，重复 JSON 键、未知字段、目标引脚冲突均拒绝。目标名匹配 `[A-Za-z][A-Za-z0-9_-]{0,63}`。RPC 只能引用目标名，不能传任意设备路径、地址或引脚来绕过策略。

## 配置示例（未部署启用）

以下仅展示源码接受的结构；不表示这些引脚当前可用或设备已连接。必须先确认电气参数、原有 pinmux、现有 SPI 模式和占用情况，再按实际设备编写策略。实现绝不自动切换 mux。

```json
{
  "version": 1,
  "targets": {
    "switch_input": {"kind": "gpio", "pin": "A4", "direction": "input"},
    "logic_output": {"kind": "gpio", "pin": "A31", "direction": "output", "initial": 0, "safe": 0},
    "sensor": {"kind": "i2c", "bus": 7, "address": 64, "read_registers": [0, 1], "write_registers": [1]},
    "spi_device": {"kind": "spi", "device": "/dev/spidev2.1", "mode": 0, "speed_hz": 100000},
    "serial_device": {"kind": "uart", "port": 2, "baud": 115200}
  }
}
```

GPIO 白名单为 A4、A8、A9、B0、B1、B2、B3、B18、B19、B20、B21、A31。输入目标只有 `kind/pin/direction`；输出另须 `initial/safe`，均为整数 0 或 1。所有目标不得复用同一引脚。

| 接口 | 固定范围与已有 mux 要求 | 事务边界 |
| --- | --- | --- |
| GPIO | 白名单引脚，功能必须为对应 `GPIO` + 引脚名，例如 `GPIOA4` | 输入/输出由策略固定；输出写值 0/1 |
| I2C | 仅外部 6 或 7；6 为 A1/I2C6_SCL、A0/I2C6_SDA；7 为 A8/I2C7_SCL、A9/I2C7_SDA；地址 0x08–0x77 | 仅 8-bit 寄存器地址，起始 0–255；连续 1–256 字节不能越过 255，整个范围必须在相应读/写白名单中；不扫描内部总线 |
| SPI | 仅 `/dev/spidev2.1`；B18/SPI2_MOSI、B19/SPI2_MISO、B20/SPI2_SCK、B21/SPI2_CS1 | 策略 mode 0–3，须与现有模式相同；10,000–1,000,000 Hz；收发 1–256 字节 |
| UART | 仅 UART2 `/dev/ttyS2`；B0/UART2_TX、B1/UART2_RX | 波特率 9600/19200/38400/57600/115200；8N1；1–256 字节；读取超时 1–1000 ms，默认 200 ms |

写入/收发的 `data_hex` 必须为 1–256 字节的连续十六进制对，不带空格或 `0x`。读取要求恰好取得请求长度，超时、短读及短写均失败。I2C 不支持 16-bit 寄存器地址或任意设备命令协议，需要时应另行设计专用适配器。

## 占用、超时和清理限制

每次操作先验证现有 pinmux；未知或不匹配立即拒绝。I2C 地址被内核驱动占用时拒绝。打开非 GPIO 设备前后检查 `/proc` 占用并持有 flock；GPIO 使用内核 line handle，UART 使用 `TIOCEXCL`。I2C/SPI 没有内核排他打开机制，proc 检查与 flock 不能防止不合作的特权/root 进程竞态，因此不能声称绝对独占总线。

同一接口对象串行处理事务，忙时拒绝新操作。RPC 超时不能取消已经开始的内核事务，超时后不能推定设备没有收到写入；写操作不自动重试，避免重复副作用。

正常 `close()` 对 GPIO 输出尝试写入配置的 `safe` 值再释放，并恢复 UART 原始属性；清理失败会报告错误。该安全值不保证 SIGKILL、断电或释放 line handle 后的电平，必须由外部电路及具体负载设计承担失效保护。

本轮没有外接器件；GPIO/I2C 未知 mux 拒绝、SPI `ff` 与 UART 无对端短读结果只属于保护/控制器验证。实测证据及未完成的外设验收见 [阶段验收报告](PERIPHERAL_MCP_VALIDATION.md)。
