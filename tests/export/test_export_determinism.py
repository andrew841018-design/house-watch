"""匯出的確定性（r4 §2.2 #7）：不同 PYTHONHASHSEED 的兩個程序，輸出逐位元組相同。"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from hw_export_samples import fill_db, make_web, new_data, tree_bytes, write_settings

HERE = Path(__file__).resolve().parent
SEEDS = ("0", "4242", "77", "31337")


def test_hash_seeds_do_not_change_output(tmp_path):
    settings_path = write_settings(tmp_path / "settings.toml")
    web = make_web(tmp_path / "web")
    _, conn = new_data(tmp_path / "base")
    fill_db(conn)
    conn.close()

    outputs: dict[str, dict[str, bytes]] = {}
    raw_orders: set[str] = set()
    for seed in SEEDS:
        root = tmp_path / f"seed_{seed}"
        shutil.copytree(tmp_path / "base", root)
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run(
            [
                sys.executable,
                str(HERE / "hw_export_samples.py"),
                str(root),
                str(settings_path),
                str(web),
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        raw_orders.add(proc.stdout.strip().splitlines()[-1])
        outputs[seed] = tree_bytes(root / "export")

    # 確認 provider 給的順序真的會隨 hash seed 改變，否則這個測試沒有測到排序
    assert len(raw_orders) > 1, [json.loads(order) for order in raw_orders]
    first = outputs[SEEDS[0]]
    assert first, "沒有產生任何檔案"
    for seed in SEEDS[1:]:
        assert outputs[seed] == first, f"PYTHONHASHSEED={seed} 的輸出不同"
