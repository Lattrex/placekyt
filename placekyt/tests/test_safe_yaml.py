"""Tests for the hardened YAML loader (engine/io/safe_yaml.py, §2.1)."""

from __future__ import annotations

import pytest

from engine.io.errors import YamlBombError, YamlDepthError, YamlSizeError
from engine.io.safe_yaml import (
    MAX_FILE_BYTES,
    dump_yaml_str,
    load_yaml_str,
)


def _bomb(levels: int, fan: int) -> str:
    names = "abcdefghij"[:levels]
    lines = [f"{names[0]}: &{names[0]} [" + ",".join(["x"] * fan) + "]"]
    for i in range(1, levels):
        prev = names[i - 1]
        lines.append(f"{names[i]}: &{names[i]} [" + ",".join([f"*{prev}"] * fan) + "]")
    return "\n".join(lines) + "\n"


class TestFloatRoundTrip:
    @pytest.mark.parametrize("val", [0.35, 0.1, 0.05, 0.001, -0.5, 1.0, 3.14159265358979])
    def test_repr_precision_survives(self, val):
        out = dump_yaml_str({"v": val})
        assert load_yaml_str(out)["v"] == val

    def test_035_exact_text(self):
        # The spec explicitly calls out 0.35 — it must serialize as "0.35",
        # not "0.34999..." or "0.3500000001".
        assert "0.35" in dump_yaml_str({"v": 0.35})

    def test_special_floats(self):
        assert "inf" in dump_yaml_str({"v": float("inf")}).lower()
        assert "nan" in dump_yaml_str({"v": float("nan")}).lower()


class TestDoSProtection:
    def test_billion_laughs_rejected(self):
        with pytest.raises(YamlBombError):
            load_yaml_str(_bomb(7, 9))

    def test_wider_bomb_rejected(self):
        with pytest.raises(YamlBombError):
            load_yaml_str(_bomb(6, 12))

    def test_legit_aliases_allowed(self):
        text = "d: &d {face: south, x: 1}\n" + "".join(
            f"k{i}: *d\n" for i in range(20)
        )
        doc = load_yaml_str(text)
        assert dict(doc["k0"]) == {"face": "south", "x": 1}

    def test_deep_nesting_rejected(self):
        with pytest.raises(YamlDepthError):
            load_yaml_str("a: " + "[" * 80 + "1" + "]" * 80)

    def test_moderate_nesting_allowed(self):
        # 10 levels is fine.
        doc = load_yaml_str("a: " + "[" * 10 + "1" + "]" * 10)
        assert doc is not None

    def test_oversize_string_rejected(self):
        big = "x: " + "a" * (MAX_FILE_BYTES + 1)
        with pytest.raises(YamlSizeError):
            load_yaml_str(big)

    def test_large_legit_file_allowed(self):
        # 500 blocks, no aliases — must parse without tripping the bomb guard.
        text = "blocks:\n" + "".join(
            f"  - {{name: b{i}, type: T}}\n" for i in range(500)
        )
        assert len(load_yaml_str(text)["blocks"]) == 500


class TestUnicode:
    def test_unicode_preserved(self):
        out = dump_yaml_str({"name": "μs ✓ café"})
        assert "μs ✓ café" in out
        assert load_yaml_str(out)["name"] == "μs ✓ café"
