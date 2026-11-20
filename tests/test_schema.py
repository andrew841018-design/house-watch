"""repo 裡的 JSON Schema 必須和匯出模型產生的一模一樣（M0 驗收）。"""

from pathlib import Path

from house_watch.models import export_json_schema_text

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "schemas" / "export.schema.json"


def test_schema_file_matches_models():
    expected = export_json_schema_text()
    actual = SCHEMA_FILE.read_text(encoding="utf-8")
    assert actual == expected, (
        "schemas/export.schema.json 已過期，請執行："
        "uv run python -m house_watch.models > schemas/export.schema.json"
    )
