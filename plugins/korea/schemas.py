"""Model-visible schemas for the official Korea API tools."""

from __future__ import annotations


_LOCATION_PRIVACY = (
    "For a Telegram static pin, prefer the opaque location_token created by the "
    "pre-dispatch privacy hook; never repeat its raw coordinates. The token is held "
    "in process RAM for at most 10 minutes. It permits at most one successful place "
    "search and one subsequent route; routing reserves it exclusively and consumes "
    "it only after success, so a transient/API failure can be retried. Telegram "
    "itself still receives the original pin."
)


KOREA_PLACE_SEARCH = {
    "name": "korea_place_search",
    "description": (
        "Search Korean places with the official Kakao Local API, by keyword or "
        "Kakao category and optional radius. Prefer a Korean query for local "
        "names. Returns addresses, phone, place coordinates, a coarse distance band, "
        "proximity rank, Kakao place/map links, and the actual provider. Exact "
        "origin-to-place distances are intentionally withheld to prevent location "
        "triangulation. Kakao Local does not return opening hours, "
        "open-now status, prices, review counts, or stock; those fields are explicitly "
        "reported as unavailable rather than guessed. Requires Kakao Map usage to be "
        "enabled for the app and configured for its REST key. " + _LOCATION_PRIVACY
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "Place or service query, preferably in Korean.",
            },
            "category": {
                "type": "string",
                "enum": [
                    "supermarket",
                    "convenience_store",
                    "parking",
                    "fuel_or_charging",
                    "subway_station",
                    "bank",
                    "culture",
                    "real_estate",
                    "public_institution",
                    "attraction",
                    "accommodation",
                    "restaurant",
                    "cafe",
                    "hospital",
                    "pharmacy",
                ],
                "description": (
                    "Optional official Kakao category. A category-only search is a "
                    "nearby search and requires location_token or coordinates."
                ),
            },
            "location_token": {
                "type": "string",
                "pattern": "^loc_[A-Za-z0-9_-]{24,128}$",
                "description": (
                    "Opaque token produced from a static Telegram pin. It allows one "
                    "successful place search and remains usable for one subsequent "
                    "route until its 10-minute TTL expires."
                ),
            },
            "latitude": {
                "type": "number",
                "minimum": -90,
                "maximum": 90,
                "description": "Ephemeral WGS84 latitude of the search center.",
            },
            "longitude": {
                "type": "number",
                "minimum": -180,
                "maximum": 180,
                "description": "Ephemeral WGS84 longitude of the search center.",
            },
            "radius_m": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20000,
                "default": 2000,
                "description": (
                    "Search radius in metres for explicit coordinates. For a private "
                    "location_token the plugin always uses a fixed 3 km radius, so "
                    "repeated radius probing cannot recover exact POI distances."
                ),
            },
            "sort": {
                "type": "string",
                "enum": ["accuracy", "distance"],
                "default": "distance",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 15,
                "default": 10,
            },
            "page": {
                "type": "integer",
                "minimum": 1,
                "maximum": 45,
                "default": 1,
            },
        },
        "allOf": [
            {
                "anyOf": [
                    {"required": ["query"]},
                    {"required": ["category"]},
                ]
            },
            {
                "oneOf": [
                    {
                        "required": ["location_token"],
                        "not": {
                            "anyOf": [
                                {"required": ["latitude"]},
                                {"required": ["longitude"]},
                            ]
                        },
                    },
                    {
                        "required": ["latitude", "longitude"],
                        "not": {"required": ["location_token"]},
                    },
                    {
                        "not": {
                            "anyOf": [
                                {"required": ["location_token"]},
                                {"required": ["latitude"]},
                                {"required": ["longitude"]},
                            ]
                        }
                    },
                ]
            },
            {
                "if": {"not": {"required": ["query"]}},
                "then": {
                    "anyOf": [
                        {"required": ["location_token"]},
                        {"required": ["latitude", "longitude"]},
                    ]
                },
            },
        ],
        "additionalProperties": False,
    },
}


KOREA_GEOCODE = {
    "name": "korea_geocode",
    "description": (
        "Convert a Korean road-name or parcel address to WGS84 coordinates with "
        "the official Kakao Map address API. Exclude apartment/unit details when "
        "possible because Kakao documents that detailed suffixes reduce accuracy."
        " Requires Kakao Map usage to be enabled and configured for the REST key."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "minLength": 1,
                "maxLength": 300,
                "description": "Korean road-name or parcel address.",
            },
            "exact": {
                "type": "boolean",
                "default": False,
                "description": "Require an exact building-name match where supported.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "default": 5,
            },
        },
        "required": ["address"],
        "additionalProperties": False,
    },
}


KOREA_REVERSE_GEOCODE = {
    "name": "korea_reverse_geocode",
    "description": (
        "Convert ephemeral WGS84 coordinates to Korean parcel and road-name "
        "addresses with the official Kakao Map API when coordinates were explicitly "
        "supplied by the user. A Telegram location_token is intentionally rejected: "
        "turning a private current-point token into a persistable exact address would "
        "break the ephemeral-location boundary. A road address may be absent. "
        "Requires Kakao Map usage to be enabled and configured for the REST key."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "longitude": {"type": "number", "minimum": -180, "maximum": 180},
        },
        "required": ["latitude", "longitude"],
        "additionalProperties": False,
    },
}


KOREA_ROUTE = {
    "name": "korea_route",
    "description": (
        "Build a Korean walking, public-transit, or car route using official Kakao "
        "APIs. Walking and transit use Kakao Map REST routing; car uses Kakao "
        "Mobility Directions. Returns compact route details, actual provider, and "
        "documented KakaoMap route links. Transit data is an estimate, not a ticket "
        "or guarantee of live service. Walking/transit require Kakao Map usage to be "
        "enabled and configured for the REST key; a bare key can receive HTTP 403. "
        + _LOCATION_PRIVACY
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["walking", "transit", "car"],
            },
            "origin_latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "origin_longitude": {
                "type": "number",
                "minimum": -180,
                "maximum": 180,
            },
            "origin_location_token": {
                "type": "string",
                "pattern": "^loc_[A-Za-z0-9_-]{24,128}$",
                "description": (
                    "Opaque token from a static Telegram pin. Routing reserves it "
                    "exclusively during the request and consumes it after a successful "
                    "route. A transient/API failure releases it for one retry."
                ),
            },
            "destination_latitude": {
                "type": "number",
                "minimum": -90,
                "maximum": 90,
            },
            "destination_longitude": {
                "type": "number",
                "minimum": -180,
                "maximum": 180,
            },
            "origin_name": {"type": "string", "maxLength": 100, "default": "출발"},
            "destination_name": {
                "type": "string",
                "maxLength": 100,
                "default": "도착",
            },
            "walking_preference": {
                "type": "string",
                "enum": ["broad_first", "shortest", "accessible"],
                "default": "broad_first",
            },
            "car_priority": {
                "type": "string",
                "enum": ["recommend", "time", "distance"],
                "default": "recommend",
            },
            "max_routes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "default": 3,
                "description": "Maximum transit or alternative routes to return.",
            },
        },
        "required": [
            "mode",
            "destination_latitude",
            "destination_longitude",
        ],
        "oneOf": [
            {
                "required": ["origin_location_token"],
                "not": {
                    "anyOf": [
                        {"required": ["origin_latitude"]},
                        {"required": ["origin_longitude"]},
                    ]
                },
            },
            {
                "required": ["origin_latitude", "origin_longitude"],
                "not": {"required": ["origin_location_token"]},
            },
        ],
        "additionalProperties": False,
    },
}


KOREA_SHOPPING_SEARCH = {
    "name": "korea_shopping_search",
    "description": (
        "Generate a Naver Shopping web-search URL for the local browser. This is a "
        "zero-API browser handoff: it reads no Naver credential and makes no network "
        "request. Browser catalog results are not proof of physical shelf stock."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


KOREA_KAKAO_LINKS = {
    "name": "korea_kakao_links",
    "description": (
        "Generate documented destination-only KakaoMap map/directions links plus "
        "Kakao Mobility's official Kakao T app-launch page. "
        "Kakao does not document a public Kakao T URL that pre-fills a taxi "
        "destination, so the response says destination_prefilled=false instead of "
        "inventing an unsupported deeplink."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "destination_name": {"type": "string", "minLength": 1, "maxLength": 100},
            "destination_latitude": {
                "type": "number",
                "minimum": -90,
                "maximum": 90,
            },
            "destination_longitude": {
                "type": "number",
                "minimum": -180,
                "maximum": 180,
            },
            "place_id": {
                "type": "string",
                "pattern": "^[0-9]+$",
                "description": "Optional Kakao place ID from korea_place_search.",
            },
        },
        "required": [
            "destination_name",
            "destination_latitude",
            "destination_longitude",
        ],
        "additionalProperties": False,
    },
}
