"""Tone codec: blending, presets, derive, render."""
from hermes_aeon.tone.codec import (
    AXES, DEFAULT_TONE, PRESETS, ToneState,
    apply_axes, apply_preset, blend, derive_from_message, tone_to_block,
)


def test_default_tone_in_range():
    for axis in AXES:
        v = getattr(DEFAULT_TONE, axis)
        assert 0.0 <= v <= 1.0


def test_clamp_keeps_values_in_range():
    s = ToneState(warmth=2.0, directness=-1.0, structure=0.5, evidence=0.5, playfulness=0.5).clamp()
    assert s.warmth == 1.0
    assert s.directness == 0.0


def test_blend_midpoint():
    a = ToneState(0.0, 0.0, 0.0, 0.0, 0.0)
    b = ToneState(1.0, 1.0, 1.0, 1.0, 1.0)
    c = blend(a, b, 0.5)
    for axis in AXES:
        assert abs(getattr(c, axis) - 0.5) < 1e-9


def test_apply_preset_shifts_toward_preset():
    base = ToneState(0.0, 0.0, 0.0, 0.0, 0.0)
    coached = apply_preset(base, "coach", weight=1.0)
    assert coached.warmth == PRESETS["coach"].warmth


def test_apply_axes_partial():
    base = ToneState(0.0, 0.0, 0.0, 0.0, 0.0)
    out = apply_axes(base, {"warmth": 1.0}, weight=0.5)
    assert abs(out.warmth - 0.5) < 1e-9
    assert out.directness == 0.0


def test_derive_recognizes_coach_intent():
    s = derive_from_message("coach me through this", DEFAULT_TONE)
    assert s is not None
    assert s.warmth >= DEFAULT_TONE.warmth


def test_derive_returns_none_for_neutral():
    assert derive_from_message("hello", DEFAULT_TONE) is None


def test_tone_to_block_omits_preset_names():
    block = tone_to_block(PRESETS["coach"])
    assert "coach" not in block.lower()
    assert "preset" not in block.lower()
    for axis in AXES:
        assert axis in block


def test_tone_state_roundtrip():
    s = PRESETS["analyst"]
    s2 = ToneState.from_dict(s.to_dict())
    for axis in AXES:
        assert abs(getattr(s, axis) - getattr(s2, axis)) < 1e-9
