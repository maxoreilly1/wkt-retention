"""ServiceM8 API: bulk reads, note writes."""
import requests
import config

BASE = "https://api.servicem8.com/api_1.0"


def _headers(write=False):
    h = {"X-Api-Key": config.SM8_API_KEY, "Accept": "application/json"}
    if write:
        h["Content-Type"] = "application/json"
    return h


def get_all(table):
    """Whole table in one call (ServiceM8 returns everything)."""
    r = requests.get(f"{BASE}/{table}.json", headers=_headers(), timeout=300)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def create_note(client_uuid, text):
    r = requests.post(f"{BASE}/Note.json", headers=_headers(True), timeout=60,
                      json={"related_object": "company", "related_object_uuid": client_uuid,
                            "note": text, "active": 1})
    r.raise_for_status()


def update_note(note_uuid, text):
    r = requests.post(f"{BASE}/Note/{note_uuid}.json", headers=_headers(True), timeout=60,
                      json={"uuid": note_uuid, "note": text, "active": 1})
    r.raise_for_status()
