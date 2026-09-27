"""HTTP API: удаление шаблона — полностью и без возврата после перезапуска сервиса."""
from fastapi.testclient import TestClient

from decksmith.api import app as api
from decksmith.parsing.template_parser import analyze_template
from decksmith.testing.synthetic import GENERATORS


def _upload(client: TestClient, path) -> str:
    """Загружает шаблон через API и возвращает его id."""
    with open(path, "rb") as f:
        r = client.post("/api/templates", files={"file": (path.name, f)})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_template_delete_is_complete_and_persistent(tmp_path):
    """Удалённый шаблон пропадает из списка, из кэша разбора и загрузок и не возвращается при перезапуске,
    пока его не загрузят снова.
    """
    client = TestClient(api.app)  # без контекстного менеджера: фоновый разбор датасета при старте не нужен
    src = GENERATORS["dark_minimal"](tmp_path / "удаляемый_шаблон.pptx")
    tid = _upload(client, src)
    assert tid in [t["id"] for t in client.get("/api/templates").json()]
    assert (api.WS / "templates" / tid / "profile.json").exists()

    # шаблон в идущей генерации удалить нельзя
    api.RUNS["busy"] = {"id": "busy", "status": "running", "request": {"template_id": tid}}
    try:
        assert client.delete(f"/api/templates/{tid}").status_code == 409
    finally:
        api.RUNS.pop("busy")

    r = client.delete(f"/api/templates/{tid}")
    assert r.status_code == 200, r.text
    assert r.json()["uploads_removed"] >= 1
    assert tid not in [t["id"] for t in client.get("/api/templates").json()]
    assert client.get(f"/api/templates/{tid}").status_code == 404
    assert not (api.WS / "templates" / tid).exists()
    assert tid in api._deleted()

    # «перезапуск»: даже если разбор того же файла снова создал кэш (CLI, датасет), шаблон не возвращается
    analyze_template(src)
    api.TEMPLATES.clear()
    api._load_existing()
    assert tid not in api.TEMPLATES

    # явная повторная загрузка возвращает шаблон
    assert _upload(client, src) == tid
    assert tid not in api._deleted()
    assert client.delete(f"/api/templates/{tid}").status_code == 200

    # чужой или несуществующий id
    assert client.delete("/api/templates/not-a-template").status_code == 404
    assert client.delete("/api/templates/0123456789ab").status_code == 404
    api._save_deleted(api._deleted() - {tid})  # тестовый каталог остаётся чистым
