from __future__ import annotations

POINTS = 11
MIN_POINTS = 3

LEGACY_DEFAULT = (0.0, 0.10, 0.50, 0.90, 1.0)


def _resample(points, count=POINTS):
    src = [float(v) for v in points]
    span = len(src) - 1
    out = []
    for i in range(count):
        pos = i / (count - 1) * span
        low = min(span - 1, int(pos))
        w = pos - low
        out.append(src[low] * (1 - w) + src[low + 1] * w)
    out[0], out[-1] = 0.0, 1.0
    return out


DEFAULT_CURVE = _resample(LEGACY_DEFAULT)

PRESETS = {
    "smooth": DEFAULT_CURVE,
    "linear": _resample((0.0, 1.0)),
    "early": _resample((0.0, 0.45, 0.75, 0.92, 1.0)),
    "late": _resample((0.0, 0.08, 0.25, 0.55, 1.0)),
    "snap": _resample((0.0, 0.02, 0.05, 0.12, 0.4, 1.0)),
}
PRESET_LABELS = {"smooth": "Smooth", "linear": "Linear", "early": "Fast start",
                 "late": "Slow start", "snap": "Slam"}


def normalise(values) -> list[float]:
    try:
        curve = [float(v) for v in values]
    except (TypeError, ValueError):
        curve = []
    if len(curve) < MIN_POINTS or any(v != v for v in curve):
        curve = list(DEFAULT_CURVE)
    curve = [max(0.0, min(1.0, v)) for v in curve]
    if len(curve) != POINTS:
        curve = _resample(curve)
    curve[0], curve[-1] = 0.0, 1.0
    return curve


def value(curve, progress: float) -> float:
    curve = curve if len(curve) == POINTS else normalise(curve)
    progress = max(0.0, min(1.0, float(progress)))
    pos = progress * (len(curve) - 1)
    low = min(len(curve) - 2, int(pos))
    w = pos - low
    return curve[low] * (1 - w) + curve[low + 1] * w
