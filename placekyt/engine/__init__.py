# SPDX-License-Identifier: GPL-3.0-or-later
"""placeKYT engine layer — backend services over the data model.

Depends on ``model/`` and (for simulation/build) the existing ``simkyt`` /
``gr_kyttar`` packages. The ``engine.io`` subpackage
holds file serialization (``.kyt`` / ``.kbl`` / ``.kdb`` / chip-type YAML),
which uses ``ruamel.yaml`` but no Qt.
"""
