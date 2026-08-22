# spikes/ 目录说明（验证性质，不作编码规范参考）

本目录存放 Kuantix 立项早期的**技术验证 Spike**（S1–S5），用于回答
「上游 easy_tdx 能不能拉数 / 怎么拉 / 系数对不对 / 有没有性能瓶颈」等
架构前置问题。它们**不是产品代码**，也不代表编码规范。

## ⚠️ 重要边界

- **验证性质**：spike 以「最快跑通、证明结论」为目标，大量使用
  `dict.get(key, 默认值)`、直接 `import easy_tdx`、硬编码主机/系数等写法。
  这些写法在正式代码里**违反红线**（R4 fail-loud / R2 上游收敛 / R6 硬编码），
  只因为 `tests/redlines/_scan.py` 把 `spikes/` 排除在强制扫描之外，
  才以 **ADVISORY（不阻断）** 形式展示。
- **不作为编码规范参考**：正式实现一律以
  `Kuantix/`（业务层）、`Kuantix/adapters/`（适配层）、`docs/`（设计文档）、
  `tests/`（单测 + 验收）为唯一依据。请勿把本目录的写法搬进业务代码。
- **刻意保留的反面样本**：`S3_vipdoc_e2e.py` 第 5b 步含一处**故意错配**
  的系数 `(0.01, 0.01)`（ETF sh510300 应使用基金系数 `(0.001, 1.0)`），
  用于复现 T1 系数陷阱并验证红线检查器 R1 能抓到。该处**禁止删除、禁止改逻辑**，
  代码块上方已有醒目注释说明；正式系数必须经
  `Kuantix/adapters/coefficients.py` 从上游 import 获取（NF-25）。

## 目录内容

| 文件 | 验证内容 | 结论 |
|---|---|---|
| `S1_api_signature.py` | easy_tdx 客户端接口签名 / 连接方式 | 见 `results/` |
| `S2_throughput.py` | 池化 vs 新建连接的拉取吞吐 | 池化 0.06s/只（S2 实测） |
| `S3_vipdoc_e2e.py` | vipdoc 写→读回端到端 + T1 系数陷阱 | RD-1 正面通过；反面复现 ×0.10 |
| `S4_hk_us.py` | 港美股扩展市场拉取可行性 | 走 `MacExClient(7727)` |
| `S5_factor_smoke.py` | 上游因子引擎冒烟 | 见 `results/` |
| `common.py` | spike 共用工具（含 ADVISORY 预警写法，勿照抄） | — |
| `run_all.py` | 批量运行全部 spike | — |

> 运行 spike 需要真实网络与上游服务器，结果存档于 `results/`；
> 本项目测试/验收**不依赖** spike 运行结果。
