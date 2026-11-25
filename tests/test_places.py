from io import BytesIO

from app.services import places


def test_serialize_google_place_keeps_verified_metadata_and_photo_name() -> None:
    result = places._serialize_place(
        {
            "id": "ChIJdestination",
            "displayName": {"text": "Portland Head Light"},
            "formattedAddress": "12 Captain Strout Circle, Cape Elizabeth, ME 04107",
            "location": {"latitude": 43.6231, "longitude": -70.2078},
            "photos": [{"name": "places/ChIJdestination/photos/abc123"}],
            "rating": 4.8,
            "userRatingCount": 2400,
        }
    )

    assert result == {
        "place_id": "ChIJdestination",
        "location": "12 Captain Strout Circle, Cape Elizabeth, ME 04107",
        "latitude": 43.6231,
        "longitude": -70.2078,
        "photo_name": "places/ChIJdestination/photos/abc123",
        "rating": 4.8,
        "rating_count": 2400,
    }


def test_resolve_place_uses_google_search_and_caches(monkeypatch) -> None:
    places.resolve_place.cache_clear()


def test_resolve_place_tries_fallback_queries_when_the_full_query_is_unresolved(
    monkeypatch,
) -> None:
    places.resolve_place.cache_clear()
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    calls: list[str] = []

    def fake_search(query: str) -> dict | None:
        calls.append(query)
        if query == "Portland Head Light":
            return {
                "id": "ChIJheadlight",
                "displayName": {"text": "Portland Head Light"},
                "location": {"latitude": 43.6231, "longitude": -70.2078},
            }
        return None

    monkeypatch.setattr(places, "_search_google_places", fake_search)

    result = places.resolve_place("Portland Head Light", "Portland, Maine")

    assert result is not None
    assert calls == [
        "Portland Head Light, Portland, Maine",
        "Portland Head Light in Portland, Maine",
        "Portland Head Light",
    ]
    places.resolve_place.cache_clear()
    calls: list[str] = []

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")

    def fake_search(query: str) -> dict:
        calls.append(query)
        return {
            "id": "ChIJrailroad",
            "displayName": {"text": "Maine Narrow Gauge Railroad"},
            "formattedAddress": "49 Thames Street, Portland, ME 04101",
            "location": {"latitude": 43.6589, "longitude": -70.2444},
        }

    monkeypatch.setattr(places, "_search_google_places", fake_search)
    first = places.resolve_place("Maine Narrow Gauge Railroad", "Portland, Maine")
    second = places.resolve_place("Maine Narrow Gauge Railroad", "Portland, Maine")

    assert first == second
    assert calls == ["Maine Narrow Gauge Railroad, Portland, Maine"]
    places.resolve_place.cache_clear()


def test_fetch_photo_rejects_arbitrary_photo_names(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    monkeypatch.setattr(
        places, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError())
    )

    assert places.fetch_photo("https://example.com/image.jpg") is None


def test_fetch_photo_returns_image_bytes(monkeypatch) -> None:
    class FakeHeaders:
        def get_content_type(self) -> str:
            return "image/jpeg"

    class FakeResponse(BytesIO):
        headers = FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    monkeypatch.setattr(places, "urlopen", lambda *_args, **_kwargs: FakeResponse(b"image"))

    assert places.fetch_photo("places/ChIJdestination/photos/abc123") == (b"image", "image/jpeg")
