# Kuantix

本地量化研究工作台。基于 [easy-tdx](https://pypi.org/project/easy-tdx/) `1.20.3` **只读复用**上游行情能力，
在其之上构建数据湖（L1 vipdoc）、因子库（L2 parquet）、选股与实时监控。

## 设计红线

| 红线 | 内容 |
| --- | --- |
| NF-1 | 上游 `easy_tdx` 包与 `~/.easy_tdx/` **绝对只读**；全部上游调用收敛在 `Kuantix/adapters/` |
| NF-25 | 证券系数表 `_SECURITY_COEFFICIENTS` 必须 **import 引用**，严禁复制常量 |
| NF-26 | fail-loud：严禁静默兜底 / 静默降级 / 静默默认值 |
| NF-5/NF-7 | 市场规则一律经 `MarketProfile`；港美股为显式占位，调用即抛错，不做 A 股降级 |
| NF-9/NF-12 | 统一信封 `{code, message, data, meta}`；JSON 禁 NaN/Infinity，浮点 6 位 |
| NF-27 | 写盘后必须回读比对；不一致进隔离区 |

## 目录结构

```
Kuantix/
├── config.py          # TOML 配置 + Kuantix__SECTION__KEY 环境变量覆盖
├── cli.py             # Typer CLI 根骨架（全局 --json）
├── main.py            # Kuantix serve：FastAPI 应用工厂
├── core/              # 契约层（不得 import easy_tdx）
│   ├── fail_loud.py   #   fail-loud 异常与断言族
│   ├── envelope.py    #   统一信封 + 数值安全序列化
│   ├── market.py      #   MarketProfile（CN 完整 / HK·US 占位）
│   ├── contracts.py   #   Bar / Security / Quote / Alert / ...
│   ├── plugins.py     #   插件注册表
│   └── eventbus.py    #   进程内事件总线
└── adapters/          # 全项目唯一 import easy_tdx 的包
    ├── coefficients.py   # NF-25 系数 import 引用 + UNKNOWN 拒绝
    ├── tdx_client.py     # 三链路工厂（MacClient / TdxClient / MacExClient）
    ├── universe.py       # 全市场枚举（每页新连接，RD-10）
    ├── quotation.py      # K 线/报价（vol 股→手 ÷100，RD-8）
    ├── vipdoc_writer.py  # 落盘（uint32 上界 + 自补 fsync + 回读校验）
    └── known_hosts.py    # 只读加载上游 known_hosts（NF-20）
```

## 安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## 快速开始

```bash
Kuantix --help                 # 命令骨架
Kuantix version --json         # 版本 + 上游锁定版本
Kuantix doctor --json          # 环境体检（上游版本 / 系数表 / 市场 / 目录隔离）
Kuantix config show            # 解析后的完整配置
Kuantix serve --dry-run --json # 打印将挂载的路由
Kuantix serve                  # 起 REST 服务（默认 127.0.0.1:8899）
```

## 配置

默认读取（按顺序）：`$Kuantix_CONFIG` → `./config.toml` → `~/.Kuantix/config.toml` → 包内置模板。
任意键都可用环境变量覆盖：

```bash
Kuantix__SERVER__PORT=9001 Kuantix serve
Kuantix__PATHS__ROOT=/tmp/Kuantix Kuantix doctor
```

## 测试

```bash
pytest                                  # 批次 1 验收用例（Kuantix/tests/）
pytest -c /dev/null tests/redlines -q   # 架构红线静态检查
pytest -m network                       # 需外网 TDX 服务器的用例
```

## 许可

MIT
