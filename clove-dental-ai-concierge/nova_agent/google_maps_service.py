"""
google_maps_service.py

Production Google Maps integration for Clove Dental clinic lookups.

This module is intentionally kept separate from app.py so that all Google
Maps API concerns (Nearby Search, Place Details, distance calculations) live
in one place and app.py only ever calls into this service - it contains no
Places/Distance Matrix logic of its own.

Responsibilities:
  - Geocoding      : resolve a free-text address/city/area into lat/lng
  - Nearby Search   : find candidate clinics near a point
  - Place Details    : enrich a place with phone number, rating, hours, etc.
  - Distance calcs  : real travel distance via Distance Matrix API, with a
                      Haversine (straight-line) fallback if that API call
                      fails or is unavailable

Configuration:
  GOOGLE_MAPS_API_KEY   Required. Read from the environment only - this
                        module never hardcodes an API key. Set it via:
                            export GOOGLE_MAPS_API_KEY="your-key-here"

All network calls in this module are synchronous (via the `requests`
library). Callers running under asyncio (see app.py) should invoke these
methods through `loop.run_in_executor(...)` so the event loop is never
blocked - the same pattern app.py already uses for its boto3 Lambda calls.
"""

import os
import math
import logging
import datetime

import requests

logger = logging.getLogger("google_maps_service")


class GoogleMapsServiceError(Exception):
    """Raised when a Google Maps API call fails in a way the caller must handle."""
    pass


class GoogleMapsService:
    """Thin, production-oriented wrapper around the Google Maps Places,
    Geocoding, and Distance Matrix HTTP APIs."""

    GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
    NEARBY_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    PLACE_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
    DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

    DEFAULT_SEARCH_KEYWORD = "Clove Dental"
    DEFAULT_SEARCH_RADIUS_METERS = 10000  # 10 km
    REQUEST_TIMEOUT_SECONDS = 6

    DEFAULT_PLACE_DETAIL_FIELDS = [
        "name",
        "formatted_address",
        "formatted_phone_number",
        "international_phone_number",
        "rating",
        "user_ratings_total",
        "opening_hours",
        "geometry",
        "url",
    ]

    def __init__(self, api_key=None):
        # Never hardcode the key - always source it from the environment
        # (or an explicit override, e.g. for testing).
        self.api_key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY")
        if not self.api_key:
            logger.warning(
                "GOOGLE_MAPS_API_KEY is not set. GoogleMapsService calls will "
                "fail until this environment variable is configured."
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_api_key(self):
        if not self.api_key:
            raise GoogleMapsServiceError(
                "GOOGLE_MAPS_API_KEY is not configured. Set it as an environment variable."
            )

    def _get(self, url, params):
        """Perform a GET request against a Google Maps API endpoint."""
        self._ensure_api_key()
        request_params = dict(params)
        request_params["key"] = self.api_key
        try:
            response = requests.get(url, params=request_params, timeout=self.REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Google Maps API request to {url} failed: {e}")
            raise GoogleMapsServiceError(f"Google Maps API request failed: {e}") from e

    # ------------------------------------------------------------------
    # Geocoding
    # ------------------------------------------------------------------

    def geocode_address(self, address):
        """
        Resolve a free-text address/city/area into (lat, lng) using the
        Geocoding API. Returns None if no match was found.
        """
        data = self._get(self.GEOCODE_URL, {"address": address})

        status = data.get("status")
        if status != "OK":
            logger.warning(
                f"Geocoding API returned status={status} for address='{address}': "
                f"{data.get('error_message', '')}"
            )
            return None

        results = data.get("results", [])
        if not results:
            return None

        location = results[0]["geometry"]["location"]
        return location["lat"], location["lng"]

    # ------------------------------------------------------------------
    # Places - Nearby Search
    # ------------------------------------------------------------------

    def nearby_search(self, lat, lng, keyword=None, radius=None):
        """
        Search for places matching `keyword` near (lat, lng) using the
        Places Nearby Search API. Returns the raw list of Places API results
        (may be empty).
        """
        data = self._get(self.NEARBY_SEARCH_URL, {
            "location": f"{lat},{lng}",
            "radius": radius or self.DEFAULT_SEARCH_RADIUS_METERS,
            "keyword": keyword or self.DEFAULT_SEARCH_KEYWORD,
        })

        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            logger.warning(f"Nearby Search API returned status={status}: {data.get('error_message', '')}")
            raise GoogleMapsServiceError(f"Nearby Search failed with status: {status}")

        return data.get("results", [])

    # ------------------------------------------------------------------
    # Places - Place Details
    # ------------------------------------------------------------------

    def place_details(self, place_id, fields=None):
        """
        Retrieve detailed information for a place (phone number, rating,
        opening hours, etc.) using the Place Details API.
        Returns an empty dict if the place could not be found/looked up.
        """
        data = self._get(self.PLACE_DETAILS_URL, {
            "place_id": place_id,
            "fields": ",".join(fields or self.DEFAULT_PLACE_DETAIL_FIELDS),
        })

        status = data.get("status")
        if status != "OK":
            logger.warning(
                f"Place Details API returned status={status} for place_id={place_id}: "
                f"{data.get('error_message', '')}"
            )
            return {}

        return data.get("result", {})

    # ------------------------------------------------------------------
    # Distance calculations
    # ------------------------------------------------------------------

    def distance_matrix(self, origin_lat, origin_lng, destinations):
        """
        Compute real travel distance/duration from an origin point to a list
        of (lat, lng) destinations using the Distance Matrix API.

        Returns a list aligned with `destinations`, where each entry is either
        a dict {distanceText, distanceMeters, durationText, durationSeconds}
        or None if that leg could not be computed. Returns a list of Nones
        (rather than raising) on outright API failure so callers can fall
        back to `haversine_distance_km`.
        """
        if not destinations:
            return []

        destinations_param = "|".join(f"{lat},{lng}" for lat, lng in destinations)

        try:
            data = self._get(self.DISTANCE_MATRIX_URL, {
                "origins": f"{origin_lat},{origin_lng}",
                "destinations": destinations_param,
            })
        except GoogleMapsServiceError as e:
            logger.warning(f"Distance Matrix API call failed, will fall back to Haversine: {e}")
            return [None] * len(destinations)

        if data.get("status") != "OK":
            logger.warning(
                f"Distance Matrix API returned status={data.get('status')}: {data.get('error_message', '')}"
            )
            return [None] * len(destinations)

        rows = data.get("rows", [])
        if not rows:
            return [None] * len(destinations)

        elements = rows[0].get("elements", [])
        results = []
        for element in elements:
            if element.get("status") == "OK":
                results.append({
                    "distanceText": element["distance"]["text"],
                    "distanceMeters": element["distance"]["value"],
                    "durationText": element["duration"]["text"],
                    "durationSeconds": element["duration"]["value"],
                })
            else:
                results.append(None)

        # Pad in case the API returned fewer elements than requested
        while len(results) < len(destinations):
            results.append(None)

        return results

    @staticmethod
    def haversine_distance_km(lat1, lng1, lat2, lng2):
        """
        Straight-line ("as the crow flies") distance in kilometers between two
        coordinates. Used as a fallback when the Distance Matrix API is
        unavailable or fails for a given leg.
        """
        earth_radius_km = 6371.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        d_phi = math.radians(lat2 - lat1)
        d_lambda = math.radians(lng2 - lng1)

        a = (
            math.sin(d_phi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return earth_radius_km * c

    # ------------------------------------------------------------------
    # High-level helpers used by app.py's tools
    # ------------------------------------------------------------------

    def find_nearby_clinics(self, lat=None, lng=None, address=None, keyword=None, radius=None, max_results=3):
        """
        Resolve an origin point (from lat/lng, or by geocoding a free-text
        address/city/area), run a Nearby Search for clinics, enrich each
        candidate with Place Details, compute distance from the origin, and
        return the `max_results` closest clinics sorted by distance ascending.

        Each returned dict contains: name, address, placeId, distanceKm,
        distanceText, rating, userRatingsTotal, phoneNumber, openNow,
        googleMapsUrl.
        """
        origin_lat, origin_lng = lat, lng

        if origin_lat is None or origin_lng is None:
            if not address:
                raise GoogleMapsServiceError("Either lat/lng or an address must be provided")
            geocoded = self.geocode_address(address)
            if not geocoded:
                return []
            origin_lat, origin_lng = geocoded

        raw_results = self.nearby_search(origin_lat, origin_lng, keyword=keyword, radius=radius)
        if not raw_results:
            return []

        destinations = []
        for place in raw_results:
            loc = place.get("geometry", {}).get("location", {})
            destinations.append((loc.get("lat"), loc.get("lng")))

        distance_results = self.distance_matrix(origin_lat, origin_lng, destinations)
        if not distance_results:
            distance_results = [None] * len(raw_results)

        enriched = []
        for place, dest, dist_info in zip(raw_results, destinations, distance_results):
            place_id = place.get("place_id")
            details = self.place_details(place_id) if place_id else {}

            if dist_info:
                distance_km = round(dist_info["distanceMeters"] / 1000.0, 2)
                distance_text = dist_info["distanceText"]
            elif dest[0] is not None and dest[1] is not None:
                distance_km = round(self.haversine_distance_km(origin_lat, origin_lng, dest[0], dest[1]), 2)
                distance_text = f"{distance_km} km (approx.)"
            else:
                distance_km = None
                distance_text = "Unknown"

            opening_hours = details.get("opening_hours") or place.get("opening_hours") or {}

            enriched.append({
                "name": details.get("name") or place.get("name"),
                "address": details.get("formatted_address") or place.get("vicinity", ""),
                "placeId": place_id,
                "distanceKm": distance_km,
                "distanceText": distance_text,
                "rating": details.get("rating", place.get("rating")),
                "userRatingsTotal": details.get("user_ratings_total", place.get("user_ratings_total")),
                "phoneNumber": details.get("formatted_phone_number") or details.get("international_phone_number"),
                "openNow": opening_hours.get("open_now") if isinstance(opening_hours, dict) else None,
                "googleMapsUrl": details.get("url"),
            })

        enriched.sort(key=lambda clinic: (clinic["distanceKm"] is None, clinic["distanceKm"]))
        return enriched[:max_results]

    def get_clinic_hours(self, place_id=None, lat=None, lng=None, address=None, keyword=None):
        """
        Retrieve today's opening hours and the full weekly schedule for a
        clinic via the Place Details API.

        If `place_id` isn't provided, the nearest/best-matching clinic is
        first resolved via `find_nearby_clinics` (using lat/lng or address)
        and its place_id is used.

        Returns None if no clinic/details could be resolved.
        """
        resolved_place_id = place_id

        if not resolved_place_id:
            candidates = self.find_nearby_clinics(
                lat=lat, lng=lng, address=address,
                keyword=keyword or self.DEFAULT_SEARCH_KEYWORD,
                max_results=1,
            )
            if not candidates:
                return None
            resolved_place_id = candidates[0]["placeId"]

        details = self.place_details(resolved_place_id, fields=[
            "name", "formatted_address", "formatted_phone_number", "opening_hours",
        ])
        if not details:
            return None

        opening_hours = details.get("opening_hours") or {}
        weekday_text = opening_hours.get("weekday_text", [])

        # Google's weekday_text is Monday-first (index 0 = Monday ... 6 = Sunday),
        # matching Python's datetime.weekday() (Monday=0 ... Sunday=6).
        today_index = datetime.datetime.now().weekday()
        today_hours = weekday_text[today_index] if len(weekday_text) == 7 else None

        return {
            "placeId": resolved_place_id,
            "name": details.get("name"),
            "address": details.get("formatted_address"),
            "phoneNumber": details.get("formatted_phone_number"),
            "openNow": opening_hours.get("open_now"),
            "todayHours": today_hours,
            "weeklyHours": weekday_text,
        }