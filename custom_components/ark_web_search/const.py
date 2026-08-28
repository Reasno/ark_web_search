"""Constants for the Ark Web Search integration."""
from __future__ import annotations

DOMAIN = "ark_web_search"

CONF_API_KEY = "api_key"
CONF_DEFAULT_COUNT = "default_count"
CONF_TIMEOUT = "timeout"

DEFAULT_COUNT = 5
DEFAULT_TIMEOUT = 20

API_URL = "https://open.feedcoopapi.com/search_api/web_search"

# Hard limits to keep the tool result from blowing up the model context.
MAX_COUNT = 10
MAX_SUMMARY_CHARS = 800
MAX_TOTAL_CHARS = 6000

TIME_RANGES = ["OneDay", "OneWeek", "OneMonth", "OneYear"]
