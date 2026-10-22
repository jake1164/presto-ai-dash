# Presto AI Dash - example Presto configuration
#
# Copy to `/ai_dash_files/config.py` on the Presto and edit.
# Wi-Fi credentials live in `secrets.py`, which is NOT committed - see
# .gitignore. Never put provider API keys on the Presto.

# --- Daemon ----------------------------------------------------------------
# False polls the daemon; True keeps the offline static quota design preview.
STATIC_DEMO = True
# The daemon's LAN IP address, not a name: looking a name up can freeze the
# touch screen for as long as the lookup takes when DNS is slow or down.
DAEMON_HOST = "192.168.1.50"
DAEMON_PORT = 8787

# Must match `daemon.shared_token` in the daemon config.
SHARED_TOKEN = "change-me"

# The name this device reports to the daemon, so several devices can be told
# apart on the daemon's Status tab. Printable ASCII, at most 32 characters.
DEVICE_NAME = "presto"

# The app revision this device reports. Leave it empty: the deployment writes
# the revision next to the app files, and the device then reports the build it
# actually runs.
DEVICE_REVISION = ""

# Seconds between polls when the daemon is reachable.
POLL_INTERVAL = 15

# Seconds between retries when the daemon is unreachable.
# The plan asks for 10-15s; 12 sits in the middle.
RETRY_INTERVAL = 12

# HTTP timeout for a single poll, in seconds.
HTTP_TIMEOUT = 8
# One bounded Wi-Fi association attempt per retry, using /secrets.py.
WIFI_TIMEOUT = 10

# --- Display ---------------------------------------------------------------
# Presto() defaults to a 240x240 framebuffer on the 480x480 panel. Set True for
# the full 480x480 surface (slower, crisper).
FULL_RES = False

# --- LEDs ------------------------------------------------------------------
# 7 SK6812 LEDs. Brightness 0.0-1.0.
LED_BRIGHTNESS = 0.35
# Dim the LEDs when an alert has been muted.
LED_DIM_WHEN_MUTED = True

# --- Sound -----------------------------------------------------------------
# Piezo on GPIO 43. Sound is quiet by design.
# This is the hardware permission only: the Presto also chirps only when the
# daemon's alert settings say so (the admin page's "Sound on a new warning",
# which is off until it is turned on). The audible result is the AND of the
# two: both must be on.
SOUND_ENABLED = True
SOUND_VOLUME = 0.15          # duty cycle 0.0-1.0
CHIRP_HZ = 880               # short chirp: new warning
DOUBLE_CHIRP_HZ = 1320       # double chirp: critical alert
CHIRP_MS = 90
# Do not repeat an alarm unless this is True.
SOUND_REPEAT = False

# --- Interaction -----------------------------------------------------------
# Milliseconds a touch must be held to count as a long press (mute).
LONG_PRESS_MS = 700
# Minimum horizontal travel in pixels to count as a swipe.
SWIPE_MIN_PX = 40
# Ignore touches closer together than this (debounce), in milliseconds.
TAP_DEBOUNCE_MS = 180

# --- Fallback --------------------------------------------------------------
# If the daemon is unreachable for this long, show the offline screen.
OFFLINE_AFTER_S = 30
