"""The saved account order survives on the device, whose sort is not stable.

MicroPython's list.sort() and sorted() are NOT stable. On a real Presto,
sorting ['dash', 'go', 'codex', 'claude', 'ocloud', 'ollama', 'litellm'] by a
key that is 0 for 'dash' and 1 for the rest returned
['dash', 'ocloud', 'litellm', 'ollama', 'go', 'claude', 'codex']: the swipe
order was scrambled on the device while the preview, which runs on CPython
with a stable sort, looked right.

These tests stand in for the device by shadowing ``sorted`` in the module under
test with a version that scrambles tied keys, and they scan presto/ so a new
sort with a tie cannot slip in.
"""

import ast
import os
import unittest
from unittest.mock import patch

import presto_path  # noqa: F401  (puts the device modules on the path)
import cards
import home


def hostile_sorted(iterable, key=None, reverse=False):
    """A sort that is correct for distinct keys and scrambles equal ones."""
    items = list(iterable)
    items.reverse()
    return sorted(items, key=key, reverse=reverse)


def forbidden(*args, **kwargs):
    raise AssertionError("this path must not sort: the device's sort is not stable")


def card(card_id):
    return {"id": card_id, "title": card_id.title(), "status": "ok",
            "primary": "p", "secondary": "s", "detail": [], "accent": "green",
            "alert": None}


def payload(order, home_at=0, accounts=True):
    ids = list(order)
    ids.insert(home_at, "dash")
    result = {"cards": [card(item) for item in ids]}
    if accounts:
        result["card_order"] = "accounts"
    return result


SEVEN = ["go", "codex", "claude", "ocloud", "ollama", "litellm"]


class SwipeOrderTests(unittest.TestCase):
    def ids(self, data):
        parsed, error = cards.parse_cards(data)
        self.assertIsNone(error)
        return [item.id for item in parsed]

    def test_the_saved_order_is_kept_with_home_first(self):
        self.assertEqual(self.ids(payload(SEVEN)), ["dash"] + SEVEN)

    def test_home_moves_to_the_front_when_the_daemon_puts_it_elsewhere(self):
        self.assertEqual(self.ids(payload(SEVEN, home_at=3)), ["dash"] + SEVEN)

    def test_the_account_path_does_not_sort_at_all(self):
        with patch.object(cards, "sorted", forbidden, create=True):
            self.assertEqual(self.ids(payload(SEVEN, home_at=2)), ["dash"] + SEVEN)

    def test_a_scrambling_sort_cannot_change_the_account_order(self):
        with patch.object(cards, "sorted", hostile_sorted, create=True):
            self.assertEqual(self.ids(payload(SEVEN)), ["dash"] + SEVEN)

    def test_without_a_saved_order_unknown_cards_keep_their_position(self):
        ids = ["zeta", "alpha", "mu", "beta", "kappa", "gamma", "omega"]
        data = payload(ids, accounts=False)
        with patch.object(cards, "sorted", hostile_sorted, create=True):
            result = self.ids(data)
        self.assertEqual(result, ["dash"] + ids)

    def test_without_a_saved_order_known_cards_come_first_in_their_own_order(self):
        known = [name for name in reversed(cards.CARD_ORDER) if name != "dash"][:2]
        data = payload(["zeta"] + known + ["alpha"], accounts=False)
        with patch.object(cards, "sorted", hostile_sorted, create=True):
            result = self.ids(data)
        expected = sorted(["dash"] + known, key=cards.CARD_ORDER.index) + ["zeta", "alpha"]
        self.assertEqual(result, expected)


class HomeRowOrderTests(unittest.TestCase):
    def source(self, name, quota):
        item = {"id": name, "type": "litellm", "name": name, "status": "ok",
                "metrics": {"requests_today": 1}}
        if quota:
            item = {"id": name, "type": "claude", "name": name, "status": "ok",
                    "quota": {"windows": [{"label": "Weekly", "period_minutes": 10080,
                                           "used_percent": 5, "state": "available",
                                           "resets_at": "2026-10-25T12:00:00+00:00",
                                           "observed_at": "2026-10-20T11:55:00+00:00"}]}}
        return item

    def names(self, data):
        return [row["name"] for row in home.source_rows(data)]

    def test_a_saved_order_keeps_home_rows_in_order(self):
        sources = [self.source(name, quota) for name, quota in
                   (("a", False), ("b", True), ("c", False), ("d", True), ("e", False), ("f", True))]
        data = {"card_order": "accounts", "sources": sources,
                "updated_at": "2026-10-20T12:00:00+00:00"}
        with patch.object(home, "sorted", forbidden, create=True):
            self.assertEqual(self.names(data), ["a", "b", "c", "d", "e", "f"])

    def test_without_a_saved_order_quota_accounts_lead_and_each_group_keeps_its_order(self):
        sources = [self.source(name, quota) for name, quota in
                   (("a", False), ("b", True), ("c", False), ("d", True), ("e", False), ("f", True))]
        data = {"sources": sources, "updated_at": "2026-10-20T12:00:00+00:00"}
        with patch.object(home, "sorted", forbidden, create=True):
            self.assertEqual(self.names(data), ["b", "d", "f", "a", "c", "e"])


class NoTiedSortsTests(unittest.TestCase):
    """Every sort in presto/ must be safe on a sort that is not stable."""

    # A sort may stay only when it cannot tie or when ties are identical:
    # cards.py sorts positions by a key that ends in the position (unique);
    # home.py sorts a list of integers, where equal values are the same value.
    # updater.py sorts unique dictionary keys for the canonical slot identity.
    ALLOWED = {("cards.py", "sorted"), ("home.py", "sort"), ("updater.py", "sorted")}

    def calls(self):
        folder = os.path.dirname(presto_path.__file__)
        found = set()
        presto = os.path.abspath(os.path.join(folder, "..", "..", "presto"))
        for name in sorted(os.listdir(presto)):
            if not name.endswith(".py"):
                continue
            if name.lower().startswith("config") or "secret" in name.lower():
                continue
            with open(os.path.join(presto, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if isinstance(function, ast.Name) and function.id == "sorted":
                    found.add((name, "sorted"))
                elif isinstance(function, ast.Attribute) and function.attr == "sort":
                    found.add((name, "sort"))
        return found

    def test_no_new_sort_has_appeared(self):
        unexpected = self.calls() - self.ALLOWED
        self.assertEqual(unexpected, set(),
                         "MicroPython's sort is not stable: a key that can tie scrambles "
                         "the order on the device. Partition with list comprehensions, or "
                         "make the key unique (include the position), then list it in ALLOWED.")


if __name__ == "__main__":
    unittest.main()
