"""Revision history shown in the page footer.

Add a new entry at the TOP for every release and bump __version__ in __init__.py to match
(a test checks they agree). Minor version (2.1 -> 2.2) for small and most changes,
major version (2.x -> 3.0) for big ones.
"""

CHANGELOG = [
    {
        "version": "3.4.0",
        "date": "2026-11-23",
        "changes": [
            "Google Chat answers are now cards: one row per person, with the name in bold, where they are above and since when below",
            "The all-clear headline is coloured (green, red or orange), and cards have an \"Open the board\" button when BOARD_URL is set",
            "/whoshere Thomas and /history get the same one-row-per-entry layout",
            "If Chat won't take a card, the plain-text answer puts each person on their own line",
        ],
    },
    {
        "version": "3.3.0",
        "date": "2026-11-22",
        "changes": [
            "Pull down to refresh when the board is opened from a phone's home screen",
            "\"↻ Refresh now\" at the bottom of the board",
            "The Board and History refresh themselves when you switch back to them after 30 seconds or more",
        ],
    },
    {
        "version": "3.2.0",
        "date": "2026-11-21",
        "changes": [
            "New Help tab: a plain-language guide to reading the board, checking before you arm, History, setting up your phone, and Google Chat",
            "Printable one-page cheat sheets (quick guide and Google Chat guide) with your board's address, with a Print / Save as PDF button",
            "Click a person's name on the Board or in History to see their arrivals and departures, with where they are right now",
            "Better on iPhone and Android: the board and History show one card per person, and the menu is a single swipeable row",
            "Add the board to your phone's home screen to open it like an app; Help explains how on iPhone and Android",
        ],
    },
    {
        "version": "3.1.0",
        "date": "2026-11-20",
        "changes": [
            "Google Chat: the board link in replies is now a plain address (\"Board: http://…\"), fixing a broken link that included \"|Open the board\"",
        ],
    },
    {
        "version": "3.0.0",
        "date": "2026-11-20",
        "changes": [
            "Google Chat app: /whoshere lists who's on campus, /whoshere Thomas looks people up by name",
            "/allclear answers \"is it safe to arm?\": all clear, who's still here (and who may be leaving), or can't confirm when UniFi data is stale",
            "/history shows today's arrivals and departures, for everyone or one person",
            "In a direct message, plain words work too: \"all clear?\", a name, \"history\", \"help\"",
            "Connects through Google Pub/Sub, so nothing on your network is opened to the internet",
            "In shared spaces, replies go only to the person who asked (GOOGLE_CHAT_PRIVATE_REPLIES)",
            "Optional GOOGLE_CHAT_ALLOWED limits who can use it; BOARD_URL adds a link to the board",
            "New Google Chat admin page: upload the service account key, test it, see connection status and the commands to set up",
        ],
    },
    {
        "version": "2.6.0",
        "date": "2026-11-19",
        "changes": [
            "New NETWORK_MODE setting: set it to host so \"Add my phone\" sees each phone's real address (needed on Synology, where Docker's bridge network hides it)",
            "PORT sets the board's port in host mode",
            "Status page shows the address the server sees for your browser, and warns when it's only Docker's internal address",
            "\"Add my phone\" explains the problem instead of just saying the phone wasn't found",
        ],
    },
    {
        "version": "2.5.0",
        "date": "2026-11-19",
        "changes": [
            "Shows which Wi-Fi network each person is on, next to their location on the board, Devices and History (turn off with SHOW_WIFI_NETWORK=false)",
            "New WIFI_NETWORKS setting for your staff networks: the Devices list hides phones on other networks, such as guest Wi-Fi",
            "\"Add my phone\" names every staff network, asks phones on a guest network to switch first, and gives private-address steps for the network the phone is on",
            "Status page shows which networks are watched and whether UniFi is providing network names",
            "Exports include the Wi-Fi network in history",
        ],
    },
    {
        "version": "2.4.0",
        "date": "2026-11-19",
        "changes": [
            "Deploys straight from GitHub as a Portainer stack: settings go in the stack's Environment variables, no .env file needed",
            "docker-compose.yml still reads a .env file for Synology Container Manager and plain Docker Compose; unset settings use their defaults",
            "Board port can be changed with HOST_PORT (default 8085)",
            "GitHub Actions now runs the tests and publishes a ready-made image (ghcr.io/thomasbates-az/whoshere2) on every merge to main",
            "README: Portainer setup steps",
        ],
    },
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
