"""The HTTP surface the UI actually calls."""

from app import db


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_status_reports_configuration(client):
    body = client.get("/api/status").json()

    assert body["ai"]["provider"] == "ollama"
    assert body["history"]["total_plays"] == 0
    assert body["scan"]["running"] is False
    assert body["settings"]["batch_size"] == 40
    assert {source["name"] for source in body["history"]["sources"]} == {"lastfm", "listenbrainz"}


def test_the_ui_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Musicdrome" in response.text


# ─── Suggestions ───────────────────────────────────────────────────────────


def test_suggestions_default_to_new(client, suggestion):
    suggestion("A", "New one", status="new")
    suggestion("B", "Hidden one", status="hidden")

    titles = [card["title"] for card in client.get("/api/suggestions").json()["suggestions"]]
    assert titles == ["New one"]


def test_suggestions_can_be_filtered_by_match(client, suggestion):
    suggestion("A", "High", match=95)
    suggestion("B", "Low", match=40)

    body = client.get("/api/suggestions?min_match=50").json()
    assert [card["title"] for card in body["suggestions"]] == ["High"]


def test_suggestions_can_be_sorted(client, suggestion):
    suggestion("Zed", "Low", match=10)
    suggestion("Alpha", "High", match=99)

    by_match = client.get("/api/suggestions?sort=match").json()["suggestions"]
    by_artist = client.get("/api/suggestions?sort=artist").json()["suggestions"]

    assert [card["artist"] for card in by_match] == ["Alpha", "Zed"]
    assert [card["artist"] for card in by_artist] == ["Alpha", "Zed"]


def test_tags_are_returned_as_a_list_with_counts(client, suggestion):
    suggestion("A", "One", tags="trip hop,downtempo")
    suggestion("B", "Two", tags="trip hop")

    body = client.get("/api/suggestions").json()
    assert body["suggestions"][0]["tags"] == ["trip hop", "downtempo"]
    assert {tag["name"]: tag["count"] for tag in body["tags"]} == {"trip hop": 2, "downtempo": 1}


def test_filtering_by_tag(client, suggestion):
    suggestion("A", "One", tags="trip hop")
    suggestion("B", "Two", tags="techno")

    body = client.get("/api/suggestions?tag=trip hop").json()
    assert [card["title"] for card in body["suggestions"]] == ["One"]


def test_hide_and_unhide(client, suggestion):
    suggestion_id = suggestion("A", "One")

    assert client.post(f"/api/suggestions/{suggestion_id}/hide").json()["status"] == "hidden"
    assert client.get("/api/suggestions").json()["suggestions"] == []
    assert client.post(f"/api/suggestions/{suggestion_id}/unhide").json()["status"] == "new"
    assert len(client.get("/api/suggestions").json()["suggestions"]) == 1


def test_save_and_unsave(client, suggestion):
    suggestion_id = suggestion("A", "One")

    assert client.post(f"/api/suggestions/{suggestion_id}/save").json()["status"] == "saved"
    assert client.post(f"/api/suggestions/{suggestion_id}/unsave").json()["status"] == "new"


def test_an_unknown_action_is_rejected(client, suggestion):
    suggestion_id = suggestion("A", "One")
    assert client.post(f"/api/suggestions/{suggestion_id}/explode").status_code == 400


def test_acting_on_a_missing_suggestion_is_a_404(client):
    assert client.post("/api/suggestions/999/save").status_code == 404


def test_download_queues_the_track(client, suggestion):
    suggestion_id = suggestion("A", "One")
    body = client.post(f"/api/suggestions/{suggestion_id}/download").json()

    assert body["queued"] is True
    assert client.get("/api/downloads").json()["downloads"][0]["title"] == "One"


def test_download_all_respects_the_minimum(client, suggestion):
    suggestion("A", "High", match=95)
    suggestion("B", "Low", match=20)

    body = client.post("/api/suggestions/download-all", json={"min_match": 50}).json()
    assert body["queued"] == 1


# ─── Downloads ─────────────────────────────────────────────────────────────


def test_a_download_carries_what_it_was_served(client, suggestion):
    """So "no re-encode" is checkable per track rather than taken on trust."""
    from app import db

    suggestion_id = suggestion("A", "One")
    client.post(f"/api/suggestions/{suggestion_id}/download")
    row = client.get("/api/downloads").json()["downloads"][0]

    # Queued but not yet fetched: nothing is known, and nothing is claimed.
    assert (row["source_codec"], row["source_abr"], row["encoded"]) == ("", 0, "")

    with db.connect() as conn:
        conn.execute(
            "UPDATE downloads SET source_codec = 'opus', source_abr = 160, encoded = 'copied' "
            "WHERE id = ?",
            (row["id"],),
        )

    row = client.get("/api/downloads").json()["downloads"][0]
    assert (row["source_codec"], row["source_abr"], row["encoded"]) == ("opus", 160, "copied")


def test_downloads_can_be_filtered_and_removed(client, suggestion):
    suggestion_id = suggestion("A", "One")
    client.post(f"/api/suggestions/{suggestion_id}/download")
    download_id = client.get("/api/downloads").json()["downloads"][0]["id"]

    assert client.get("/api/downloads?status=done").json()["downloads"] == []
    assert client.delete(f"/api/downloads/{download_id}").json()["deleted"] is True
    assert client.get("/api/downloads").json()["downloads"] == []


def test_removing_a_download_returns_the_card_to_the_grid(client, suggestion):
    suggestion_id = suggestion("A", "One")
    client.post(f"/api/suggestions/{suggestion_id}/download")
    download_id = client.get("/api/downloads").json()["downloads"][0]["id"]

    client.delete(f"/api/downloads/{download_id}")
    assert client.get("/api/suggestions").json()["suggestions"][0]["status"] == "new"


def test_retrying_something_that_is_not_failed_is_a_404(client, suggestion):
    suggestion_id = suggestion("A", "One")
    client.post(f"/api/suggestions/{suggestion_id}/download")
    download_id = client.get("/api/downloads").json()["downloads"][0]["id"]

    assert client.post(f"/api/downloads/{download_id}/retry").status_code == 404


def test_active_downloads_endpoint(client):
    assert client.get("/api/downloads/active").json() == {"active": []}


# ─── Settings ──────────────────────────────────────────────────────────────


def test_settings_round_trip(client):
    body = client.put("/api/settings", json={"batch_size": 25, "schedule": "weekly"}).json()

    assert body["settings"]["batch_size"] == 25
    assert body["settings"]["schedule"] == "weekly"
    assert client.get("/api/settings").json()["settings"]["batch_size"] == 25


def test_out_of_range_settings_are_clamped(client):
    body = client.put("/api/settings", json={"batch_size": 5000, "min_match": -20}).json()

    assert body["settings"]["batch_size"] == 100
    assert body["settings"]["min_match"] == 0


def test_an_invalid_schedule_falls_back(client):
    body = client.put("/api/settings", json={"schedule": "hourly-ish"}).json()
    assert body["settings"]["schedule"] == "daily"


def test_unknown_settings_are_ignored(client):
    body = client.put("/api/settings", json={"delete_everything": True}).json()
    assert "delete_everything" not in body["settings"]


def test_booleans_accept_the_strings_a_form_sends(client):
    assert client.put("/api/settings", json={"auto_download": "true"}).json()["settings"]["auto_download"] is True
    assert client.put("/api/settings", json={"auto_download": "false"}).json()["settings"]["auto_download"] is False


# ─── Stats ─────────────────────────────────────────────────────────────────


def test_stats_on_an_empty_database(client):
    body = client.get("/api/stats").json()
    assert body["plays"] == 0
    assert len(body["clock"]) == 24


def test_stats_count_plays(client, play):
    play("Radiohead", "Karma Police")
    play("Radiohead", "No Surprises")
    play("Portishead", "Glory Box")

    body = client.get("/api/stats?days=30").json()
    assert body["plays"] == 3
    assert body["artists"] == 2
    assert body["top_artists"][0] == {"artist": "Radiohead", "plays": 2}


def test_new_versus_familiar(client, play):
    play("Old", "Song", at=db.now() - 300 * 86400)
    play("Old", "Song", at=db.now() - 86400)
    play("Fresh", "Song", at=db.now() - 86400)

    body = client.get("/api/stats?days=30").json()
    assert body["new_plays"] == 1
    assert body["familiar_plays"] == 1


def test_the_taste_summary_is_skippable(client):
    client.put("/api/settings", json={"taste_summary": False})
    assert client.get("/api/stats/summary").json()["enabled"] is False


def test_the_taste_summary_reports_a_thin_history(client, play):
    play("A", "One")
    body = client.get("/api/stats/summary").json()
    assert body["text"] == ""
    assert body["error"]


# ─── Scanning ──────────────────────────────────────────────────────────────


def test_scan_state_is_reported(client):
    body = client.get("/api/scan").json()
    assert body["state"]["running"] is False
    assert body["recent"] == []
