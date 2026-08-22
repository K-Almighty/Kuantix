/**
 * 各评分维度的阈值映射表（Kuantix 实现，规格对齐 docs/06 §3.4 / 上游阈值锚点）。
 *
 * 每个维度用一组 (阈值, 分数) 锚点描述「值→分数」的对应关系。
 * 评分时做线性插值：值落在两个锚点之间时，按比例计算分数。
 *
 * 设计原则：
 * - 收益类指标（夏普/卡玛/利润因子/胜率）：越高越好，threshold 升序 + score 升序。
 * - 风险类指标（最大回撤/波动率/回撤持续）：越低越好，threshold 升序 + score 降序。
 *   即：所有锚点都按「指标值从好到差」排列，分数从高到低。
 *
 * 阈值集中在此文件，方便根据真实回测分布微调，无需动评分引擎。
 */

/** 锚点：一个 (指标原始值, 对应分数) 对。 */
export interface Anchor {
  threshold: number;
  score: number;
}

/** 单个维度的配置：标签 + 锚点列表。 */
export interface DimensionConfig {
  label: string;
  anchors: Anchor[];
}

/** 全部维度阈值表（key 即 performance 指标名，供 engine 消费）。 */
export const THRESHOLDS = {
  // ── 风险调整收益类（越大越好）──────────────────────────────────────────────

  /** 卡玛比率 = 年化收益 / 最大回撤。直接反映「套牢回本难度」。 */
  calmar: {
    label: '卡玛比率',
    anchors: [
      { threshold: 0.0, score: 0 },
      { threshold: 0.3, score: 20 },
      { threshold: 0.5, score: 35 },
      { threshold: 0.8, score: 50 },
      { threshold: 1.0, score: 65 },
      { threshold: 1.5, score: 80 },
      { threshold: 2.0, score: 90 },
      { threshold: 3.0, score: 100 },
    ],
  },

  /** 夏普比率。A股长期 >1 算不错，>2 优秀。 */
  sharpe: {
    label: '夏普比率',
    anchors: [
      { threshold: 0.0, score: 10 },
      { threshold: 0.3, score: 25 },
      { threshold: 0.5, score: 40 },
      { threshold: 0.8, score: 55 },
      { threshold: 1.0, score: 68 },
      { threshold: 1.5, score: 82 },
      { threshold: 2.0, score: 92 },
      { threshold: 3.0, score: 100 },
    ],
  },

  /** 索提诺比率（仅用下行波动）。组合评级用，阈值比夏普略宽松。 */
  sortino: {
    label: '索提诺比率',
    anchors: [
      { threshold: 0.0, score: 10 },
      { threshold: 0.5, score: 30 },
      { threshold: 1.0, score: 50 },
      { threshold: 1.5, score: 65 },
      { threshold: 2.0, score: 78 },
      { threshold: 2.5, score: 88 },
      { threshold: 4.0, score: 100 },
    ],
  },

  // ── 风险类（越小越好：threshold 升序，score 降序）─────────────────────────

  /** 最大回撤（小数，0.4165 = 41.65%）。深回撤 = 套牢难回本。 */
  max_drawdown: {
    label: '最大回撤',
    anchors: [
      { threshold: 0.0, score: 100 },
      { threshold: 0.1, score: 88 },
      { threshold: 0.15, score: 78 },
      { threshold: 0.2, score: 68 },
      { threshold: 0.25, score: 58 },
      { threshold: 0.3, score: 48 },
      { threshold: 0.4, score: 30 },
      { threshold: 0.5, score: 15 },
      { threshold: 0.6, score: 0 },
    ],
  },

  /** 波动率（年化，小数）。持有过程的颠簸程度。 */
  volatility: {
    label: '波动率',
    anchors: [
      { threshold: 0.0, score: 100 },
      { threshold: 0.1, score: 85 },
      { threshold: 0.15, score: 75 },
      { threshold: 0.2, score: 62 },
      { threshold: 0.25, score: 50 },
      { threshold: 0.3, score: 38 },
      { threshold: 0.4, score: 22 },
      { threshold: 0.6, score: 0 },
    ],
  },

  /** 回撤持续天数。长期套牢的核心指标（组合评级展示参考，不参与单标的加权）。 */
  max_dd_duration: {
    label: '回撤持续',
    anchors: [
      { threshold: 0, score: 100 },
      { threshold: 30, score: 80 },
      { threshold: 90, score: 62 },
      { threshold: 180, score: 45 },
      { threshold: 365, score: 28 },
      { threshold: 730, score: 10 },
      { threshold: 1095, score: 0 },
    ],
  },

  // ── 交易质量类 ────────────────────────────────────────────────────────────

  /** 胜率（小数，0.3556 = 35.56%）。普通人拿不住低胜率品种。 */
  win_rate: {
    label: '胜率',
    anchors: [
      { threshold: 0.0, score: 0 },
      { threshold: 0.25, score: 12 },
      { threshold: 0.3, score: 22 },
      { threshold: 0.35, score: 32 },
      { threshold: 0.4, score: 45 },
      { threshold: 0.45, score: 58 },
      { threshold: 0.5, score: 70 },
      { threshold: 0.55, score: 82 },
      { threshold: 0.6, score: 92 },
      { threshold: 0.7, score: 100 },
    ],
  },

  /**
   * 利润因子（profit_factor）= 总盈利 / 总亏损的绝对值。
   * < 1 表示系统实际亏钱；1.0–1.2 勉强盈亏平衡；> 2 算健康。
   */
  profit_factor: {
    label: '利润因子',
    anchors: [
      { threshold: 0.0, score: 0 },
      { threshold: 0.8, score: 10 },
      { threshold: 1.0, score: 25 },
      { threshold: 1.2, score: 42 },
      { threshold: 1.5, score: 60 },
      { threshold: 1.8, score: 75 },
      { threshold: 2.0, score: 84 },
      { threshold: 2.5, score: 92 },
      { threshold: 3.0, score: 100 },
    ],
  },
} as const;

/** 便捷类型：所有维度配置的映射。 */
export type DimensionKey = keyof typeof THRESHOLDS;
