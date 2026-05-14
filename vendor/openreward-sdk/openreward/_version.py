from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("openreward")
except PackageNotFoundError:
    __version__ = "unknown"

USER_AGENT = f"openreward-sdk/{__version__}"
