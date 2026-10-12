# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2020 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""A completion category that offers filesystem paths."""

import os
from typing import Iterable, Optional, Tuple

from PyQt5.QtCore import QAbstractListModel, QModelIndex, Qt

from qutebrowser.config import config


_Row = Tuple[Optional[str], Optional[str], Optional[str]]


class FilePathCategory(QAbstractListModel):

    """A completion category which suggests filesystem paths.

    When the pattern is empty, the paths configured in
    `completion.favorite_paths` are suggested. When the pattern points at an
    absolute directory (i.e. it is absolute and ends with the path separator),
    the entries directly contained in that directory are suggested.

    Attributes:
        name: The name of the category as shown in the completion.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.name = "Filesystem"
        self._pattern = ''
        self._paths: Iterable[_Row] = []

        # This category only suggests the paths themselves, so only the first
        # column is relevant for filtering/highlighting.
        self.columns_to_filter = [0]
        self.delete_func = None

    def set_pattern(self, val: str) -> None:
        """Set the pattern used to compute the suggested paths.

        Args:
            val: The (partial) path/URL the user has typed so far.
        """
        self.beginResetModel()
        self._pattern = val
        self._paths = list(self._completions(val))
        self.endResetModel()

    def _completions(self, val: str) -> Iterable[_Row]:
        """Get the path suggestions for the given pattern."""
        if not val:
            for path in config.val.completion.favorite_paths:
                yield (path, None, None)
            return

        if not (os.path.isabs(val) and val.endswith(os.sep)):
            return

        try:
            entries = os.listdir(val)
        except OSError:
            return

        for entry in sorted(entries):
            yield (val + entry, None, None)

    def data(self, index: QModelIndex,
             role: int = Qt.DisplayRole) -> Optional[str]:
        """Override QAbstractListModel::data."""
        if role != Qt.DisplayRole or not index.isValid():
            return None
        row, column = index.row(), index.column()
        if row < 0 or row >= len(self._paths):
            return None
        try:
            return self._paths[row][column]  # type: ignore[index]
        except IndexError:
            return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """Override QAbstractListModel::rowCount."""
        if parent.isValid():
            return 0
        return len(self._paths)
