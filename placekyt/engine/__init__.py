"""placeKYT engine layer — backend services over the data model.

Depends on ``model/`` and (for simulation/build) the existing ``simkyt`` /
``gr_kyttar`` packages (the architecture notes §0, §6). The ``engine.io`` subpackage
holds file serialization (``.kyt`` / ``.kbl`` / ``.kdb`` / chip-type YAML),
which uses ``ruamel.yaml`` but no Qt.
"""
