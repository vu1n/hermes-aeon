"""5-axis tone codec — warmth/directness/structure/evidence/playfulness on [0,1]."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

AXES = ("warmth", "directness", "structure", "evidence", "playfulness")


@dataclass
class ToneState:
    warmth: float = 0.6
    directness: float = 0.6
    structure: float = 0.6
    evidence: float = 0.5
    playfulness: float = 0.3

    def clamp(self) -> "ToneState":
        return ToneState(**{k: max(0.0, min(1.0, getattr(self, k))) for k in AXES})

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ToneState":
        return cls(**{k: float(d.get(k, getattr(DEFAULT_TONE, k))) for k in AXES})


DEFAULT_TONE = ToneState()

PRESETS = {
    "coach":     ToneState(0.8, 0.6, 0.7, 0.5, 0.3),
    "analyst":   ToneState(0.4, 0.8, 0.8, 0.8, 0.2),
    "creative":  ToneState(0.6, 0.5, 0.5, 0.5, 0.5),
    "visionary": ToneState(0.4, 0.8, 0.7, 0.6, 0.3),
    "mentor":    ToneState(0.7, 0.5, 0.5, 0.5, 0.5),
}


def blend(current: ToneState, target: ToneState, weight: float) -> ToneState:
    w = max(0.0, min(1.0, weight))
    return ToneState(**{
        k: getattr(current, k) * (1 - w) + getattr(target, k) * w
        for k in AXES
    }).clamp()


def apply_preset(current: ToneState, preset_name: str, weight: float = 0.7) -> ToneState:
    if preset_name not in PRESETS:
        return current
    return blend(current, PRESETS[preset_name], weight)


def apply_axes(current: ToneState, axes: dict, weight: float = 0.7) -> ToneState:
    target_kwargs = {k: float(axes[k]) if k in axes else getattr(current, k) for k in AXES}
    target = ToneState(**target_kwargs)
    return blend(current, target, weight)


def _label(value: float, low: str, mid: str, high: str) -> str:
    if value < 0.34:
        return low
    if value < 0.67:
        return mid
    return high


_AXIS_LABELS = {
    "warmth":      ("clinical", "balanced", "warm/encouraging"),
    "directness":  ("subtle", "balanced", "explicit/direct"),
    "structure":   ("exploratory", "balanced", "linear/structured"),
    "evidence":    ("intuitive", "balanced", "evidence-led"),
    "playfulness": ("serious", "balanced", "playful/light"),
}


def tone_to_block(state: ToneState) -> str:
    """Render a tone block for system context. Never names the preset."""
    lines = ["Tone state (blend naturally; do not announce):"]
    for axis in AXES:
        value = getattr(state, axis)
        low, mid, high = _AXIS_LABELS[axis]
        lines.append(f"  {axis}: {value:.2f} ({_label(value, low, mid, high)})")
    return "\n".join(lines)


def derive_from_message(msg: str, current: ToneState) -> Optional[ToneState]:
    """Cheap heuristic tone shift from message intent. Returns None if no shift."""
    if not msg:
        return None
    lower = msg.lower()

    if any(k in lower for k in ("coach me", "guide me", "encourage")):
        return apply_preset(current, "coach", 0.7)
    if any(k in lower for k in ("analyze", "data shows", "evidence", "rigorous")):
        return apply_preset(current, "analyst", 0.7)
    if any(k in lower for k in ("brainstorm", "ideas", "play with")):
        return apply_preset(current, "creative", 0.7)
    if any(k in lower for k in ("vision", "long-term", "strategy")):
        return apply_preset(current, "visionary", 0.7)
    if any(k in lower for k in ("teach me", "explain", "help me understand")):
        return apply_preset(current, "mentor", 0.7)

    # Domain hints (gentle nudges)
    if any(k in lower for k in ("stressed", "overwhelmed", "anxious", "tired")):
        return apply_axes(current, {"warmth": min(1.0, current.warmth + 0.2),
                                    "directness": max(0.0, current.directness - 0.1)}, 0.5)
    return None
