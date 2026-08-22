/**
 * 评级系统自检脚本（Node 内置 test runner）。
 *
 * 评级逻辑是纯函数 + 零 DOM 依赖，可直接 import ESM TypeScript（Node v22+ 原生 type-stripping）。
 * 运行：node --test src/grading/__tests__/grade.test.ts
 *
 * 关键断言：京东方案例（126.43% 收益但胜率 35.56%、回撤 41.65%、卡玛 0.336）
 * 必须落在 D 档——这是产品诉求的核心验证点。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { gradePerformance, gradeGridPoint, gradePortfolio } from '../index.ts';
import { interpolate, scoreToGrade } from '../engine.ts';
import { THRESHOLDS } from '../thresholds.ts';
import { computeCombinedMetrics } from '../combinedMetrics.ts';
import type { BacktestEquityPoint } from '../../types/backtest.ts';
import type { GridPointInput, SinglePerformanceInput } from '../index.ts';

// ── 京东方案例（用户提供的真实回测数据）──────────────────────────────────────
const BOE_PERF: SinglePerformanceInput = {
  total_return: 1.2643,
  annual_return: 0.1401,
  max_drawdown: 0.4165,
  max_dd_duration: 1,
  sharpe: 0.529,
  sortino: 0.825,
  calmar: 0.336,
  total_trades: 90,
  win_trades: 32,
  lose_trades: 58,
  rejected_trades: 0,
  win_rate: 0.3556,
  profit_factor: 1.107,
  avg_win: 0.0444,
  avg_loss: -0.0203,
  max_win: 0.2554,
  max_loss: -0.05,
  avg_holding_days: 11.222,
  volatility: 0.2496,
};

test('京东方回测必须评为 D 档（用户核心诉求验证点）', () => {
  const r = gradePerformance(BOE_PERF);
  assert.equal(r.grade, 'D', `期望 D，实际 ${r.grade}（分数 ${r.score}）`);
});

test('京东方评分应在 25-43 区间（C 与 D 的边界）', () => {
  const r = gradePerformance(BOE_PERF);
  assert.ok(r.score >= 25 && r.score < 43, `分数 ${r.score} 不在 D 档合理区间`);
});

test('低利润因子触发系统亏损否决', () => {
  const r = gradePerformance({ ...BOE_PERF, profit_factor: 0.95 });
  assert.equal(r.grade, 'D');
  assert.equal(r.isLosing, true);
  assert.ok(r.vetoes.some((v) => v.key === 'losing_system'));
});

test('样本不足（< 10 笔交易）降权但不否决整个评级', () => {
  const r = gradePerformance({
    ...BOE_PERF,
    total_trades: 6,
    sharpe: 1.2,
    max_drawdown: 0.2,
    calmar: 1.5,
    volatility: 0.15,
    win_rate: 0.5,
    profit_factor: 1.5,
  });
  assert.equal(r.insufficientSample, true, '应标记样本不足');
  assert.ok(['A', 'B', 'S'].includes(r.grade), `高夏普长线策略不应因交易少被打到 D，实际 ${r.grade}`);
  const wr = r.dimensions.find((d) => d.key === 'win_rate');
  const pf = r.dimensions.find((d) => d.key === 'profit_factor');
  assert.equal(wr?.weight, 0, 'win_rate 权重应降为 0');
  assert.equal(pf?.weight, 0, 'profit_factor 权重应降为 0');
});

test('6年6笔交易 + 高夏普 → 应得 A/B（核心回归测试）', () => {
  const longTermGood: SinglePerformanceInput = {
    total_return: 1.8,
    annual_return: 0.103,
    max_drawdown: 0.18,
    sharpe: 1.4,
    calmar: 0.57,
    total_trades: 6,
    win_rate: 0.8333,
    profit_factor: 2.5,
    volatility: 0.12,
  };
  const r = gradePerformance(longTermGood);
  assert.ok(['A', 'B', 'S'].includes(r.grade), `实际 ${r.grade}（分数 ${r.score}）`);
});

test('深回撤 >60% → 直接 D（一票否决）', () => {
  const r = gradePerformance({ ...BOE_PERF, max_drawdown: 0.65 });
  assert.equal(r.grade, 'D');
  assert.ok(r.vetoes.some((v) => v.key === 'deep_drawdown'));
});

test('高回撤 50%-60% → 最高 B', () => {
  const r = gradePerformance({ ...BOE_PERF, max_drawdown: 0.55, profit_factor: 2.2, win_rate: 0.5 });
  assert.ok(r.vetoes.some((v) => v.key === 'high_drawdown'));
  assert.ok(['B', 'C', 'D'].includes(r.grade), `实际 ${r.grade}`);
  assert.notEqual(r.grade, 'A');
  assert.notEqual(r.grade, 'S');
});

test('低胜率 <25% 且样本充足 → 直接 D', () => {
  const r = gradePerformance({ ...BOE_PERF, win_rate: 0.2 });
  assert.equal(r.grade, 'D');
  assert.ok(r.vetoes.some((v) => v.key === 'very_low_winrate'));
});

test('微利 1.0 <= profit_factor < 1.2 → 最高 B', () => {
  const r = gradePerformance({ ...BOE_PERF, profit_factor: 1.1 });
  assert.ok(r.vetoes.some((v) => v.key === 'thin_edge'));
  assert.notEqual(r.grade, 'A');
  assert.notEqual(r.grade, 'S');
});

test('优秀策略 → S/A 档', () => {
  const good: SinglePerformanceInput = {
    total_return: 2.0,
    annual_return: 0.2,
    max_drawdown: 0.08,
    sharpe: 2.2,
    calmar: 2.5,
    total_trades: 120,
    win_rate: 0.62,
    profit_factor: 2.8,
    volatility: 0.15,
  };
  const r = gradePerformance(good);
  assert.ok(['S', 'A'].includes(r.grade), `实际 ${r.grade}（分数 ${r.score}）`);
});

test('interpolate 边界：越界取端点，区间线性插值', () => {
  // 0.4165 落在 (0.4→30, 0.5→15) 之间：30 - (0.4165-0.4)/0.1*15 = 27.525
  assert.ok(Math.abs(interpolate(THRESHOLDS.max_drawdown.anchors, 0.4165) - 27.525) < 1e-9);
  // 0.529 落在 (0.5→40, 0.8→55) 之间：40 + (0.529-0.5)/0.3*15 = 41.45
  assert.ok(Math.abs(interpolate(THRESHOLDS.sharpe.anchors, 0.529) - 41.45) < 1e-9);
  assert.equal(interpolate(THRESHOLDS.max_drawdown.anchors, 0.7), 0);
  assert.equal(interpolate(THRESHOLDS.sharpe.anchors, 5), 100);
});

test('scoreToGrade 阈值边界', () => {
  assert.equal(scoreToGrade(88), 'S');
  assert.equal(scoreToGrade(87.9), 'A');
  assert.equal(scoreToGrade(73), 'A');
  assert.equal(scoreToGrade(58), 'B');
  assert.equal(scoreToGrade(43), 'C');
  assert.equal(scoreToGrade(42.9), 'D');
});

// ── 组合评级（净值重算） ────────────────────────────────────────────────────

function makeEquity(prices: number[]): BacktestEquityPoint[] {
  let peak = 0;
  return prices.map((total, i) => {
    peak = Math.max(peak, total);
    return {
      datetime: `2020-01-${String(i + 1).padStart(2, '0')}`,
      total,
      drawdown: Math.round((peak - total) * 1000) / 1000,
      drawdown_pct: peak > 0 ? Math.round(((peak - total) / peak) * 1000) / 1000 : 0,
    };
  });
}

test('组合净值重算：总收益/回撤/夏普口径', () => {
  // 简单 5 点上升曲线
  const equity = makeEquity([1.0, 1.02, 1.05, 1.03, 1.1]);
  const m = computeCombinedMetrics(equity);
  assert.ok(Math.abs(m.total_return - 0.1) < 1e-9, `总收益 ${m.total_return}`);
  assert.ok(m.max_drawdown > 0.018 && m.max_drawdown < 0.02, `回撤 ${m.max_drawdown}`);
  assert.ok(m.n_points === 5);
});

test('组合评级：深回撤净值 → 否决 D', () => {
  const equity = makeEquity([1.0, 0.3, 0.9]); // 回撤 70%
  const r = gradePortfolio(equity);
  assert.equal(r.grade, 'D');
  assert.ok(r.vetoes.some((v) => v.key === 'deep_drawdown'));
});

test('组合评级：点数不足 60 → 标记样本不足（不否决整体）', () => {
  const equity = makeEquity([1.0, 1.01, 1.02, 1.03]);
  const r = gradePortfolio(equity);
  assert.equal(r.insufficientSample, true);
  assert.ok(['S', 'A', 'B', 'C', 'D'].includes(r.grade));
});

// ── 寻优评级 ────────────────────────────────────────────────────────────────

const GOOD_POINT: GridPointInput = {
  params: { fast: 5, slow: 20 },
  total_return: 1.2,
  sharpe: 1.8,
  max_drawdown: 0.15,
  total_trades: 80,
  win_rate: 0.55,
  profit_factor: 2.1,
};

test('寻优评级：优秀网格点 → A/S', () => {
  const r = gradeGridPoint(GOOD_POINT);
  assert.ok(['S', 'A'].includes(r.grade), `实际 ${r.grade}（分数 ${r.score}）`);
});

test('寻优评级：低胜率网格点触发否决', () => {
  const r = gradeGridPoint({ ...GOOD_POINT, win_rate: 0.2 });
  assert.equal(r.grade, 'D');
  assert.ok(r.vetoes.some((v) => v.key === 'very_low_winrate'));
});

test('寻优评级：样本不足降权', () => {
  const r = gradeGridPoint({ ...GOOD_POINT, total_trades: 5 });
  assert.equal(r.insufficientSample, true);
  const wr = r.dimensions.find((d) => d.key === 'win_rate');
  assert.equal(wr?.weight, 0);
});
