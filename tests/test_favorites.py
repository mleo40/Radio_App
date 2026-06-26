"""Unit tests for the Favorites matcher."""

from datetime import UTC, datetime, timedelta

from radio_app.core.favorites import Favorites


def test_callsign_match_is_case_insensitive():
    favs = Favorites()
    favs.add("KD2ABC", "Bob")
    assert favs.is_favorite("kd2abc")
    assert favs.is_favorite("KD2ABC")
    assert not favs.is_favorite("KD2XYZ")


def test_hex_hash_prefix_match_both_directions():
    full = "abc123def456789a0b1c2d3e4f5a6b7c8"
    favs = Favorites()
    favs.add(full, "lab-node")
    # Announce carried a 12-char short hash.
    short = full[:12]
    fav = favs.match(short)
    assert fav is not None
    assert fav.label == "lab-node"
    # And the inverse direction (stored short, announce full).
    favs2 = Favorites()
    favs2.add(short)
    assert favs2.match(full) is not None


def test_hex_does_not_match_callsign():
    favs = Favorites()
    favs.add("KD2ABC")
    assert favs.match("abc123def456") is None


def test_add_updates_label_when_re_added():
    favs = Favorites()
    favs.add("KD2ABC")
    favs.add("kd2abc", "Bob")
    assert favs.match("KD2ABC").label == "Bob"
    assert len(favs.all()) == 1


def test_remove_returns_false_when_missing():
    favs = Favorites()
    assert favs.remove("KD2ABC") is False
    favs.add("KD2ABC")
    assert favs.remove("KD2ABC") is True
    assert favs.all() == []


def test_set_label_creates_entry_for_new_id():
    favs = Favorites()
    fav = favs.set_label("abc123def456789a", "Alice")
    assert fav is not None
    assert fav.label == "Alice"
    assert favs.match("abc123def456789a").display == "Alice"


def test_set_label_updates_existing_via_prefix_match():
    full = "abc123def456789a0b1c2d3e4f5a6b7c8"
    favs = Favorites()
    favs.add(full)
    favs.set_label(full[:12], "lab-node")  # name via the short hash
    assert favs.match(full).label == "lab-node"
    assert len(favs.all()) == 1


def test_set_label_clear_with_empty_string():
    favs = Favorites()
    favs.add("KD2ABC", "Bob")
    favs.set_label("kd2abc", "")
    assert favs.match("KD2ABC").label == ""


def test_set_label_clear_missing_is_noop():
    favs = Favorites()
    assert favs.set_label("KD2ABC", "") is None
    assert favs.all() == []


# -- note_sighting decision seam ---------------------------------------------


def test_note_sighting_unknown_returns_none():
    favs = Favorites()
    favs.add("KD2ABC")
    assert favs.note_sighting("W1AW") is None


def test_note_sighting_first_time_is_back_online():
    favs = Favorites()
    favs.add("KD2ABC")
    s = favs.note_sighting("kd2abc", when=datetime(2026, 1, 1, tzinfo=UTC))
    assert s is not None
    assert s.is_back_online is True
    assert s.favorite.id == "KD2ABC"


def test_note_sighting_recent_repeat_is_not_alert():
    favs = Favorites()
    favs.add("KD2ABC")
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    favs.note_sighting("KD2ABC", when=t0)
    s2 = favs.note_sighting("KD2ABC", when=t0 + timedelta(seconds=30))
    assert s2.is_back_online is False


def test_note_sighting_after_quiet_period_alerts_again():
    favs = Favorites()
    favs.add("KD2ABC")
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    favs.note_sighting("KD2ABC", when=t0, quiet_seconds=600)
    s2 = favs.note_sighting(
        "KD2ABC", when=t0 + timedelta(seconds=601), quiet_seconds=600
    )
    assert s2.is_back_online is True


def test_note_sighting_matches_via_display_name():
    favs = Favorites()
    favs.add("lab-node")
    s = favs.note_sighting("abc123def456", display_name="lab-node")
    assert s is not None
    assert s.favorite.id == "lab-node"


def test_note_sighting_updates_last_seen_and_dirty():
    favs = Favorites()
    favs.add("KD2ABC")
    favs._dirty = False  # add() sets it; reset to test note_sighting
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    favs.note_sighting("KD2ABC", when=t0)
    assert favs.match("KD2ABC").last_seen == t0
    assert favs.dirty is True


# -- callsign case-insensitivity (last seen) ---------------------------------


def test_last_seen_updates_when_sighting_case_differs():
    # Stored one case, heard the other: last_seen must still update.
    favs = Favorites()
    favs.add("kd2abc")  # stored lowercase
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    s = favs.note_sighting("KD2ABC", when=t0)  # heard uppercase
    assert s is not None
    assert favs.match("kd2abc").last_seen == t0
    # ...and the reverse direction (stored upper, heard lower).
    favs2 = Favorites()
    favs2.add("W1AW")
    s2 = favs2.note_sighting("w1aw", when=t0)
    assert s2 is not None
    assert favs2.match("W1AW").last_seen == t0


def test_add_same_callsign_different_case_is_one_favorite():
    favs = Favorites()
    favs.add("KD2ABC")
    favs.add("kd2abc", "Bob")  # same callsign, different case
    assert len(favs.all()) == 1
    assert favs.match("Kd2Abc").label == "Bob"


# -- persistence round-trip ---------------------------------------------------


class _FakeConfig:
    """Minimal Config stand-in for save/load round-trips."""

    def __init__(self):
        self.data = {}
        self.saved = 0

    def save(self):
        self.saved += 1


def test_save_and_reload_preserves_last_seen():
    cfg = _FakeConfig()
    favs = Favorites()
    favs.add("KD2ABC", "Bob")
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    favs.note_sighting("KD2ABC", when=t0)
    favs.save(cfg)
    assert cfg.saved == 1
    assert favs.dirty is False

    reloaded = Favorites.from_config(cfg)
    fav = reloaded.match("KD2ABC")
    assert fav is not None
    assert fav.label == "Bob"
    assert fav.last_seen == t0


# -- contact metadata (name / gridsquare / power / notes / custom) -----------


def test_add_with_meta_stored_and_displayed():
    favs = Favorites()
    fav = favs.add(
        "KD2ABC",
        meta={"name": "Bob", "gridsquare": "FN31pr", "power": "5W"},
    )
    assert fav.meta["gridsquare"] == "FN31pr"
    assert fav.meta["power"] == "5W"
    # A name takes precedence over label/id in display.
    assert fav.display == "Bob"


def test_set_meta_merges_and_marks_dirty():
    favs = Favorites()
    favs.add("KD2ABC")
    favs._dirty = False
    fav = favs.set_meta("kd2abc", {"gridsquare": "FN31"})
    assert fav is not None
    assert fav.meta == {"gridsquare": "FN31"}
    assert favs.dirty is True
    # Merging keeps existing keys and adds new ones.
    favs.set_meta("KD2ABC", {"power": "10W"})
    assert favs.match("KD2ABC").meta == {"gridsquare": "FN31", "power": "10W"}


def test_set_meta_empty_value_deletes_key():
    favs = Favorites()
    favs.add("KD2ABC", meta={"gridsquare": "FN31", "power": "5W"})
    favs.set_meta("KD2ABC", {"power": ""})
    assert favs.match("KD2ABC").meta == {"gridsquare": "FN31"}


def test_set_meta_creates_entry_for_new_id():
    favs = Favorites()
    fav = favs.set_meta("W1AW", {"name": "Hiram"})
    assert fav is not None
    assert favs.match("W1AW").meta["name"] == "Hiram"


def test_set_meta_all_empty_for_missing_is_noop():
    favs = Favorites()
    assert favs.set_meta("W1AW", {"name": ""}) is None
    assert favs.all() == []


def test_meta_ignores_reserved_keys():
    favs = Favorites()
    fav = favs.add("KD2ABC", meta={"id": "spoof", "name": "Bob"})
    assert "id" not in fav.meta
    assert fav.id == "KD2ABC"
    assert fav.meta["name"] == "Bob"


def test_save_and_reload_preserves_meta():
    cfg = _FakeConfig()
    favs = Favorites()
    favs.add("KD2ABC", "Bob", meta={"gridsquare": "FN31pr", "power": "5W"})
    favs.save(cfg)

    reloaded = Favorites.from_config(cfg)
    fav = reloaded.match("KD2ABC")
    assert fav is not None
    assert fav.meta == {"gridsquare": "FN31pr", "power": "5W"}


def test_from_config_tolerates_flat_legacy_meta_keys():
    # Hand-edited config with metadata as flat top-level keys.
    cfg = _FakeConfig()
    cfg.data = {
        "favorites": [
            {"id": "KD2ABC", "label": "Bob", "gridsquare": "FN31", "power": "5W"}
        ]
    }
    favs = Favorites.from_config(cfg)
    fav = favs.match("KD2ABC")
    assert fav is not None
    assert fav.meta == {"gridsquare": "FN31", "power": "5W"}




