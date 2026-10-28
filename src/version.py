"""Single source of the agent version.

pyproject.toml reads this attribute (``dynamic = ["version"]``), so bumping
the release means editing exactly one line here. At runtime the installed
package metadata is preferred (``pip install .``, PyInstaller with bundled
metadata) and this constant is the fallback for a plain source checkout.
"""

from importlib import metadata

__version__ = "0.2.3"


def get_version() -> str:
    """Version string reported in heartbeats and the status page."""
    try:
        return metadata.version("openeos-printer-agent")
    except metadata.PackageNotFoundError:
        return __version__
