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


def test_weather_falls_back_to_historical_data() -> None:
    calls = []

    def transport(request):
        calls.append(request.full_url)
        if "api.open-meteo.com/v1/forecast" in request.full_url:
            raise OSError("forecast unavailable")
        return json.dumps(
            {
                "daily": {
                    "time": ["2027-01-01"],
                    "temperature_2m_max": [10],
                    "temperature_2m_min": [3],
                    "precipitation_probability_max": [10],
                    "weather_code": [2],
                }
            }
        ).encode()

    snapshot = fetch_weather_snapshot(
        35.68, 139.65, date(2027, 1, 1), date(2027, 1, 1), transport=transport
    )

    assert len(calls) == 2
    assert snapshot["data_type"] == "historical_average"
    assert snapshot["source"] == "open-meteo"


def test_weather_failure_is_not_fabricated() -> None:
    def transport(request):
        raise OSError("provider unavailable")

    snapshot = fetch_weather_snapshot(
        35.68, 139.65, date(2027, 1, 1), date(2027, 1, 1), transport=transport
    )
    assert snapshot == {"data_type": "unavailable", "source": "open-meteo", "days": []}
