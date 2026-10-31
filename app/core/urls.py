"""Pure public URL validation shared by contracts and network managers."""
import ipaddress
from urllib.parse import urlsplit

def safe_public_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.hostname.lower() == "localhost":
            return False
        try:
            return not ipaddress.ip_address(parsed.hostname).is_private
        except ValueError:
            return True
    except ValueError:
        return False
