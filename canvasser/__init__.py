"""canvasser -- browser automation for UF Canvas (ufl.instructure.com).

Navigates Canvas as a person would rather than through the REST API, because UF
discontinued API token support. See canon/workbook/devnotes.md for the reasoning.
"""

__all__ = ["auth", "browser", "config", "duo"]
