"""Profile Store for AgentComputerUse Form Filling.

Maintains owner profile details (names, contact, address, URLs, resume path)
for automated job applications and form filling.
Stored locally in data/profile.json (never committed to git).
"""
import json
from pathlib import Path
from typing import Any, Dict, Optional

PROFILE_PATH = Path(__file__).resolve().parent / "data" / "profile.json"

DEFAULT_PROFILE: Dict[str, str] = {
    "first_name": "",
    "last_name": "",
    "full_name": "",
    "email": "",
    "phone": "",
    "street_address": "",
    "city": "",
    "state": "",
    "zip": "",
    "country": "",
    "linkedin_url": "",
    "github_url": "",
    "portfolio_url": "",
    "resume_path": "",
}


def load_profile(path: Optional[Path] = None) -> Dict[str, str]:
    """Loads profile dictionary from disk, merging with default structure."""
    p = path or PROFILE_PATH
    if not p.is_file():
        return dict(DEFAULT_PROFILE)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        merged = dict(DEFAULT_PROFILE)
        merged.update({k: str(v) for k, v in data.items() if isinstance(v, (str, int, float))})
        return merged
    except Exception:
        return dict(DEFAULT_PROFILE)


def save_profile(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Saves profile dictionary to disk."""
    p = path or PROFILE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: str(v) for k, v in data.items()}
    p.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")


def get_field(key: str, profile: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Gets a specific profile field or None if empty."""
    prof = profile or load_profile()
    val = prof.get(key, "").strip()
    return val if val else None
