"""Provider adapters. Each turns one provider's real response into WeatherSamples."""

from loop.capabilities.weather.adapters.base import (
    AdapterError,
    HttpTransport,
    ProviderAdapter,
)
from loop.capabilities.weather.adapters.open_meteo import (
    OPEN_METEO_MODELS,
    OpenMeteoAdapter,
    open_meteo_descriptors,
)

__all__ = ["AdapterError", "HttpTransport", "ProviderAdapter",
           "OpenMeteoAdapter", "OPEN_METEO_MODELS", "open_meteo_descriptors"]
