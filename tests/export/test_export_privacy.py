"""匯出不受本機運作設定影響（R2-02、R3-15）。

本機設定只有兩項會改變輸出，而且是刻意的：enabled_areas（匯出哪些區域）與
各來源的 enabled／publish（哪些來源出現在網站上）。其他本機值（聯絡方式、發布設定、
資料夾、請求速率）不論怎麼寫，網站都逐位元組相同，也不會出現在輸出裡。
"""

from hw_export_samples import (
    BOTH_AREAS,
    NOW,
    SAMPLE_PROVIDERS,
    fill_db,
    make_web,
    new_data,
    settings_for,
    tree_bytes,
    write_settings,
)

from house_watch.clock import FakeClock
from house_watch.export import export_dir, export_site

OWNER_OPS = """
data_dir = "/Users/someone-else/private/runtime"

[publish]
site_repo_dir = "/Users/someone-else/private/site-clone"
remote = "git@example.invalid:someone-else/site.git"
deploy_key = "/Users/someone-else/.ssh/hw_deploy_key"
known_hosts = "/Users/someone-else/.ssh/known_hosts"

[http]
contact = "someone-else@example.invalid"

[sources.cthouse]
min_interval_s = 9
max_requests_full = 77
probe_pages = 3
detail_cap = 4
"""


def _export_with(tmp_path, name: str, settings_text: str, web):
    data, conn = new_data(tmp_path / name)
    try:
        fill_db(conn)
        settings = settings_for(write_settings(tmp_path / f"{name}.toml", settings_text))
        export_site(
            conn,
            settings,
            data=data,
            clock=FakeClock(NOW),
            providers=SAMPLE_PROVIDERS,
            web_root=web,
        )
    finally:
        conn.close()
    return tree_bytes(export_dir(data))


def test_export_ignores_owner_ops_settings(tmp_path):
    web = make_web(tmp_path / "web")
    plain = _export_with(tmp_path, "plain", BOTH_AREAS, web)
    busy = _export_with(tmp_path, "busy", BOTH_AREAS + OWNER_OPS, web)
    assert plain == busy
    blob = b"\n".join(busy.values())
    for secret in ("someone-else", "example.invalid", "hw_deploy_key", "known_hosts"):
        assert secret.encode() not in blob


def test_area_selection_is_the_intended_difference(tmp_path):
    web = make_web(tmp_path / "web")
    one = _export_with(tmp_path, "one", "", web)  # 沒寫 enabled_areas：只啟用第一個區域
    assert "site/data/listings_taoyuan_core.json" in one
    assert not any("zhongli_ic" in path for path in one)
