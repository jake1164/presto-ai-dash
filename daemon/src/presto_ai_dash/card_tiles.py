"""Tiles: the short list of facts a card shows as a grid.

The tiles are built here, in the daemon, so every provider's facts have
the same shape: at most six of them, a short ASCII label, a short value and
a tone. A value the report does not carry is left out: an unknown number is
never shown as zero, and a missing reader is never shown as a reading.

The tiles are the third tier of the card work (plan item 1d). They are
worth about 1.5 KB over seven cards, which is past the payload budget, so
they travel in the lazy card detail, not in every poll.
"""

from .quotas import compact_age, compact_tokens, timestamp

# The shape of one tile.
MAX_TILES = 6
MAX_LABEL = 8
MAX_VALUE = 10
TONES = ("normal", "good", "warn", "bad", "dim")


def tile(label, value, tone="normal"):
    """One tile, inside the limits, or None when it makes no sense."""
    if not isinstance(label, str) or not isinstance(value, str):
        return None
    label = " ".join(label.split())[:MAX_LABEL]
    value = " ".join(value.split())[:MAX_VALUE]
    if not label or not value:
        return None
    if tone not in TONES:
        tone = "normal"
    if not label.isascii() or not value.isascii():
        return None
    return {"label": label, "value": value, "tone": tone}


def _number(value):
    """A real number, or None. A missing number is never a zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _money(value):
    """A dollar amount: two decimals, or more when a cent would hide it."""
    if float(value) == int(value):
        return "%d" % int(value)
    if 0 < value < 0.01:
        return "%.5f" % value
    return "%.2f" % value


def _age(moment, now):
    """How long ago a moment was, as a short phrase."""
    try:
        seconds = (now - timestamp(moment)).total_seconds()
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        seconds = 0
    return "%s ago" % compact_age(seconds)


def _claude_tiles(report, metrics, now):
    """Claude: last use, the local token totals and the included credit."""
    tiles = []
    activity = report.get("local_activity")
    if isinstance(activity, dict) and activity.get("available"):
        last = activity.get("last_request_at")
        if last:
            age = _age(last, now)
            if age:
                try:
                    seconds = (now - timestamp(last)).total_seconds()
                except (TypeError, ValueError):
                    seconds = None
                tiles.append(tile("last use", age,
                                  "good" if seconds is not None and seconds < 300
                                  else "dim"))
        if activity.get("complete", True):
            five = compact_tokens(activity.get("tokens_5h"))
            seven = compact_tokens(activity.get("tokens_7d"))
            if five is not None:
                tiles.append(tile("tok 5h", five))
            if seven is not None:
                tiles.append(tile("tok 7d", seven))
        else:
            # The cold scan is still reading the week: the totals are
            # lower bounds, and lower bounds are not shown as measurements.
            tiles.append(tile("tok 5h", "scanning", "dim"))
    cache = report.get("usage_cache")
    if isinstance(cache, dict):
        credit = cache.get("credit")
        if isinstance(credit, dict):
            limit = _number(credit.get("limit"))
            used = _number(credit.get("used"))
            if limit is not None and used is not None:
                tiles.append(tile("credit", "$%s/$%s" % (_money(limit - used),
                                                          _money(limit))))
    return tiles


def _codex_tiles(report, metrics, now):
    """Codex: the banked resets, when they expire, and the credit balance."""
    tiles = []
    credits = report.get("reset_credits")
    if isinstance(credits, dict):
        available = credits.get("available")
        if type(available) is int:
            tiles.append(tile("resets", "%d" % available,
                              "good" if available else "dim"))
        expiries = credits.get("expires_at")
        if isinstance(expiries, list):
            moments = []
            for value in expiries:
                try:
                    moments.append(timestamp(value))
                except (TypeError, ValueError):
                    continue
            if moments:
                tiles.append(tile("next exp", min(moments).strftime("%m-%d")))
    balance = report.get("credits")
    if isinstance(balance, dict):
        if balance.get("unlimited") is True:
            tiles.append(tile("credits", "unlimited", "good"))
        else:
            amount = balance.get("balance")
            if isinstance(amount, str) and amount:
                tiles.append(tile("credits", amount))
    return tiles


def _litellm_tiles(report, metrics, now):
    """LiteLLM: the requests, the tokens, the spend, the errors, the model."""
    tiles = []
    for key, label in (("requests_today", "requests"), ("tokens_today", "tokens")):
        value = _number(metrics.get(key))
        if value is not None:
            tiles.append(tile(label, "%d" % value))
    spend = _number(metrics.get("spend_today"))
    if spend is not None:
        tiles.append(tile("spend", "$%s" % _money(spend)))
    errors = _number(metrics.get("errors_today"))
    if errors is not None:
        tiles.append(tile("errors", "%d" % errors, "bad" if errors else "good"))
    model = metrics.get("top_model")
    if isinstance(model, str) and model.strip():
        tiles.append(tile("model", model))
    return tiles


def gigabytes(size):
    """A size in bytes as "8.4 GB", or "58 GB" from ten up."""
    gigs = size / 1e9
    return "%.1f GB" % gigs if gigs < 10 else "%d GB" % round(gigs)


def _ollama_tiles(report, metrics, now):
    """Ollama: the loaded model, where it runs, what it holds and how many there are."""
    tiles = []
    model = metrics.get("loaded_model")
    if isinstance(model, str) and model.strip():
        tiles.append(tile("model", model))
    gpu = _number(metrics.get("gpu_percent"))
    if gpu is not None:
        # All on the GPU is the fast case; a partial share means some layers
        # run on the CPU, which is slower, and none is a CPU-only machine.
        tiles.append(tile("gpu", "%d%%" % gpu,
                          "normal" if gpu >= 100 else "warn" if gpu > 0 else "dim"))
    memory = _number(metrics.get("loaded_memory_bytes"))
    if memory is not None:
        tiles.append(tile("memory", gigabytes(memory)))
    context = _number(metrics.get("loaded_context"))
    if context is not None and context >= 1024:
        tiles.append(tile("context", "%dk" % round(context / 1024)))
    expires = metrics.get("loaded_expires_at")
    if expires:
        try:
            left = (timestamp(expires) - now).total_seconds()
        except (TypeError, ValueError):
            left = None
        if left is not None:
            # A model kept loaded without limit has an expiry years away.
            tiles.append(tile("unloads", "kept" if left > 86400 else "now" if left <= 0
                              else compact_age(left)))
    speed = _number(metrics.get("last_measured_tokens_per_sec"))
    if speed is not None and speed > 0:
        tiles.append(tile("speed", "%.0f tok/s" % speed))
    models = _number(metrics.get("available_models"))
    if models is not None:
        tiles.append(tile("models", "%d" % models))
    return tiles


def _text(value):
    """A reported text, or None when it is missing or says it is unavailable."""
    if isinstance(value, str) and value.strip() and value != "unavailable":
        return value.strip()
    return None


def local_readings(source_type, metrics, now):
    """Labelled facts for the admin page, for a service that has no quota windows.

    The admin page has room for the whole story, so these are written out in
    words, unlike the tiles. Only what the service really reported is listed.
    """
    rows = []

    def add(label, value):
        if value is not None:
            rows.append([label, value])

    if source_type == "ollama":
        add("Version", _text(metrics.get("version")))
        models = _number(metrics.get("available_models"))
        add("Models installed", None if models is None else "%d" % models)
        disk = _number(metrics.get("disk_bytes"))
        add("Disk used by models", None if disk is None else gigabytes(disk))
        loaded = _text(metrics.get("loaded_model"))
        add("Loaded model", loaded or ("none" if models is not None else None))
        if loaded:
            shape = " ".join(str(metrics[key]) for key in ("loaded_params", "loaded_quant")
                             if metrics.get(key))
            add("Size and quantisation", shape or None)
            memory = _number(metrics.get("loaded_memory_bytes"))
            add("Memory in use", None if memory is None else gigabytes(memory))
            gpu = _number(metrics.get("gpu_percent"))
            if gpu is not None:
                add("On the GPU", "%d%%%s" % (gpu, "" if gpu else " (CPU only)"))
            context = _number(metrics.get("loaded_context"))
            add("Context length", None if context is None else "{:,} tokens".format(int(context)))
            expires = metrics.get("loaded_expires_at")
            if expires:
                try:
                    left = (timestamp(expires) - now).total_seconds()
                except (TypeError, ValueError):
                    left = None
                if left is not None:
                    add("Unloads", "kept loaded" if left > 86400 else "now" if left <= 0
                        else "in " + compact_age(left))
        speed = _number(metrics.get("last_measured_tokens_per_sec"))
        if speed is not None and speed > 0:
            add("Last measured speed", "%.1f tokens/s" % speed)
    elif source_type == "litellm":
        add("Proxy", None if metrics.get("online") is None
            else "online" if metrics["online"] else "offline")
        add("Database", _text(metrics.get("database")))
        for key, label in (("requests_today", "Requests today"), ("tokens_today", "Tokens today"),
                           ("errors_today", "Failed requests today")):
            value = _number(metrics.get(key))
            add(label, None if value is None else "{:,}".format(int(value)))
        spend = _number(metrics.get("spend_today"))
        add("Spend today", None if spend is None else "$" + _money(spend))
        add("Top model today", _text(metrics.get("top_model")))
    return rows


def _activity_tiles(report, metrics, now):
    """A report that carries activity totals: requests, tokens and spend."""
    tiles = []
    activity = report.get("activity")
    if not isinstance(activity, dict):
        return tiles
    count = _number(activity.get("request_count"))
    if count is not None:
        tiles.append(tile("requests", "%d" % count))
    first = _number(activity.get("input_tokens"))
    second = _number(activity.get("output_tokens"))
    if first is not None and second is not None:
        total = compact_tokens(first + second)
        if total is not None:
            tiles.append(tile("tokens", total))
    cost = _number(activity.get("usage_usd"))
    if cost is not None:
        tiles.append(tile("spend", "$%s" % _money(cost)))
    return tiles


def _no_tiles(report, metrics, now):
    """A report that carries only allowance windows has nothing to tile.

    The card already draws each window as a quota row, with its bar and reset
    time; a tile that repeats the percentage would only take the space.
    """
    return []


def _cloud_tiles(report, metrics, now):
    """Monthly included credits stay separate from purchased funds and activity."""
    tiles = []
    balance = report.get("cloud_balance")
    if isinstance(balance, dict):
        stale = balance.get("state") == "stale"
        tone = "dim" if stale else "normal"
        for key, label in (("included_remaining_usd", "left"),
                           ("purchased_remaining_usd", "bought")):
            value = _number(balance.get(key))
            if value is not None:
                tiles.append(tile(("old buy" if label == "bought" else "old " + label) if stale else label,
                                  "$%s" % _money(value), tone))
        for window in report.get("windows", []):
            if window.get("id") == "monthly_credits" and _number(window.get("limit_amount")) is not None:
                tiles.append(tile("included", "$%s" % _money(window["limit_amount"]), tone))
    activity = _activity_tiles(report, metrics, now)
    period = report.get("activity", {}).get("range")
    if period:
        labels = {"requests": "req " + period, "tokens": "tok " + period, "spend": "$ " + period}
        for item in activity:
            if item:
                item["label"] = labels[item["label"]]
    if report.get("activity", {}).get("state") == "stale":
        for item in activity:
            if item:
                item["tone"] = "dim"
    return tiles + activity


def _generic_tiles(report, metrics, now):
    """Anything else: the activity it carries. Windows are quota rows."""
    return _activity_tiles(report, metrics, now)


BUILDERS = {
    "claude": _claude_tiles,
    "codex": _codex_tiles,
    "litellm": _litellm_tiles,
    "ollama": _ollama_tiles,
    "ollama_cloud": _cloud_tiles,
    "opencode_go": _no_tiles,
    "opencode": _no_tiles,
}


def tiles_for(source_type, source, now=None):
    """The tiles of a source, from the data it really reported.

    ``source`` is one entry of the daemon's status payload: its quota
    report and its metrics. Only the values the report really carries
    become tiles; anything missing is left out.
    """
    if not isinstance(source, dict):
        return []
    report = source.get("quota")
    report = report if isinstance(report, dict) else {}
    metrics = source.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    if now is None:
        from .models import now_utc
        now = now_utc()
    builder = BUILDERS.get(source_type, _generic_tiles)
    tiles = []
    for item in builder(report, metrics, now):
        if item:
            tiles.append(item)
        if len(tiles) >= MAX_TILES:
            break
    return tiles[:MAX_TILES]
