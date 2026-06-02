from __future__ import annotations

import os

API_URL = os.environ.get("OPENAI_API_URL", "https://api.openai.com/v1")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
