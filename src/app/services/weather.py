from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class WeatherProviderError(RuntimeError):
    pass


Transport = Callable[[Request], bytes]


def default_transport(request: Request) -> bytes:
    with urlopen(request, timeout=10) as response:
        return response.read()


def request_daily(
    base_url: str,
    latitude: float,
    longitude: float,
    start_date: date,
    end_date: date,
    transport: Transport,
) -> dict:
    query = urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "timezone": "UTC",
        }
    )
    request = Request(f"{base_url}?{query}", headers={"Accept": "application/json"})
    payload = json.loads(transport(request))
    if not isinstance(payload, dict) or not isinstance(payload.get("daily"), dict):
        raise WeatherProviderError("Open-Meteo returned no daily weather data")
    return payload


def snapshot(payload: dict, data_type: str) -> dict:
    daily = payload["daily"]
    times = daily.get("time", [])
    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    precipitation = daily.get("precipitation_probability_max", [])
    codes = daily.get("weather_code", [])
    rows = []
    for index, day in enumerate(times):
        rows.append(
            {
                "date": day,
                "temperature_high": highs[index] if index < len(highs) else None,
                "temperature_low": lows[index] if index < len(lows) else None,
                "precipitation_probability": (
                    precipitation[index] if index < len(precipitation) else None
                ),
                "weather_code": codes[index] if index < len(codes) else None,
            }
        )
    return {"data_type": data_type, "source": "open-meteo", "days": rows}


def fetch_weather_snapshot(
    latitude: float,
    longitude: float,
    start_date: date,
    end_date: date,
    *,
    transport: Transport = default_transport,
) -> dict:
    """Fetch forecast weather, falling back to Open-Meteo historical data.

    The transport is injectable so tests never make live external calls. If both
    endpoints fail, an unavailable marker contains no fabricated values.
    """
    forecast_url = "https://api.open-meteo.com/v1/forecast"
    archive_url = "https://archive-api.open-meteo.com/v1/archive"
    try:
        return snapshot(
            request_daily(forecast_url, latitude, longitude, start_date, end_date, transport),
            "forecast",
        )
    except (KeyError, OSError, TypeError, ValueError):
        try:
            return snapshot(
                request_daily(archive_url, latitude, longitude, start_date, end_date, transport),
                "historical_average",
            )
        except (KeyError, OSError, TypeError, ValueError):
            return {"data_type": "unavailable", "source": "open-meteo", "days": []}
