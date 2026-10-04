"""Per-horizon water level models for X.44.

Every model predicts the daily maximum level at t+h. Models of kind "delta"
learn the change from today's level and add it back, so they can forecast
levels above anything seen in training, which tree models predicting the
level directly cannot.
"""

import json

import joblib
import lightgbm
import pandas
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from mojito import config

MODELS_DIR = config.DATA_DIR / "models"

LGBM_DEFAULTS = {
    "n_estimators": 400,
    "learning_rate": 0.03,
    "num_leaves": 15,
    "min_child_samples": 20,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "random_state": 42,
    "verbose": -1,
}

# name -> (target kind, needs NaN-free input)
MODEL_KINDS = {
    "ridge_delta": ("delta", True),
    "lgbm_level": ("level", False),
    "lgbm_delta": ("delta", False),
    "lgbm_linear_delta": ("delta", True),
}


def _make_estimator(name, params):
    if name == "ridge_delta":
        return make_pipeline(
            SimpleImputer(strategy="median"), StandardScaler(), Ridge(**params)
        )
    lgbm_params = {**LGBM_DEFAULTS, **params}
    if name == "lgbm_linear_delta":
        lgbm_params.setdefault("linear_tree", True)
        lgbm_params.setdefault("linear_lambda", 1.0)
    return lightgbm.LGBMRegressor(**lgbm_params)


class HorizonModel:
    def __init__(self, name, horizon, feature_names, params=None):
        self.name = name
        self.horizon = horizon
        self.feature_names = list(feature_names)
        self.params = params or {}
        self.kind, self.needs_filled = MODEL_KINDS[name]
        self.estimator = _make_estimator(name, self.params)
        self.fill_values = None

    def _inputs(self, frame):
        inputs = frame[self.feature_names]
        if self.needs_filled and not self.name.startswith("ridge"):
            inputs = inputs.fillna(self.fill_values)
        return inputs

    def fit(self, frame):
        rows = frame.dropna(subset=[f"target_h{self.horizon}", "x44_max"])
        target = rows[f"target_h{self.horizon}"]
        if self.kind == "delta":
            target = target - rows["x44_max"]
        self.fill_values = rows[self.feature_names].median()
        self.estimator.fit(self._inputs(rows), target)
        return self

    def predict(self, frame):
        """Predicted daily maximum level (m MSL) at t+h for each row's day t."""
        prediction = self.estimator.predict(self._inputs(frame))
        if self.kind == "delta":
            prediction = prediction + frame["x44_max"].to_numpy()
        return pandas.Series(prediction, index=frame.index, name=f"pred_h{self.horizon}")

    def feature_importance(self):
        estimator = self.estimator
        if isinstance(estimator, lightgbm.LGBMRegressor):
            gain = estimator.booster_.feature_importance(importance_type="gain")
            return pandas.Series(gain, index=self.feature_names).sort_values(ascending=False)
        coefficients = estimator[-1].coef_
        return pandas.Series(coefficients, index=self.feature_names).sort_values(
            key=abs, ascending=False
        )


def save(models, tag, extra=None):
    """Store one fitted model per horizon plus a small manifest.

    `extra` is kept in the manifest for settings the caller needs at
    prediction time, such as which rain forecast feeds the model."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"tag": tag, "horizons": {}, "extra": extra or {}}
    for model in models:
        path = MODELS_DIR / f"{tag}_h{model.horizon}.joblib"
        joblib.dump(model, path)
        manifest["horizons"][model.horizon] = {
            "model": model.name,
            "params": model.params,
            "path": path.name,
            "features": model.feature_names,
        }
    manifest_path = MODELS_DIR / f"{tag}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest_path


def load_manifest(tag):
    return json.loads((MODELS_DIR / f"{tag}.json").read_text())


def load(tag):
    manifest = load_manifest(tag)
    return [
        joblib.load(MODELS_DIR / entry["path"])
        for _, entry in sorted(manifest["horizons"].items(), key=lambda item: int(item[0]))
    ]
