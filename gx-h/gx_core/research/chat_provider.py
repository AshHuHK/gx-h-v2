"""One non-streaming Chat Completions call. The transport never follows redirects."""
from __future__ import annotations

import http.client
import json
import os
from urllib.parse import urlsplit

from ..errors import SchemaInvalid

CAPABILITIES = {
    "kind": "one_shot_model_activity", "multi_turn": False, "resume": False,
    "tools": False, "child": False, "json_object": True, "json_schema": False,
    "cancel_local_request": True, "confirm_remote_stop": False,
    "usage": "provider_response_if_available",
}
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
RESPONSE_LIMIT = 250_000


def validated_profile(document):
    if not isinstance(document, dict):
        raise SchemaInvalid("this task has no configured Model Activity profile")
    allowed = {"revision", "provider", "model", "endpoint", "max_requests", "max_output_tokens",
               "max_input_bytes", "timeout_seconds"}
    if set(document) != allowed:
        raise SchemaInvalid(f"activity profile fields must be {sorted(allowed)}")
    profile = dict(document)
    for name in ("revision", "max_requests", "max_input_bytes", "max_output_tokens", "timeout_seconds"):
        if type(profile[name]) is not int or profile[name] < 1:
            raise SchemaInvalid(f"activity profile {name} must be a positive integer")
    if not isinstance(profile["model"], str) or not profile["model"].strip():
        raise SchemaInvalid("activity profile needs an exact expected response model")
    if not isinstance(profile["endpoint"], str):
        raise SchemaInvalid("activity profile needs a chat-completions endpoint")
    url = urlsplit(profile["endpoint"])
    if profile["provider"] == "deepseek":
        if profile["endpoint"] != DEEPSEEK_ENDPOINT:
            raise SchemaInvalid("the DeepSeek activity uses the official TLS endpoint")
    elif profile["provider"] == "local_chat":
        if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1", "localhost")
                or url.username or url.password or url.query or url.fragment
                or not url.path.endswith("/chat/completions")):
            raise SchemaInvalid("local_chat requires a loopback HTTP chat-completions endpoint")
    else:
        raise SchemaInvalid("unknown Model Activity provider")
    return profile


def post_chat(profile, request, pipe):
    """Executed in a stoppable subprocess. Only structured response data crosses the pipe."""
    connection = None
    try:
        url = urlsplit(profile["endpoint"])
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if profile["provider"] == "deepseek":
            connection = http.client.HTTPSConnection("127.0.0.1", 7890, timeout=profile["timeout_seconds"])
            connection.set_tunnel("api.deepseek.com", 443)
            headers["Authorization"] = "Bearer " + os.environ["DEEPSEEK_API_KEY"]
        else:
            connection = http.client.HTTPConnection(url.hostname, url.port, timeout=profile["timeout_seconds"])
        connection.request("POST", url.path, json.dumps(request, ensure_ascii=False).encode(), headers)
        response = connection.getresponse()
        raw = response.read(RESPONSE_LIMIT + 1)
        if len(raw) > RESPONSE_LIMIT:
            result = {"status": "invalid_response", "reason": "response_size"}
        elif response.status != 200:
            # Never retain service error bodies (which may echo credentials).
            rejected = response.status in (400, 401, 403, 404, 413, 422, 429)
            result = {"status": "provider_rejected" if rejected else "outcome_unknown",
                      "http_status": response.status}
        else:
            try:
                result = {"status": "response_received", "body": json.loads(raw)}
            except (UnicodeError, ValueError):
                result = {"status": "invalid_response", "reason": "malformed_provider_json"}
        pipe.send(result)
    except (OSError, http.client.HTTPException, ValueError, KeyError) as exc:
        pipe.send({"status": "outcome_unknown", "reason": type(exc).__name__})
    finally:
        if connection:
            connection.close()
        pipe.close()
