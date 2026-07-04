#!/usr/bin/env python3
"""Headless GRC flowgraph validator — reports Missing-Block + port type/size errors.

Run with the SYSTEM python that has GNU Radio (NOT the placeKYT .venv), e.g.
    GRC_BLOCKS_PATH=/usr/share/gnuradio/grc/blocks:$HOME/.local/share/gnuradio/grc/blocks \
        /usr/bin/python3 examples/validate_grc.py examples/bpsk_modem/bpsk_modem.grc

Exit code 0 = zero errors. Prints each connection/block error GRC would show in the
GUI, so a demo .grc can be checked for "opens clean" without launching gnuradio-companion.
The kyttar blocks must be installed (gr-kyttar/install.sh or copy grc/*.block.yml into
the GRC blocks dir) for ids to resolve.
"""
import sys
import os
import warnings

warnings.filterwarnings("ignore")

# GRC 3.10.x ships a Config whose __init__ doesn't take install_prefix; Platform
# forwards its kwargs to Config. Adapt so we can construct a Platform headlessly.
import gnuradio.grc.core.Config as _C
import gnuradio.grc.core.platform as _P


class _Cfg(_C.Config):
    def __init__(self, *a, **k):
        k.pop("install_prefix", None)
        super().__init__(
            version=k.get("version", "3.10.12"),
            version_parts=k.get("version_parts", ("3", "10", "12")),
            name="GRC",
            prefs=k.get("prefs"),
        )


_P.Platform.Config = _Cfg


def validate(path: str) -> int:
    if "GRC_BLOCKS_PATH" not in os.environ:
        os.environ["GRC_BLOCKS_PATH"] = (
            "/usr/share/gnuradio/grc/blocks:"
            + os.path.expanduser("~/.local/share/gnuradio/grc/blocks")
        )
    import yaml

    p = _P.Platform(version="3.10.12", version_parts=("3", "10", "12"), prefs=None)
    p.build_library()
    fg = p.make_flow_graph()
    fg.import_data(yaml.safe_load(open(path)))
    fg.rewrite()
    fg.validate()
    errs = fg.get_error_messages()
    print(f"=== {path}: {len(errs)} error(s) ===")
    for e in errs:
        print("  ", " ".join(str(e).split()))
    return 1 if errs else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: validate_grc.py <flowgraph.grc> [more.grc ...]")
        sys.exit(2)
    rc = 0
    for path in sys.argv[1:]:
        rc |= validate(path)
    sys.exit(rc)
