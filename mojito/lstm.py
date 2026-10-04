"""LSTM water level model (proposal section 4.2).

One network predicts all five horizons at once. Like the tabular models it
predicts the change from today's X.44 level, so it can forecast above the
highest level seen in training. Rain forecasts, when used, join after the
LSTM: they describe the days ahead, not the sequence behind.
"""

import numpy
import pandas
import torch
from torch import nn

from mojito import features

LOOKBACK_DAYS = 30


class Scaler:
    """Standardise with training statistics; missing values become 0 (= mean)."""

    def fit(self, values):
        self.mean = numpy.nanmean(values, axis=0)
        self.std = numpy.nanstd(values, axis=0)
        self.std[~numpy.isfinite(self.std) | (self.std == 0)] = 1.0
        self.mean[~numpy.isfinite(self.mean)] = 0.0
        return self

    def transform(self, values):
        scaled = (values - self.mean) / self.std
        return numpy.nan_to_num(scaled, nan=0.0)


def make_sequences(table, feature_names, rain_columns=(), days=None, lookback=LOOKBACK_DAYS):
    """Sliding windows ending on each of `days` (default: all) with an X.44 level.

    The window may reach back before the first of `days`, so pass the whole
    table and select the split with `days`. Returns
    (sequences [n, lookback, f], rain [n, r], targets [n, 5], end days)."""
    values = table[feature_names].to_numpy(dtype=float)
    rain = table[list(rain_columns)].to_numpy(dtype=float) if rain_columns else numpy.zeros((len(table), 0))
    targets = table[[f"target_delta_h{h}" for h in features.HORIZONS]].to_numpy(dtype=float)
    selected = table["x44_max"].notna().to_numpy().copy()
    if days is not None:
        selected &= table.index.isin(days)

    ends = [i for i in range(lookback - 1, len(table)) if selected[i]]
    sequences = numpy.stack([values[i - lookback + 1 : i + 1] for i in ends])
    return sequences, rain[ends], targets[ends], table.index[ends]


class FloodLSTM(nn.Module):
    def __init__(self, n_features, n_rain, hidden=64, dropout=0.2):
        super().__init__()
        self.lstm = nn.LSTM(n_features, hidden, batch_first=True)
        self.head = nn.Sequential(
            nn.Dropout(dropout),
            nn.Linear(hidden + n_rain, hidden),
            nn.ReLU(),
            nn.Linear(hidden, len(features.HORIZONS)),
        )

    def forward(self, sequence, rain):
        _, (hidden, _) = self.lstm(sequence)
        return self.head(torch.cat([hidden[-1], rain], dim=1))


class LSTMForecaster:
    """Fit / predict wrapper with the same "level at t+h" output as HorizonModel."""

    def __init__(self, feature_names, rain_columns=(), hidden=64, dropout=0.2, lr=1e-3,
                 high_water_weight=4.0, batch_size=64, seed=0):
        self.feature_names = list(feature_names)
        self.rain_columns = list(rain_columns)
        self.hidden, self.dropout, self.lr = hidden, dropout, lr
        self.high_water_weight = high_water_weight
        self.batch_size = batch_size
        self.seed = seed

    def _tensors(self, table, days=None):
        sequences, rain, targets, days = make_sequences(table, self.feature_names, self.rain_columns, days)
        n, steps, width = sequences.shape
        sequences = self.feature_scaler.transform(sequences.reshape(-1, width)).reshape(n, steps, width)
        rain = self.rain_scaler.transform(rain) if self.rain_columns else rain
        level_now = table.loc[days, "x44_max"].to_numpy()
        return (
            torch.tensor(sequences, dtype=torch.float32),
            torch.tensor(rain, dtype=torch.float32),
            targets,
            level_now,
            days,
        )

    def _loss(self, prediction, target, level_now):
        """MSE over known targets; high-water days weigh more (they are rare)."""
        known = ~torch.isnan(target)
        future_level = torch.nan_to_num(target * self.target_std + level_now[:, None], nan=0.0)
        weight = 1.0 + self.high_water_weight * (future_level >= 3.0).float()
        error = (prediction - torch.nan_to_num(target)) ** 2 * weight
        return (error * known).sum() / known.sum().clamp(min=1)

    def fit(self, train_table, train_days, epochs=60, validation_table=None, validation_days=None):
        """Train on windows ending on `train_days`.

        With a validation set, keep the weights of the epoch with the lowest
        validation loss (early stopping) and remember that epoch count. The
        validation table may differ from the training table, e.g. forecast
        rain instead of observed rain."""
        torch.manual_seed(self.seed)
        numpy.random.seed(self.seed)

        sequences, rain, targets, _ = make_sequences(train_table, self.feature_names, self.rain_columns, train_days)
        self.feature_scaler = Scaler().fit(sequences.reshape(-1, sequences.shape[-1]))
        self.rain_scaler = Scaler().fit(rain) if self.rain_columns else None
        self.target_std = float(numpy.nanstd(targets)) or 1.0

        x, r, y, level_now, _ = self._tensors(train_table, train_days)
        y = torch.tensor(y / self.target_std, dtype=torch.float32)
        level_now = torch.tensor(level_now, dtype=torch.float32)

        self.network = FloodLSTM(x.shape[-1], r.shape[-1], self.hidden, self.dropout)
        optimiser = torch.optim.Adam(self.network.parameters(), lr=self.lr, weight_decay=1e-4)

        if validation_table is not None:
            vx, vr, vy, vlevel, _ = self._tensors(validation_table, validation_days)
            vy = torch.tensor(vy / self.target_std, dtype=torch.float32)
            vlevel = torch.tensor(vlevel, dtype=torch.float32)

        self.history = []
        best_state, best_loss = None, numpy.inf
        for epoch in range(epochs):
            self.network.train()
            order = torch.randperm(len(x))
            for start in range(0, len(x), self.batch_size):
                batch = order[start : start + self.batch_size]
                optimiser.zero_grad()
                loss = self._loss(self.network(x[batch], r[batch]), y[batch], level_now[batch])
                loss.backward()
                nn.utils.clip_grad_norm_(self.network.parameters(), 1.0)
                optimiser.step()

            if validation_table is not None:
                self.network.eval()
                with torch.no_grad():
                    validation_loss = float(self._loss(self.network(vx, vr), vy, vlevel))
                self.history.append(validation_loss)
                if validation_loss < best_loss:
                    best_loss = validation_loss
                    best_state = {k: v.clone() for k, v in self.network.state_dict().items()}
                    self.best_epoch = epoch + 1

        if best_state is not None:
            self.network.load_state_dict(best_state)
        return self

    def predict(self, table, days=None):
        """DataFrame of predicted levels, columns h1..h5, indexed by issue day."""
        x, r, _, level_now, days = self._tensors(table, days)
        self.network.eval()
        with torch.no_grad():
            delta = self.network(x, r).numpy() * self.target_std
        return pandas.DataFrame(
            delta + level_now[:, None], index=days, columns=[f"h{h}" for h in features.HORIZONS]
        )
