import os

from config import load_local_env_file


def test_local_env_loader_does_not_override_existing_variables(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("FROM_FILE=hello\nKEEP=value-from-file\nQUOTED='two words'\n", encoding="utf-8")
    monkeypatch.setenv("KEEP", "already-set")

    load_local_env_file(path)

    assert os.environ["FROM_FILE"] == "hello"
    assert os.environ["KEEP"] == "already-set"
    assert os.environ["QUOTED"] == "two words"
