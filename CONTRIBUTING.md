<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Contributing to placeKYT

Thanks for your interest in placeKYT. Contributions — bug reports, fixes, new DSP
blocks, docs, and packaging work — are welcome.

## Ground rules

- placeKYT and the `gr_kyttar` block library are **GPL-3.0-or-later**. By
  contributing, you agree your contribution is licensed under the same terms.
- Keep new source files' SPDX headers consistent: `SPDX-License-Identifier:
  GPL-3.0-or-later`.
- Be excellent to each other.

## Development setup

Follow **[INSTALL.md](INSTALL.md)** to get a source install in a virtual
environment. In short:

```bash
python3 -m venv .venv
.venv/bin/pip install -r placekyt/requirements-dev.txt
.venv/bin/pip install -e runtime/python
```

## Running the tests

placeKYT has a full test suite. Run it headless:

```bash
cd placekyt
QT_QPA_PLATFORM=offscreen ../.venv/bin/python -m pytest tests/ -q
```

Please add tests for new behaviour, and make sure the suite is green before
opening a pull request. A quick end-to-end sanity check is the headless demo
build:

```bash
.venv/bin/python placekyt/cli.py --test placekyt/tests/data/demo/qam16_demo.kyt \
    --chip-type placekyt/resources/chips/kyttar_10x12.yaml
```

## Writing a new DSP block

New blocks live in `runtime/python/gr_kyttar/placement/`. Read
**[PROGRAMMING_GUIDE.md](PROGRAMMING_GUIDE.md)** and an existing block (the gain
block is the simplest) to learn the declarative block API. A good block ships
with a test that compares the simulated Q15 output against a floating-point
reference (and, where applicable, against a GNU Radio block).

## Pull requests

- Keep changes focused; one logical change per PR.
- Describe *what* and *why*, and note any new dependencies.
- Match the style of the surrounding code.

## About simKYT (the simulator)

The `simkyt` simulator is distributed as a **prebuilt binary** extension and is a
closed Lattrex component — its source is not part of this repository. It is
**free to use, and always will be** — you just don't get the source at this time.
You can build placeKYT and `gr_kyttar` from source and contribute to them freely;
changes that would require modifying the simulator itself can't be done in this
repo, but please **open an issue** describing the need and we'll work with you.

If you hit a platform/Python-version mismatch with the bundled `simkyt` binary,
that's expected for now (see INSTALL.md §4) — open an issue and we can provide a
build for your target.

## Brand note

The **Lattrex** name, the **Kyttar** name, and associated logos are trademarks of
Lattrex. They appear in this repository for identification and branding of a
Lattrex product. Their inclusion under GPL applies to the *software*; it does not
grant rights to use the Lattrex/Kyttar marks or logos for other purposes. Please
don't use them in a way that implies endorsement of a fork or derivative.
