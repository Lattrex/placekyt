# gr_kyttar — block placement & bitstream generation for the Kyttar cell array

`gr_kyttar` turns a set of DSP block definitions into a programmed Kyttar chip.
It is the block-build library that placeKYT uses to place, route, and generate a
bitstream for the Kyttar asynchronous mcell array, and it is consumed by the
`gr-kyttar` GNU Radio out-of-tree module.

## Layout

- **`gr_kyttar.placement`** — block definitions, the placer, the router, the cell
  map, and the program resolver.
- **`gr_kyttar.bitstream`** — bitstream generation from a placed and routed cell
  map.

## Installation

`gr_kyttar` is pure Python; it depends on the compiled `simkyt` simulator
extension for running a built bitstream.

```bash
# 1. the block-build library (this package)
pip install -e .

# 2. the simkyt simulator extension is shipped prebuilt as
#    runtime/python/simkyt/simkyt.cpython-*.so and is importable as `simkyt`.
```

See the top-level `INSTALL.md` for the full placeKYT + GNU Radio setup.

## Quick use

```python
from gr_kyttar.placement import ArrayConfig, Placer, Router
from gr_kyttar.bitstream import BitstreamGenerator

config = ArrayConfig.from_yaml("configs/dev_12x12.yaml")
# place + route block definitions, then:
gen = BitstreamGenerator("configs/dev_12x12.yaml")
# gen.load_cell_map(cell_map); bitstream = gen.generate()
```

For the full, runnable demos (a coherent BPSK receiver, a gain block, a DSP
comparison) see `gr-kyttar/examples/`.
