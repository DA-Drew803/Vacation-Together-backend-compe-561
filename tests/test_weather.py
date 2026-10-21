import json
from datetime import date

from app.services.weather import fetch_weather_snapshot


def test_weather_uses_forecast_without_external_calls() -> None:
    def transport(request):
        assert "api.open-meteo.com" in request.full_url
        return json.dumps(
            {
                "daily": {
                    "time": ["2027-01-01"],
                    "temperature_2m_max": [12],
                    "temperature_2m_min": [4],
                    "precipitation_probability_max": [20],
                    "weather_code": [1],
                }
            }
        ).encode()

    snapshot = fetch_weather_snapshot(
        35.68, 139.65, date(2027, 1, 1), date(2027, 1, 1), transport=transport
    )

    assert snapshot["data_type"] == "forecast"
    assert snapshot["source"] == "open-meteo"
    assert snapshot["days"][0]["temperature_high"] == 12


def test_weather_is_unavailable_when_forecast_fails() -> None:
    calls = []

    def transport(request):
        calls.append(request.full_url)
        raise OSError("forecast unavailable")

    snapshot = fetch_weather_snapshot(
        35.68, 139.65, date(2027, 1, 1), date(2027, 1, 1), transport=transport
    )

    assert len(calls) == 1
    assert snapshot == {"data_type": "unavailable", "source": "open-meteo", "days": []}


def test_weather_failure_is_not_fabricated() -> None:
    def transport(request):
        raise OSError("provider unavailable")

    snapshot = fetch_weather_snapshot(
        35.68, 139.65, date(2027, 1, 1), date(2027, 1, 1), transport=transport
    )
    assert snapshot == {"data_type": "unavailable", "source": "open-meteo", "days": []}
