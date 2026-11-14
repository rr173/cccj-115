"""Library configuration: threshold, depth, cache limits, enabled masks.

``threshold`` is an exact non-zero decimal in ``(0, 1]`` stored as text and
compared as ``Fraction(Decimal(text))``.  No float conversion happens, so
``threshold=0.1`` means exactly one tenth and ``points/W >= threshold`` is
an exact rational comparison.

``depth`` and the two cache limits are initialisation parameters: they may be
changed when a library is re-opened (a cache limit is pure performance; the
depth select the index shape).  ``threshold`` and the enabled mask rules are
fixed at creation, because changing them would re-interpret existing
templates.  A library records which is which in ``meta.config``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from .masks import MASK_KINDS, validate_enabled


class ConfigError(ValueError):
    pass


def parse_threshold(text: str) -> Fraction:
    if not isinstance(text, str):
        raise ConfigError("threshold must be a decimal string")
    t = text.strip()
    if not t:
        raise ConfigError("threshold is empty")
    try:
        d = Decimal(t)
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"threshold {text!r} is not a decimal number") from exc
    if not d.is_finite():
        raise ConfigError("threshold must be finite")
    if d <= 0 or d > 1:
        raise ConfigError("threshold must satisfy 0 < threshold <= 1")
    return Fraction(d)


def fraction_to_text(value: Fraction) -> str:
    """Exact decimal text for a fraction with denominator 2^a*5^b."""
    denom = value.denominator
    twos = fives = 0
    while denom % 2 == 0:
        denom //= 2
        twos += 1
    while denom % 5 == 0:
        denom //= 5
        fives += 1
    if denom != 1:
        return f"{value.numerator}/{value.denominator}"
    scale = max(twos, fives)
    scaled = abs(value.numerator) * (10**scale) // value.denominator
    digits = str(scaled).rjust(scale + 1, "0")
    whole = digits[: len(digits) - scale]
    frac = digits[len(digits) - scale:].rstrip("0")
    return ("-" if value.numerator < 0 else "") + whole + ("." + frac if frac else "")


@dataclass(frozen=True)
class Config:
    threshold_text: str
    depth: int
    template_cache: int
    node_cache: int
    enabled_masks: tuple[str, ...]

    @property
    def threshold(self) -> Fraction:
        return parse_threshold(self.threshold_text)

    def to_json(self) -> str:
        return json.dumps(
            {
                "threshold": self.threshold_text,
                "depth": self.depth,
                "template_cache": self.template_cache,
                "node_cache": self.node_cache,
                "enabled_masks": list(self.enabled_masks),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> "Config":
        d = json.loads(text)
        return cls(
            threshold_text=str(d["threshold"]),
            depth=int(d["depth"]),
            template_cache=int(d["template_cache"]),
            node_cache=int(d["node_cache"]),
            enabled_masks=tuple(d["enabled_masks"]),
        )


def build_config(
    *,
    threshold: str = "0.5",
    depth: int = 2,
    template_cache: int = 64,
    node_cache: int = 256,
    enabled_masks=None,
) -> Config:
    parse_threshold(threshold)
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
        raise ConfigError("depth must be a non-negative integer")
    for name, value in (("template_cache", template_cache), ("node_cache", node_cache)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(f"{name} must be a non-negative integer")
    enabled = validate_enabled(enabled_masks)
    return Config(
        threshold_text=fraction_to_text(parse_threshold(threshold)),
        depth=depth,
        template_cache=template_cache,
        node_cache=node_cache,
        # MASK_KINDS order, not frozenset iteration order: the stored config
        # text must be byte-identical across processes and hash seeds.
        enabled_masks=tuple(k for k in MASK_KINDS if k in enabled),
    )
