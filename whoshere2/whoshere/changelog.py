"""Revision history shown in the page footer.

Add a new entry at the TOP for every release and bump __version__ in __init__.py to match
(a test checks they agree). Minor version (2.1 -> 2.2) for small and most changes,
major version (2.x -> 3.0) for big ones.
"""

CHANGELOG = [
    {
        "version": "2.3.0",
        "date": "2026-11-19",
        "changes": [
            "New Backup page: export people, their devices, custom location names and hidden devices to a file, optionally with history",
            "Import an export file on a new server, either adding to what's there or replacing everything",
            "The Backup page also imports a v1 users.txt (moved from the People page)",
            "Command-line export and import for moving servers from a terminal",
            "Revision history is now its own page, linked from the footer, and marks the version that's running",
        ],
    },
    {
        "version": "2.2.0",
        "date": "2026-11-19",
        "changes": [
            "\"Here?\" (not seen for a few minutes, may be leaving) is now shown in yellow instead of green",
            "The banner marks those people in yellow too, with when they were last seen",
        ],
    },
    {
        "version": "2.1.0",
        "date": "2026-11-19",
        "changes": [
            "Staff can add their own phone at /register while on the staff Wi-Fi (turn on with SELF_REGISTER)",
            "Registration refuses private Wi-Fi addresses and shows iPhone and Android steps to turn them off",
            "Optional REGISTER_CODE so only staff who know the code can register",
            "Reverse proxy addresses are trusted only from FORWARDED_ALLOW_IPS",
            "Revision history added to the page footer",
            "Credit line now reads \"By Thomas Bates\"",
        ],
    },
    {
        "version": "2.0.0",
        "date": "2026-11-12",
        "changes": [
            "Rewritten as a Docker container (Python, SQLite) to run on a Synology NAS or any Docker host",
            "\"All clear to arm\" banner; says \"Can't confirm\" when UniFi data is out of date",
            "History of arrivals and departures, filterable by person and date range",
            "Devices are discovered from UniFi and assigned to people from a list; name matches are suggested",
            "Access point names come from UniFi automatically, with optional display names",
            "10-minute grace period before someone counts as gone, so dozing phones don't flicker",
            "Sign in to UniFi with an API key (Network 9+), or username and password",
            "Import people from the v1 users.txt",
        ],
    },
    {
        "version": "1.5",
        "date": "2020-05-26",
        "changes": ["Original PHP version (github.com/batesta/whoshere)"],
    },
]
