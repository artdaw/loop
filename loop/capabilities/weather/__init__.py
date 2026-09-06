"""The weather.local capability pack (capabilities/weather.md).

Multiple sources with a *justified* preference for local ones. "Local" means the
meteorological authority covering the requested geography — not whichever site
happens to use the local language, and not a preference that survives the source
going stale.
"""

from loop.capabilities.weather.sources import (
    Authority,
    ProductType,
    SourceCatalogue,
    SourceDescriptor,
    select_sources,
)

__all__ = ["Authority", "ProductType", "SourceDescriptor", "SourceCatalogue",
           "select_sources"]
