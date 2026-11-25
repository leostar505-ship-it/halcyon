__version__ = "1.0.0"


def short_version(version=__version__):
    parts = version.split(".")
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)


SHORT_VERSION = short_version()
NAME = "Halcyon"
EXPANSION = "H.A.L.C.Y.O.N: Hears, Answers, Launches, Controls, Yields, Operates, Notifies"
