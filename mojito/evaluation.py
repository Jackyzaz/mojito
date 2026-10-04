"""Scores shared by the model notebooks."""

import numpy

from mojito import features

HIGH_WATER_M = 4.0
FLOOD_M = features.ALERT_LEVELS["ท่วมพื้นที่ลุ่มต่ำ"]


def scores(prediction, frame, horizon):
    """MAE overall and on high-water days, plus 7.40 m alert hits."""
    target = frame[f"target_h{horizon}"]
    valid = target.notna() & prediction.notna()
    target, prediction = target[valid], prediction[valid]
    error = (prediction - target).abs()
    high = target >= HIGH_WATER_M
    flooded, warned = target >= FLOOD_M, prediction >= FLOOD_M
    return {
        "MAE ทุกวัน": error.mean(),
        "MAE วันน้ำหลาก": error[high].mean() if high.any() else numpy.nan,
        "เตือนถูก": int((flooded & warned).sum()),
        "วันท่วมจริง": int(flooded.sum()),
        "เตือนผิด": int((~flooded & warned).sum()),
    }


def persistence(frame, horizon):
    """Forecast that the level stays at today's maximum."""
    return frame["x44_max"].rename(f"pred_h{horizon}")


def stage_split_rmse(prediction, frame, horizon, split_stage=HIGH_WATER_M):
    """RMSE for forecasts below / above `split_stage`, used as forecast sigma."""
    error = (prediction - frame[f"target_h{horizon}"]).dropna()
    high = prediction.loc[error.index] >= split_stage
    return {
        "low": round(float(numpy.sqrt((error[~high] ** 2).mean())), 3),
        "high": round(float(numpy.sqrt((error[high] ** 2).mean())), 3) if high.any() else None,
        "high_days": int(high.sum()),
    }
