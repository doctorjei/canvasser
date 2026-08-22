"""canvasser -- browser automation for UF Canvas (ufl.instructure.com).

Navigates Canvas as a person would rather than through the REST API, because UF
discontinued API token support. See canon/workbook/devnotes.md for the reasoning.

Copyright (C) 2026 Jei Blanchard.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed WITHOUT ANY WARRANTY -- without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
General Public License (the LICENSE file, or <https://www.gnu.org/licenses/>)
for details.
"""

#: Single source of truth for the version -- `pyproject.toml` reads it from
#: here, so a release bumps one line rather than two that can disagree.
__version__ = "0.1.3"

__all__ = ["auth", "browser", "config", "duo", "__version__"]
