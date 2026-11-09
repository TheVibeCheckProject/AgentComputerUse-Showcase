"""Domain-Locked Credential Manager for AgentComputerUse.

Stores passwords securely in Windows Credential Manager via keyring (encrypted with Windows DPAPI).
Guarantees:
- Passwords are strictly locked to verified domain patterns (anti-phishing).
- Secrets are typed directly into target fields by hands (secret=True, caps lock off).
- Zero LLM exposure: the model never sees passwords, and credentials are never logged to disk or IPC.
"""
import re
import urllib.parse

SERVICE_NAME = "AlwaysOnAgent_ComputerUse"


class CredentialError(Exception):
    pass


class DomainMismatchError(CredentialError):
    pass


def extract_hostname(url: str | None) -> str:
    """Extracts lowercase hostname from a full URL."""
    if not url:
        return ""
    u = url.strip()
    if not re.match(r"^[a-zA-Z]+://", u):
        u = "https://" + u
    try:
        parsed = urllib.parse.urlparse(u)
        host = (parsed.hostname or "").lower().strip()
        # Strip port if present
        return host.split(":")[0]
    except Exception:
        return ""


def matches_domain(url: str, allowed_domain: str) -> bool:
    """Strictly verifies if url's host matches or is a subdomain of allowed_domain.

    e.g. 'accounts.google.com' matches 'google.com' (True)
         'evilgoogle.com' does NOT match 'google.com' (False)
         'google.com.phishing.net' does NOT match 'google.com' (False)
    """
    host = extract_hostname(url)
    dom = (allowed_domain or "").lower().strip()
    if not host or not dom:
        return False
    if host == dom:
        return True
    return host.endswith("." + dom)


def save_credential(domain: str, username: str, password: str) -> None:
    """Saves a domain-locked credential into Windows Credential Manager."""
    import keyring
    d = domain.lower().strip()
    u = username.strip()
    key = f"{d}::{u}"
    keyring.set_password(SERVICE_NAME, key, password)


def get_credential(domain: str, current_url: str) -> tuple[str, str] | None:
    """Retrieves (username, password) for domain only if current_url strictly matches domain."""
    import keyring
    d = domain.lower().strip()
    if not matches_domain(current_url, d):
        raise DomainMismatchError(
            f"Security alert: Credential for '{d}' cannot be unlocked on '{extract_hostname(current_url)}'"
        )

    # Search for credentials under SERVICE_NAME
    # We check default user formats: first specific username, then general
    try:
        # Standard query format
        for entry in list_credentials():
            if entry[0].lower() == d:
                user = entry[1]
                key = f"{d}::{user}"
                pw = keyring.get_password(SERVICE_NAME, key)
                if pw:
                    return (user, pw)
    except Exception:
        pass
    return None


def find_for_url(current_url: str) -> tuple[str, str, str] | None:
    """(domain, username, password) saved for the page's own host or a domain above it, the most specific first:
    on accounts.google.com a password saved for accounts.google.com wins over one for google.com. Only saved
    domains are ever looked up, so a look-alike host (google.com.evil.net, evilgoogle.com) finds nothing.
    (2026-10-08: the loop asked for the exact host only, so a password saved for google.com was never found on
    accounts.google.com.)"""
    import keyring
    host = extract_hostname(current_url)
    if not host:
        return None
    saved = list_credentials()
    labels = host.split(".")
    for n in range(len(labels) - 1):                 # never a bare top-level domain ("com")
        candidate = ".".join(labels[n:])
        for domain, user in saved:
            if domain.lower() == candidate and matches_domain(current_url, domain):
                pw = keyring.get_password(SERVICE_NAME, f"{candidate}::{user}")
                if pw:
                    return candidate, user, pw
    return None


def delete_credential(domain: str, username: str) -> bool:
    """Deletes a credential from Credential Manager."""
    import keyring
    key = f"{domain.lower().strip()}::{username.strip()}"
    try:
        keyring.delete_password(SERVICE_NAME, key)
        return True
    except Exception:
        return False


def list_credentials() -> list[tuple[str, str]]:
    """Lists stored credential domains and usernames (passwords are NEVER returned)."""
    # Keyring doesn't provide a cross-platform list, but Windows Credential Manager supports
    # querying via win32cred or by keeping a registry/metadata list of stored domains.
    # We maintain a metadata index in data/credentials_index.json for quick discovery.
    import json
    from pathlib import Path
    idx_file = Path(__file__).resolve().parent / "data" / "credentials_index.json"
    if not idx_file.is_file():
        return []
    try:
        data = json.loads(idx_file.read_text(encoding="utf-8"))
        return [(item["domain"], item["username"]) for item in data if "domain" in item]
    except Exception:
        return []


def record_credential_metadata(domain: str, username: str) -> None:
    import json
    from pathlib import Path
    idx_file = Path(__file__).resolve().parent / "data" / "credentials_index.json"
    idx_file.parent.mkdir(parents=True, exist_ok=True)
    items = []
    if idx_file.is_file():
        try:
            items = json.loads(idx_file.read_text(encoding="utf-8"))
        except Exception:
            items = []
    d = domain.lower().strip()
    u = username.strip()
    # Update or append
    items = [i for i in items if not (i.get("domain") == d and i.get("username") == u)]
    items.append({"domain": d, "username": u})
    idx_file.write_text(json.dumps(items, indent=2), encoding="utf-8")


def forget_credential(domain: str, username: str) -> bool:
    """Deletes the password and its index entry; True if a password was there."""
    import json
    from pathlib import Path
    gone = delete_credential(domain, username)
    idx_file = Path(__file__).resolve().parent / "data" / "credentials_index.json"
    if idx_file.is_file():
        try:
            items = json.loads(idx_file.read_text(encoding="utf-8"))
            d, u = domain.lower().strip(), username.strip()
            items = [i for i in items if not (i.get("domain") == d and i.get("username") == u)]
            idx_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
        except Exception:
            pass
    return gone


def store_secure_credential(domain: str, username: str, password: str) -> None:
    """Stores credential in Windows Credential Manager and records domain metadata."""
    save_credential(domain, username, password)
    record_credential_metadata(domain, username)
