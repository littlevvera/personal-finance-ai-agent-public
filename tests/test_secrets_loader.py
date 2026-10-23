import secrets_loader


def test_missing_dotenv_and_secrets_returns_none(monkeypatch):
    monkeypatch.setattr(secrets_loader, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(secrets_loader, "_streamlit_secret_key", lambda: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert secrets_loader.get_openai_api_key() is None


def test_missing_key_message_does_not_include_a_secret():
    assert "sk-" not in secrets_loader.MISSING_KEY_MESSAGE.lower()
    assert "OPENAI_API_KEY" in secrets_loader.MISSING_KEY_MESSAGE


def test_streamlit_secrets_error_is_swallowed(monkeypatch):
    monkeypatch.setattr(secrets_loader, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def boom():
        raise FileNotFoundError("secrets.toml")

    monkeypatch.setattr(secrets_loader, "_streamlit_secret_key", boom)
    assert secrets_loader.get_openai_api_key() is None
