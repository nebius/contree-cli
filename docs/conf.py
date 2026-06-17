import importlib.metadata
import os
import sys

# Add project root so the extension can import contree_cli
sys.path.insert(0, os.path.abspath(".."))
# Add local extensions
sys.path.insert(0, os.path.abspath("ext"))

project = "contree-cli"
author = "ConTree"
release = importlib.metadata.version("contree-cli")
version = ".".join(release.split(".")[:2])

extensions = [
    "myst_parser",
    "sphinx_design",
    "contree_doc_ext",
    "terminal_ext",
    "sphinx_mintlify_output",
]

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
source_suffix = {
    ".md": "markdown",
}

myst_enable_extensions = [
    "colon_fence",
    "fieldlist",
]

html_theme = "furo"
html_title = "contree-cli"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_logo = "_static/logo.svg"

html_theme_options = {
    "navigation_with_keys": True,
    "sidebar_hide_name": True,
}


# -- Mintlify output --------------------------------------------------------

mintlify_docs_json = {
    "name": "ConTree CLI",
    "theme": "mint",
    "logo": {"light": "_static/logo.svg", "dark": "_static/logo.svg"},
}
