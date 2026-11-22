import pytest

import database_manager as db


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)


def test_new_secret_key_preferred(monkeypatch):
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_new")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJlegacy")
    assert db.resolve_backend_key() == "sb_secret_new"


def test_legacy_key_still_works(monkeypatch):
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJlegacy")
    assert db.resolve_backend_key() == "eyJlegacy"


def test_empty_new_secret_falls_back(monkeypatch):
    # GitHub passes an empty string when a secret isn't defined
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJlegacy")
    assert db.resolve_backend_key() == "eyJlegacy"


def test_publishable_key_rejected(monkeypatch):
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_publishable_oops")
    with pytest.raises(db.DatabaseUnavailable):
        db.resolve_backend_key()


def test_new_key_accepted_by_supabase_client():
    from supabase import create_client
    c = create_client("https://example.supabase.co", "sb_secret_abc123")
    assert c.options.headers["apiKey"] == "sb_secret_abc123"


@pytest.mark.parametrize("raw", ["https://abc.supabase.co", "https://abc.supabase.co/",
                                 "https://abc.supabase.co/rest/v1", "https://abc.supabase.co/rest/v1/"])
def test_url_normalised(monkeypatch, raw):
    monkeypatch.setenv("SUPABASE_URL", raw)
    assert db.resolve_url() == "https://abc.supabase.co"
