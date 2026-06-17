"""Journal config loader.

Loads `journal.json`, auto-creating a defaults file when it is missing. The
journal is disabled by default; it stays off until `enabled` is set to true.
"""

import json
import os


DEFAULT_CONFIG = {
    "enabled": False,
    "max_bytes": 1073741824,   # 1 GiB
    "max_age_seconds": 7200,    # 2 hours
    "max_body_bytes": 10240,  # max request/response body bytes kept per entry
    "redact_fields": [        # body field names replaced with "[redacted]"
        "system",
        "api_key",
        "Authorization",
        "api_secret",
        "private_key",
    ],
    "enable_gpu": True,     # run the GPU poller / expose GPU telemetry
    "enable_system": True,  # run the system poller / expose system telemetry
    "gpu_poll_interval_seconds": 5,
    "system_poll_interval_seconds": 10,
    "disk_mounts": ["/"],
    "ui": {
        "enabled": False,
        "admin_password": "",
        "session_ttl_seconds": 43200,  # 12 hours
        "theme": "system",  # "system" | "light" | "dark"
        "show_gpu": True,    # default state of the GPU panel toggle
        "show_system": True,  # default state of the system panel toggle
        # One configurable colour per connection state. The Journal UI uses
        # the same set for the state dot and the proxy-connection timeline.
        "state_colors": {
            "INIT": "#9e9e9e",             # grey   — connected / initializing
            "CLASSIFYING": "#9c27b0",      # purple — classifying the request
            "QUEUED": "#ff9800",           # orange — queued, resolving destination
            "ROUTING_REQUEST": "#2196f3",  # blue   — sending the proxied request
            "ROUTING_RESPONSE": "#3f51b5", # indigo — awaiting the proxied response
            "SUCCESS": "#4caf50",          # green  — successfully proxied
            "FAILURE": "#f44336",          # red    — error / failure
        },
    },
}


def _merged_ui(user_ui):
    merged = dict(DEFAULT_CONFIG["ui"])
    merged["state_colors"] = dict(DEFAULT_CONFIG["ui"]["state_colors"])
    if isinstance(user_ui, dict):
        user_colors = user_ui.get("state_colors")
        merged.update(user_ui)
        merged["state_colors"] = dict(DEFAULT_CONFIG["ui"]["state_colors"])
        if isinstance(user_colors, dict):
            merged["state_colors"].update(user_colors)
    return merged


def _default_cfg():
    cfg = dict(DEFAULT_CONFIG)
    cfg["redact_fields"] = list(DEFAULT_CONFIG["redact_fields"])
    cfg["ui"] = dict(DEFAULT_CONFIG["ui"])
    cfg["ui"]["state_colors"] = dict(DEFAULT_CONFIG["ui"]["state_colors"])
    return cfg


def load_config(path):
    """Load `journal.json`, creating a default file if missing.

    Returns a dict with all DEFAULT_CONFIG keys present (user values take
    precedence; missing fields fill from defaults).

    Raises ValueError when `ui.enabled` is true and `ui.admin_password` is
    empty or missing.
    """
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return _default_cfg()

    with open(path, "r") as f:
        user_cfg = json.load(f)

    cfg = dict(DEFAULT_CONFIG)
    cfg.update(user_cfg)
    cfg["ui"] = _merged_ui(user_cfg.get("ui"))

    if cfg["ui"]["enabled"] and not cfg["ui"].get("admin_password"):
        raise ValueError(
            "journal config: ui.admin_password must be set when ui.enabled is true"
        )
    return cfg
