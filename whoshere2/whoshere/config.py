"""Settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, got {raw!r}")
    return max(value, minimum)


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # UniFi connection
    unifi_url: str
    unifi_api_key: str
    unifi_username: str
    unifi_password: str
    unifi_site: str
    unifi_verify_ssl: bool

    # Behaviour
    poll_interval: int          # seconds between polls
    departure_grace: int        # seconds a device must be missing before someone "leaves"
    stale_after: int            # seconds without a good poll before the board refuses to say "all clear"
    history_days: int           # how long arrival/departure history is kept
    device_retention_days: int  # unassigned devices not seen for this long are forgotten

    # Self-registration (/register): staff add their own phone
    self_register: bool
    register_code: str
    staff_wifi: str
    show_wifi_network: bool          # show which Wi-Fi network people/devices are on
    wifi_networks: tuple[str, ...]   # the staff networks to watch; empty = all
    # Google Chat (all optional; Chat is on when chat_subscription is set)
    chat_subscription: str           # projects/<project>/subscriptions/<subscription>
    chat_key_file: Path
    chat_allowed: tuple[str, ...]  # emails and @domains allowed to use the app; empty = anyone
    chat_private_replies: bool     # in shared spaces, answer only the person who asked
    chat_avatar_url: str           # public https image for the card header; empty = no image
    board_url: str                 # link added to Chat replies, e.g. http://whoshere.local:8085

    # Web
    title: str
    admin_password: str
    view_password: str
    secret_key: str
    timezone: ZoneInfo
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "whoshere.db"

    def is_watched_wifi(self, ssid: str | None) -> bool:
        """True if ssid is one of WIFI_NETWORKS (case-insensitive), or no list is set."""
        if not self.wifi_networks:
            return True
        return bool(ssid) and ssid.lower() in {n.lower() for n in self.wifi_networks}

    @property
    def auth_mode(self) -> str:
        if self.unifi_api_key:
            return "api_key"
        if self.unifi_username and self.unifi_password:
            return "password"
        return "none"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(_str("DATA_DIR", "/data"))
        data_dir.mkdir(parents=True, exist_ok=True)

        poll = _int("POLL_INTERVAL_SECONDS", 60, minimum=10)
        wifi_networks: tuple[str, ...] = tuple(dict.fromkeys(
            n.strip() for n in _str("WIFI_NETWORKS").split(",") if n.strip()))
        tz_name = _str("TZ", "America/Phoenix") or "America/Phoenix"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            raise SystemExit(f"Unknown TZ {tz_name!r}; use a name like America/Phoenix")

        return cls(
            unifi_url=_str("UNIFI_URL").rstrip("/"),
            unifi_api_key=_str("UNIFI_API_KEY"),
            unifi_username=_str("UNIFI_USERNAME"),
            unifi_password=os.environ.get("UNIFI_PASSWORD", ""),
            unifi_site=_str("UNIFI_SITE", "default") or "default",
            unifi_verify_ssl=_bool("UNIFI_VERIFY_SSL", False),
            poll_interval=poll,
            departure_grace=_int("DEPARTURE_GRACE_MINUTES", 10, minimum=1) * 60,
            stale_after=_int("STALE_AFTER_SECONDS", max(poll * 3, 180), minimum=poll),
            history_days=_int("HISTORY_DAYS", 365, minimum=1),
            device_retention_days=_int("DEVICE_RETENTION_DAYS", 30, minimum=1),
            self_register=_bool("SELF_REGISTER", False),
            register_code=os.environ.get("REGISTER_CODE", "").strip(),
            staff_wifi=_str("STAFF_WIFI_NAME") or " or ".join(wifi_networks) or "the staff Wi-Fi",
            show_wifi_network=_bool("SHOW_WIFI_NETWORK", True),
            chat_subscription=_chat_subscription(),
            chat_key_file=Path(_str("GOOGLE_CHAT_KEY_FILE") or str(data_dir / "google-chat-key.json")),
            chat_allowed=tuple(a.strip().lower() for a in _str("GOOGLE_CHAT_ALLOWED").split(",") if a.strip()),
            chat_private_replies=_bool("GOOGLE_CHAT_PRIVATE_REPLIES", True),
            board_url=_str("BOARD_URL").rstrip("/"),
            chat_avatar_url=_str("CHAT_AVATAR_URL"),
            wifi_networks=wifi_networks,
            title=_str("SITE_TITLE", "Who's Here?") or "Who's Here?",
            admin_password=os.environ.get("ADMIN_PASSWORD", ""),
            view_password=os.environ.get("VIEW_PASSWORD", ""),
            secret_key=_str("SECRET_KEY") or _load_or_create_secret(data_dir),
            timezone=tz,
            data_dir=data_dir,
        )


def _chat_subscription() -> str:
    """Full subscription path, from GOOGLE_CHAT_SUBSCRIPTION (or project + short id)."""
    sub = _str("GOOGLE_CHAT_SUBSCRIPTION")
    project = _str("GOOGLE_CHAT_PROJECT")
    if sub and not sub.startswith("projects/"):
        if not project:
            raise SystemExit("GOOGLE_CHAT_SUBSCRIPTION must look like projects/<project>/subscriptions/<name>, "
                             "or set GOOGLE_CHAT_PROJECT too")
        sub = f"projects/{project}/subscriptions/{sub}"
    if sub and not re.match(r"^projects/[^/]+/subscriptions/[^/]+$", sub):
        raise SystemExit(f"GOOGLE_CHAT_SUBSCRIPTION {sub!r} should look like projects/<project>/subscriptions/<name>")
    return sub


def _load_or_create_secret(data_dir: Path) -> str:
    """Keep login sessions valid across restarts without asking the user for a key."""
    path = data_dir / ".secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key
