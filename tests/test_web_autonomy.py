"""The autonomy dashboard page."""

from __future__ import annotations


def test_page_lists_every_action(web_client):
    response = web_client.get("/autonomy")
    assert response.status_code == 200
    for action in ("email_send", "task_create", "wrike_write", "note_write"):
        assert action in response.text


def test_post_sets_a_level_and_redirects(web_client):
    response = web_client.post("/autonomy/note_write", data={"level": "act"},
                               follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/autonomy"

    page = web_client.get("/autonomy").text
    note_row = page.split("note_write", 1)[1][:400]
    assert "act" in note_row


def test_email_send_shows_that_it_is_capped(web_client):
    web_client.post("/autonomy/email_send", data={"level": "act"})
    page = web_client.get("/autonomy").text.lower()
    assert "ceiling" in page or "capped" in page


def test_invalid_level_is_rejected_without_500(web_client):
    response = web_client.post("/autonomy/note_write", data={"level": "banana"},
                               follow_redirects=False)
    assert response.status_code == 303
    page = web_client.get("/autonomy").text
    note_row = page.split("note_write", 1)[1][:400]
    assert "approve" in note_row


def test_unknown_action_does_not_500(web_client):
    response = web_client.post("/autonomy/not_an_action", data={"level": "act"},
                               follow_redirects=False)
    assert response.status_code == 303
