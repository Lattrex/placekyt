"""Block Library panel — categorized tree of available blocks (§3.4).

Populated from the :class:`BlockCatalog`. Items are draggable onto the canvas
with MIME ``application/x-placekyt-block`` carrying ``{block_type, library}``
(§3.4). A search box filters by name/description/category/tags (§7.2).
"""

from __future__ import annotations

import json

from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from engine.catalog import BlockCatalog, verify_badge as _verify_badge, \
    verify_note as _verify_note
from ui.canvas.chip_canvas import BLOCK_MIME

# Role storing the (block_type, library) tuple on leaf items.
_BLOCK_ROLE = Qt.UserRole + 1


class _BlockTree(QTreeWidget):
    """Tree whose leaf (block) items start a block-MIME drag."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setDragEnabled(True)

    def startDrag(self, supported_actions) -> None:  # noqa: N802
        item = self.currentItem()
        data = item.data(0, _BLOCK_ROLE) if item else None
        if not data:
            return
        block_type, library = data
        mime = QMimeData()
        payload = json.dumps({"block_type": block_type, "library": library})
        mime.setData(BLOCK_MIME, payload.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)


class LibraryPanel(QWidget):
    """Search box + categorized block tree."""

    def __init__(self, catalog: BlockCatalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search blocks…")
        self.search.textChanged.connect(self._repopulate)
        self.tree = _BlockTree()
        layout.addWidget(self.search)
        layout.addWidget(self.tree)
        self.setMinimumWidth(180)

        self._repopulate()

    def _repopulate(self) -> None:
        query = self.search.text().strip()
        specs = self.catalog.search(query) if query else self.catalog.all()
        self.tree.clear()
        by_cat: dict[str, list] = {}
        for s in specs:
            by_cat.setdefault(s.category, []).append(s)
        for category in sorted(by_cat):
            cat_item = QTreeWidgetItem([_pretty(category)])
            for spec in by_cat[category]:
                badge = _verify_badge(getattr(spec, "verification", "verified"))
                label = f"{spec.type_name} {badge}".rstrip()
                leaf = QTreeWidgetItem([label])
                leaf.setData(0, _BLOCK_ROLE, (spec.type_name, spec.library))
                tip = spec.description
                note = _verify_note(getattr(spec, "verification", "verified"))
                if note:
                    tip = f"{tip}\n\n{note}" if tip else note
                leaf.setToolTip(0, tip)
                cat_item.addChild(leaf)
            self.tree.addTopLevelItem(cat_item)
            cat_item.setExpanded(True)

    def block_count(self) -> int:
        """Number of leaf (block) items currently shown (for tests)."""
        n = 0
        for i in range(self.tree.topLevelItemCount()):
            n += self.tree.topLevelItem(i).childCount()
        return n


def _pretty(category: str) -> str:
    return category.replace("_", " ").title()
