#!/usr/bin/env python3
"""Shared config loader for notify.py / autosend.py.

Reads .env (gitignored) plus owner.json / state.json / postings.json paths
used by the rest of the pipeline, so the small tools stay in-sync.
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE, ".env")


def load_env():
    env = {}
    p = ENV_FILE
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def get_config():
    env = load_env()
    user = env.get("GMAIL_USER") or os.environ.get("GMAIL_USER", "")
    pw = env.get("GMAIL_APP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD", "")
    topic = env.get("NTFY_TOPIC") or os.environ.get("NTFY_TOPIC", "")
    url = env.get("NTFY_URL") or os.environ.get("NTFY_URL", "https://ntfy.sh")
    missing = [k for k, v in (("GMAIL_USER", user), ("GMAIL_APP_PASSWORD", pw), ("NTFY_TOPIC", topic)) if not v]
    if missing:
        raise SystemExit(f"config missing in .env: {', '.join(missing)}")
    return {"user": user, "pw": pw, "topic": topic, "url": url}