"""Comparable, bounded prominence from public audience counts, never ratings averages."""

import math


def audience(*signals):
    """Log-scale counts, with a noise floor of 1% of the audience cap.

    A handful of ratings must not count as an established audience. Scaling
    before taking the logarithm preserves that distinction while bounding hits.
    """
    scores = []
    for value, cap, weight in signals:
        try:
            value = float(value or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            scores.append(
                min(1.0, math.log1p(value / (cap / 100)) / math.log1p(100)) * weight
            )
    return max(scores, default=0.0)
