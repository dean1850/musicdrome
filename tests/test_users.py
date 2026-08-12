"""Multi-user: separate taste, one shared library.

The tests that matter here are the isolation ones. A household sharing a
Musicdrome must never see one person's decisions applied to another's grid, and
that is exactly the kind of bug a per-user filter left off one query produces —
invisible in single-user testing, obvious and unfixable-looking in a house.
"""

import pytest

from app import db, download, history, stats, users
from app.norm import track_key


# ─── The user list ─────────────────────────────────────────────────────────


def test_a_first_user_is_created_from_the_environment():
    """An upgrade must not land on an empty user list and stop working."""
    assert users.default_id() is not None
    assert len(users.all_users()) == 1


def test_users_are_created_and_listed(make_user):
    make_user("alex", lastfm_user="alex_fm")
    make_user("sam", listenbrainz_user="sam_lb")

    names = [user["name"] for user in users.all_users()]
    assert "alex" in names and "sam" in names


def test_duplicate_names_are_refused(make_user):
    make_user("alex")
    with pytest.raises(users.UserError, match="already a user"):
        make_user("alex")


def test_a_user_needs_a_name():
    with pytest.raises(users.UserError, match="needs a name"):
        users.create(name="   ")


def test_the_listenbrainz_token_never_leaves_in_a_user_payload(make_user):
    user_id = make_user("alex", listenbrainz_token="secret-token")

    payload = users.get(user_id)
    assert "listenbrainz_token" not in payload
    assert payload["has_listenbrainz_token"] is True
    # It is still readable by the code that actually needs it.
    assert users.credentials(user_id)["listenbrainz_token"] == "secret-token"


def test_a_blank_token_leaves_the_stored_one_alone(make_user):
    """Otherwise saving the settings form would wipe a token every time."""
    user_id = make_user("alex", listenbrainz_token="secret-token")
    users.update(user_id, {"listenbrainz_token": "", "lastfm_user": "alex_fm"})

    assert users.credentials(user_id)["listenbrainz_token"] == "secret-token"
    assert users.get(user_id)["lastfm_user"] == "alex_fm"


def test_resolve_falls_back_rather_than_failing(make_user):
    """A stale id in a bookmark must not break the whole page."""
    assert users.resolve(9999) == users.default_id()
    assert users.resolve(None) == users.default_id()


def test_deactivated_users_are_skipped_by_scheduled_scans(make_user):
    user_id = make_user("alex")
    users.update(user_id, {"active": False})

    assert user_id not in [user["id"] for user in users.active_users()]
    assert user_id in [user["id"] for user in users.all_users()]


# ─── Isolation ─────────────────────────────────────────────────────────────


def test_two_users_can_be_suggested_the_same_track(make_user, suggestion, default_user):
    """The old schema made track_key globally unique, which silently gave the
    second user's card to the first."""
    alex = make_user("alex")
    suggestion("Portishead", "Glory Box", user_id=default_user)
    suggestion("Portishead", "Glory Box", user_id=alex)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM suggestions WHERE track_key = ?",
            (track_key("Portishead", "Glory Box"),),
        ).fetchall()
    assert {row["user_id"] for row in rows} == {default_user, alex}


def test_hiding_a_card_does_not_hide_it_for_everyone(client, make_user, suggestion, default_user):
    alex = make_user("alex")
    mine = suggestion("Portishead", "Glory Box", user_id=default_user)
    theirs = suggestion("Portishead", "Glory Box", user_id=alex)

    assert client.post(f"/api/suggestions/{mine}/hide").status_code == 200

    with db.connect() as conn:
        still_new = conn.execute(
            "SELECT status FROM suggestions WHERE id = ?", (theirs,)
        ).fetchone()
    assert still_new["status"] == "new"


def test_the_grid_only_shows_the_selected_user(client, make_user, suggestion, default_user):
    alex = make_user("alex")
    suggestion("Portishead", "Glory Box", user_id=default_user)
    suggestion("Slowdive", "Alison", user_id=alex)

    body = client.get(f"/api/suggestions?user_id={alex}").json()
    titles = [card["title"] for card in body["suggestions"]]
    assert titles == ["Alison"]


def test_two_users_playing_the_same_track_at_once_are_both_recorded(
    make_user, play, default_user
):
    """The plays UNIQUE constraint has to include the user, or one is dropped."""
    alex = make_user("alex")
    play("Radiohead", "Karma Police", at=1_700_000_000, user_id=default_user)
    play("Radiohead", "Karma Police", at=1_700_000_000, user_id=alex)

    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"]
    assert count == 2


def test_a_profile_only_sees_its_own_plays(make_user, play, default_user):
    alex = make_user("alex")
    play("Radiohead", "Karma Police", user_id=default_user)
    play("Slowdive", "Alison", user_id=alex)

    mine = history.profile(days=90, user_id=default_user)
    theirs = history.profile(days=90, user_id=alex)

    assert [a["artist"] for a in mine["top_artists"]] == ["Radiohead"]
    assert [a["artist"] for a in theirs["top_artists"]] == ["Slowdive"]
    # Without a user, the household total.
    assert history.profile(days=90)["plays"] == 2


def test_stats_are_scoped_to_the_user(make_user, play, default_user):
    alex = make_user("alex")
    play("Radiohead", "Karma Police", user_id=default_user)
    play("Slowdive", "Alison", user_id=alex)
    play("Slowdive", "Souvlaki Space Station", user_id=alex)

    assert stats.overview(90, user_id=default_user)["plays"] == 1
    assert stats.overview(90, user_id=alex)["plays"] == 2
    assert stats.overview(90)["plays"] == 3


def test_sync_cursors_are_per_user(make_user, default_user):
    """One person's broken Last.fm must not stall anybody else's sync."""
    alex = make_user("alex")
    with db.connect() as conn:
        history._save_cursor(conn, default_user, "lastfm", 100)
        history._save_cursor(conn, alex, "lastfm", 500, error="rate limited")

    with db.connect() as conn:
        assert history._cursor(conn, default_user, "lastfm") == 100
        assert history._cursor(conn, alex, "lastfm") == 500


def test_download_all_does_not_reach_into_another_users_grid(
    client, monkeypatch, make_user, suggestion, default_user
):
    queued = []
    monkeypatch.setattr(download, "enqueue", lambda suggestion_id: queued.append(suggestion_id))

    alex = make_user("alex")
    suggestion("Portishead", "Glory Box", user_id=default_user)
    theirs = suggestion("Slowdive", "Alison", user_id=alex)

    client.post("/api/suggestions/download-all", json={"min_match": 0, "user_id": alex})
    assert queued == [theirs]


def test_deleting_a_user_takes_their_history_but_leaves_the_files(
    make_user, play, suggestion, default_user
):
    """The library is shared, so download rows outlive the user who asked."""
    alex = make_user("alex")
    play("Slowdive", "Alison", user_id=alex)
    suggestion_id = suggestion("Slowdive", "Alison", user_id=alex)
    download_id = download.enqueue(suggestion_id)

    assert users.delete(alex) is True

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM suggestions").fetchone()["n"] == 0
        row = conn.execute("SELECT user_id FROM downloads WHERE id = ?", (download_id,)).fetchone()
    assert row is not None, "the download row must survive its requester"
    assert row["user_id"] is None


# ─── Roster import ─────────────────────────────────────────────────────────


def test_importing_a_roster_adds_the_new_people(make_user):
    make_user("alex")
    result = users.import_roster([{"name": "alex"}, {"name": "sam", "email": "sam@home"}])

    assert result["added"] == ["sam"]
    assert result["skipped"] == ["alex"]


def test_reimporting_never_overwrites_configured_scrobble_names(make_user):
    """Re-importing picks up new housemates; it does not undo configuration."""
    alex = make_user("alex", lastfm_user="alex_fm")
    users.import_roster([{"name": "alex", "email": "different@home"}])

    assert users.get(alex)["lastfm_user"] == "alex_fm"


def test_roster_entries_without_a_name_are_ignored():
    assert users.import_roster([{"name": ""}, {"email": "x@y"}])["added"] == []
