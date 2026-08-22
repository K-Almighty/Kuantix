"""深度学习时序因子（LSTM / Transformer），预测未来收益的 ML 信号。

依赖懒加载：仅在 ``compute`` 时 ``import torch``；未安装 torch 时给出清晰报错。
注册（import 阶段）不触碰 torch，故因子发现流程不受影响（R2/NF-1）。

红线注意（R2）：业务层禁止 ``import easy_tdx``。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from Kuantix.adapters.factor_bridge import Factor, register_factor

from .ml_common import build_features, forward_return_label

__all__ = ["DeepLSTMReturnFactor", "DeepTransformerReturnFactor"]

_SEQ_LEN = 20
_MIN_SAMPLES = 120
_REFIT_EVERY = 120
_TRAIN_WINDOW = 1000
_EPOCHS = 3


def _build_sequences(X: np.ndarray, y: np.ndarray, seq_len: int):
    """滑窗构造 (seq, target) 训练样本。"""
    seqs, targets = [], []
    for i in range(seq_len, len(X)):
        seqs.append(X[i - seq_len : i])
        targets.append(y[i])
    return np.array(seqs, dtype=np.float32), np.array(targets, dtype=np.float32)


def _make_model(kind: str, n_features: int, seq_len: int):
    """在 torch 已 import 的前提下构造模型（LSTM 或 Transformer）。"""
    import torch
    import torch.nn as nn

    class _Model(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            if kind == "transformer":
                self.embed = nn.Linear(n_features, 16)
                layer = nn.TransformerEncoderLayer(
                    d_model=16, nhead=2, dim_feedforward=32, batch_first=True
                )
                self.enc = nn.TransformerEncoder(layer, num_layers=1)
                self.head = nn.Linear(16, 1)
            else:
                self.lstm = nn.LSTM(
                    input_size=n_features, hidden_size=16, num_layers=1, batch_first=True
                )
                self.head = nn.Linear(16, 1)

        def forward(self, x):  # x: (B, seq_len, n_features)
            if kind == "transformer":
                h = self.embed(x)
                h = self.enc(h)
                return self.head(h[:, -1, :])
            out, _ = self.lstm(x)
            return self.head(out[:, -1, :])

    return _Model()


def _fit(X: np.ndarray, y: np.ndarray, kind: str) -> object:
    import torch

    seqs, targets = _build_sequences(X, y, _SEQ_LEN)
    if len(seqs) == 0:
        raise ValueError("序列样本不足")
    Xt = torch.tensor(seqs)
    yt = torch.tensor(targets).unsqueeze(1)
    model = _make_model(kind, X.shape[1], _SEQ_LEN)
    model.train()
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = torch.nn.MSELoss()
    for _ in range(_EPOCHS):
        opt.zero_grad()
        loss = loss_fn(model(Xt), yt)
        loss.backward()
        opt.step()
    model.eval()
    return model


def _predict(model, seq: np.ndarray) -> float:
    import torch

    t = torch.tensor(seq[None, ...], dtype=torch.float32)
    with torch.no_grad():
        return float(model(t).item())


class _DeepReturnFactor(Factor):
    """深度时序因子基类（LSTM / Transformer 共用滚动前向逻辑）。"""

    category = "ml"
    inputs = ("open", "high", "low", "close", "vol")
    _kind = "lstm"

    def compute(self, df: pd.DataFrame) -> pd.Series:
        import torch  # 明确依赖：torch 缺失时在此给出清晰报错（而非静默全 NaN）

        feats = build_features(df)
        label = forward_return_label(df, horizon=5)
        data = pd.concat(
            [feats, label.reindex(feats.index).rename("__y")], axis=1
        ).dropna()
        n = len(data)
        out = np.full(len(feats), np.nan, dtype=float)
        if n < _MIN_SAMPLES:
            return pd.Series(out, index=feats.index)

        feat_cols = list(feats.columns)
        X = data[feat_cols].to_numpy(dtype=float)
        y = data["__y"].to_numpy(dtype=float)
        idx = list(data.index)

        model = None
        last_fit = -10**9
        for pos in range(len(data)):
            if pos < _MIN_SAMPLES:
                continue
            if model is None or (pos - last_fit) >= _REFIT_EVERY:
                try:
                    lo = max(0, pos - _TRAIN_WINDOW)
                    model = _fit(X[lo:pos], y[lo:pos], self._kind)
                    last_fit = pos
                except Exception:  # noqa: BLE001
                    model = None
                    continue
            if model is None:
                continue
            seq = X[max(0, pos - _SEQ_LEN) : pos]
            if len(seq) < _SEQ_LEN:
                continue
            try:
                out[feats.index.get_indexer([idx[pos]])[0]] = _predict(model, seq)
            except Exception:  # noqa: BLE001
                continue
        return pd.Series(out, index=feats.index)


@register_factor
class DeepLSTMReturnFactor(_DeepReturnFactor):
    """LSTM 深度时序因子：LSTM 编码特征窗口，预测未来收益信号（需 torch）。"""

    name = "ml_lstm_return"
    display_name = "LSTM收益预测"
    description = "LSTM 深度时序预测因子：编码特征窗口预测未来收益（需安装 torch）"
    _kind = "lstm"


@register_factor
class DeepTransformerReturnFactor(_DeepReturnFactor):
    """Transformer 深度时序因子：Transformer 编码特征窗口，预测未来收益（需 torch）。"""

    name = "ml_transformer_return"
    display_name = "Transformer收益预测"
    description = (
        "Transformer 深度时序预测因子：编码特征窗口预测未来收益（需安装 torch）"
    )
    _kind = "transformer"
