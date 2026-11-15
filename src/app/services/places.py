from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

GOOGLE_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_PLACES_BASE_URL = "https://places.googleapis.com/v1/"
GOOGLE_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.photos,places.rating,places.userRatingCount,places.editorialSummary,"
    "places.websiteUri,places.googleMapsUri,places.reviews"
)
PHOTO_NAME_PATTERN = re.compile(r"^places/[^/]+/photos/[^/]+$")


def _api_key() -> str | None:
    value = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    return value or None


def _timeout() -> float:
    try:
        return max(1.0, min(float(os.getenv("PLACES_TIMEOUT_SECONDS", "4")), 10.0))
    except ValueError:
        return 4.0


def _search_google_places(query: str) -> dict | None:
    api_key = _api_key()
    if not api_key:
        return None

    request = Request(
        GOOGLE_SEARCH_URL,
        data=json.dumps({"textQuery": query, "pageSize": 1}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": GOOGLE_FIELD_MASK,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=_timeout()) as response:
            payload = json.load(response)
    except (HTTPError, URLError, OSError, ValueError, TypeError) as error:
        logger.warning("Google Places lookup failed for %r: %s", query, error)
        return None

    if not isinstance(payload, dict):
        return None
    places = payload.get("places")
    if not isinstance(places, list) or not places or not isinstance(places[0], dict):
        return None
    return places[0]


def _serialize_place(place: dict) -> dict[str, object] | None:
    place_id = place.get("id")
    display_name = place.get("displayName")
    location = place.get("location")
    formatted_address = place.get("formattedAddress")
    if not isinstance(place_id, str) or not place_id.strip():
        return None
    if not isinstance(display_name, dict) or not isinstance(display_name.get("text"), str):
        return None
    if not isinstance(location, dict):
        return None
    try:
        latitude = float(location["latitude"])
        longitude = float(location["longitude"])
    except (KeyError, TypeError, ValueError):
        return None

    result: dict[str, object] = {
        "place_id": place_id,
        "location": formatted_address
        if isinstance(formatted_address, str)
        else display_name["text"],
        "latitude": latitude,
        "longitude": longitude,
    }
    rating = place.get("rating")
    if isinstance(rating, (int, float)) and 0 <= rating <= 5:
        result["rating"] = float(rating)
    rating_count = place.get("userRatingCount")
    if isinstance(rating_count, int) and rating_count > 0:
        result["rating_count"] = rating_count
    editorial_summary = place.get("editorialSummary")
    if isinstance(editorial_summary, dict) and isinstance(editorial_summary.get("text"), str):
        result["about"] = editorial_summary["text"]
    for source_key, response_key in (
        ("websiteUri", "website_uri"),
        ("googleMapsUri", "google_maps_uri"),
    ):
        value = place.get(source_key)
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            result[response_key] = value
    reviews = place.get("reviews")
    if isinstance(reviews, list):
        serialized_reviews: list[dict[str, str | int | float]] = []
        for review in reviews[:2]:
            if not isinstance(review, dict):
                continue
            text = review.get("text")
            author = review.get("authorAttribution")
            text_value = text.get("text") if isinstance(text, dict) else None
            author_value = author.get("displayName") if isinstance(author, dict) else None
            if not isinstance(text_value, str) or not text_value.strip():
                continue
            item: dict[str, str | int | float] = {"text": text_value}
            if isinstance(author_value, str) and author_value.strip():
                item["author"] = author_value
            rating_value = review.get("rating")
            if isinstance(rating_value, (int, float)) and 0 <= rating_value <= 5:
                item["rating"] = float(rating_value)
            relative_time = review.get("relativePublishTimeDescription")
            if isinstance(relative_time, str) and relative_time.strip():
                item["relative_time"] = relative_time
            serialized_reviews.append(item)
        if serialized_reviews:
            result["reviews"] = serialized_reviews
    photos = place.get("photos")
    if isinstance(photos, list) and photos and isinstance(photos[0], dict):
        photo_name = photos[0].get("name")
        if isinstance(photo_name, str) and PHOTO_NAME_PATTERN.fullmatch(photo_name):
            result["photo_name"] = photo_name
    return result


@lru_cache(maxsize=512)
def resolve_place(title: str, destination: str) -> dict[str, object] | None:
    """Resolve an itinerary activity through Google Places (New)."""
    place = _search_google_places(f"{title}, {destination}")
    return _serialize_place(place) if place else None


@lru_cache(maxsize=128)
def resolve_destination(destination: str, location: str = "") -> dict[str, object] | None:
    """Resolve the selected destination through Google Places (New)."""
    query = ", ".join(part for part in (destination, location) if part.strip())
    place = _search_google_places(query)
    return _serialize_place(place) if place else None


def fetch_photo(photo_name: str) -> tuple[bytes, str] | None:
    """Fetch a photo resource returned by our Google Places resolver only."""
    if not PHOTO_NAME_PATTERN.fullmatch(photo_name):
        return None
    api_key = _api_key()
    if not api_key:
        return None
    request = Request(
        f"{GOOGLE_PLACES_BASE_URL}{quote(photo_name, safe='/')}/media?maxWidthPx=1600",
        headers={"X-Goog-Api-Key": api_key},
    )
    try:
        with urlopen(request, timeout=_timeout()) as response:
            content_type = response.headers.get_content_type()
            if not content_type.startswith("image/"):
                return None
            return response.read(), content_type
    except (HTTPError, URLError, OSError, ValueError) as error:
        logger.warning("Google Places photo fetch failed: %s", error)
        return None
