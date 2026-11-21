"""門牌索引：解析、建索引、檢查、比對（doorindex.py）。全部離線，用合成的 CSV。"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from geo_city import (
    BADE,
    FIXTURES,
    MIN_ROWS,
    NOW,
    P1,
    TAOYUAN,
    Z1,
    ZHONGLI,
    Door,
    csv_text,
)

from house_watch.errors import ErrorCode
from house_watch.geo.doorindex import (
    HEADER,
    DoorIndex,
    DoorIndexError,
    HouseNumber,
    build_index,
    cn2int,
    district_code,
    lane_key,
    norm,
    parse_number_column,
    read_info,
    split_prefix,
    split_section,
)

# ---------------------------------------------------------------- 正規化與解析


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("１４９號八樓", ("", 149, 0, 8)),
        ("２６９號五樓之１", ("", 269, 0, 5)),
        ("３７５號地下室貳層", ("", 375, 0, None)),
        ("１７５之２號", ("", 175, 2, 1)),
        ("８號之１", ("", 8, 1, 1)),
        ("１２號之１五樓", ("", 12, 1, 5)),
        ("５衖１３號二樓", ("5", 13, 0, 2)),
        ("４９７之９９９號（為統計使用）", ("", 497, 999, 1)),
        ("１６號(臨)", ("", 16, 0, 1)),
        ("２號十七樓", ("", 2, 0, 17)),
        ("沒有號碼", None),
    ],
)
def test_parse_number_column(text, expected):
    assert parse_number_column(text) == expected


def test_normalisation_helpers():
    assert norm("厦門街　１２號") == "廈門街12號"
    assert norm("中寧街") == "中寧街"  # CJK 相容字經 NFKC 變一般字
    assert cn2int("二十三") == 23 and cn2int("十") == 10 and cn2int("一0五") == 105
    assert lane_key("１２１之１０巷") == "121之10"
    assert lane_key("") == ""
    assert split_section("大興西路二段") == ("大興西路", "二段")
    assert split_section("中正路") == ("中正路", "")
    assert district_code("中壢區") == district_code("中壢") == ZHONGLI


@pytest.mark.parametrize(
    ("text", "districts", "rest"),
    [
        ("桃園市中壢區桃園區莊敬路", ["中壢區", "桃園區"], "莊敬路"),
        ("桃園縣桃園市桃園市中正路1號", ["桃園區"], "中正路1號"),
        ("桃園市中正路", [], "中正路"),
        ("臺灣省桃園縣中壢市民族路二段", ["中壢區"], "民族路二段"),
        ("八德區中華路", ["八德區"], "中華路"),
    ],
)
def test_split_prefix_keeps_every_district(text, districts, rest):
    assert split_prefix(text) == (districts, rest)


# ---------------------------------------------------------------- 建索引


@pytest.fixture
def formats_index(tmp_path):
    path = tmp_path / "formats.sqlite"
    build_index(FIXTURES / "doors_formats.csv", path, version="11412", now=NOW, min_rows=MIN_ROWS)
    with DoorIndex.open(path) as index:
        yield index


def test_build_aggregates_floors_and_formats(formats_index):
    idx = formats_index
    assert idx.version == "11412"
    assert idx.info.rows == 17 and idx.info.doors == 11
    one = idx.lookup("桃園區", "示範路一段", None, HouseNumber(num=1))
    assert one is not None and one.level == "exact"
    with closing(sqlite3.connect(idx.path)) as conn:
        rows = dict(
            ((r[0], r[1]), r[2:])
            for r in conn.execute(
                "SELECT num, sub, n_rows, max_floor, spread_m FROM doors JOIN roads USING (road_id) "
                "WHERE name = '示範路一段' AND lane = ''"
            )
        )
    assert rows[(1, 0)][:2] == (4, 9)  # 1、2、9 樓與地下室；地下室不算最高樓層
    assert rows[(5, 2)][:2] == (2, 7)  # 「５號之２」是之號
    assert rows[(3, 1)][:2] == (1, 1)


def test_build_handles_lane_alley_area_and_variants(formats_index):
    idx = formats_index
    lane = idx.lookup("桃園區", "示範路一段", None, HouseNumber(lane="121之10", num=4))
    assert lane is not None and lane.level == "exact"  # 「(臨)」註記被拿掉
    alley = idx.lookup("中壢區", "示範路", None, HouseNumber(lane="8", alley="5", num=13))
    assert alley is not None and alley.level == "exact"  # 「５衖１３號」的衖是弄
    assert idx.geocode("桃園區", "廈門街9號").level == "exact"  # 門牌檔寫「厦」
    assert idx.geocode("桃園區", "示範新村11號").level == "exact"  # 只有「地區」欄的列


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "doors.csv"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _door_lines(count: int, code: str = TAOYUAN) -> list[Door]:
    return [Door(code, "示範路", n, Z1, dx=n) for n in range(1, count + 1)]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda t: t.replace("縱座標", "Y", 1), "表頭"),
        (lambda t: t.replace(f"{Z1[0] + 3:.3f}", "1.000", 1), "坐標"),
        (lambda t: t.replace("３號", "沒有號碼", 1), "解析不了"),
    ],
)
def test_validation_failures(tmp_path, mutate, message):
    text = mutate(csv_text(_door_lines(20) + _door_lines(5, ZHONGLI)))
    out = tmp_path / "idx.sqlite"
    with pytest.raises(DoorIndexError) as caught:
        build_index(_write(tmp_path, text), out, version="1", now=NOW, min_rows=MIN_ROWS)
    assert caught.value.code == ErrorCode.DOORINDEX_INVALID
    assert message in str(caught.value)
    assert not out.exists()


def test_per_district_minimum_counts(tmp_path):
    path = _write(tmp_path, csv_text(_door_lines(5)))
    with pytest.raises(DoorIndexError, match="中壢區"):
        build_index(path, tmp_path / "idx.sqlite", version="1", now=NOW, min_rows=MIN_ROWS)
    with pytest.raises(DoorIndexError, match="桃園區"):
        build_index(path, tmp_path / "idx.sqlite", version="1", now=NOW, min_rows={TAOYUAN: 6})


def test_default_minimums_reject_a_tiny_file(tmp_path):
    path = _write(tmp_path, csv_text(_door_lines(5) + _door_lines(5, ZHONGLI)))
    with pytest.raises(DoorIndexError):
        build_index(path, tmp_path / "idx.sqlite", version="1", now=NOW)


def test_header_must_be_exact(tmp_path):
    text = ",".join(HEADER[:-1]) + "\n"
    with pytest.raises(DoorIndexError, match="表頭"):
        build_index(
            _write(tmp_path, text), tmp_path / "i.sqlite", version="1", now=NOW, min_rows={}
        )


def test_read_info(city_index_path):
    info = read_info(city_index_path)
    assert info is not None and info.version == "11501" and info.doors > 30
    assert read_info(city_index_path.with_name("missing.sqlite")) is None


# ---------------------------------------------------------------- 比對


def test_exact_main_and_range(idx):
    assert idx.lookup("中壢區", "新生路", None, HouseNumber(num=4)).level == "exact"
    main = idx.lookup("中壢區", "新生路", None, HouseNumber(num=4, sub=9))
    assert main.level == "main_number"
    ranged = idx.lookup("中壢區", "新生路", None, HouseNumber(num=2, num_to=8))
    assert ranged.level == "number_range" and ranged.n_points == 3


def test_nearby_number_needs_close_neighbours(idx):
    near = idx.lookup("中壢區", "新生路", None, HouseNumber(num=6))
    assert near.level == "nearby_number" and near.nearest_num == 4
    # 新興路 2 號和 6 號相距好幾公里：不用鄰號，退回路的層級
    far = idx.lookup("中壢區", "新興路", None, HouseNumber(num=4))
    assert far.level == "road" and "number_not_found" in far.flags


def test_lane_and_road_levels(idx):
    lane = idx.lookup("中壢區", "新生路", None, HouseNumber(lane="121"))
    assert lane.level == "lane" and lane.n_points == 2
    road = idx.lookup("中壢區", "新生路", None, None)
    assert road.level == "road" and road.n_points == 5
    assert idx.lookup("中壢區", "不存在路", None, None) is None


def test_section_unknown_uses_union(idx):
    """R3-07：沒寫段而有分段的同名路，用「不帶段＋所有段」的聯集。"""
    union = idx.lookup("中壢區", "民族路", None, HouseNumber(num=3))  # 不帶段與三段都有 3 號
    assert union.level == "road" and union.n_points == 9
    assert "section_unknown" in union.flags and union.road_key == f"{ZHONGLI}|民族路*||"
    inferred = idx.lookup("中壢區", "民族路", None, HouseNumber(num=100))  # 只有二段有 100 號
    assert inferred.level == "exact" and "section_inferred" in inferred.flags
    assert inferred.road_key == f"{ZHONGLI}|民族路二段||"
    exact = idx.lookup("中壢區", "民族路", "二段", HouseNumber(num=100))
    assert exact.level == "exact"
    bare = idx.lookup("中壢區", "民族路", "", None)
    assert bare.n_points == 3


def test_parse_road_section_cases(idx):
    for district, text, base in [
        ("中壢區", "民族路", "民族路"),
        ("中壢區", "中園路", "中園路"),
        ("中壢區", "中豐北路", "中豐北路"),
        ("桃園區", "莊敬路", "莊敬路"),  # 只有一段、二段，沒有不帶段的
    ]:
        parts = idx.parse_road(district, text)
        assert parts.base == base and parts.section is None
        assert "section_unknown" in parts.flags
    single = idx.parse_road("桃園區", "國際路3號")  # 只有一段：沒寫段也不會弄錯
    assert (single.base, single.section) == ("國際路", "一段")
    assert "section_unknown" not in single.flags
    parts = idx.parse_road("中壢區", "中壢區民族路2段104號")
    assert (parts.base, parts.section, parts.number.num) == ("民族路", "二段", 104)
    assert idx.parse_road("中壢區", "中壢區中正路").section == ""


@pytest.mark.parametrize("text", ["中正路100段5號", "中正路0段", "民族路123456789段"])
def test_odd_section_digits_do_not_crash(idx, text):
    parts = idx.parse_road("中壢區", text)
    assert parts.base == "中正路" or parts.base == "民族路"


def test_parse_road_failures_and_intersections(idx):
    assert idx.parse_road("中壢區", "某某段123地號").failure == "land_parcel"
    assert idx.parse_road("中壢區", "完全沒有的路").failure == "road_not_found"
    cross = idx.parse_road("中壢區", "新生路與中正路交叉口")
    assert cross.cross_road == "中正路" and "intersection" in cross.flags


def test_same_name_roads_stay_in_their_district(idx):
    zl = idx.geocode("中壢區", "中正路10號")
    ty = idx.geocode("桃園區", "中正路1188號")
    assert zl.district == ZHONGLI and ty.district == TAOYUAN
    assert abs(zl.x - ty.x) > 5000


def test_road_doors_filters_by_highest_floor(idx):
    every = idx.road_doors("中壢區", "中正路", None, None)
    tall = idx.road_doors("中壢區", "中正路", None, 9)
    assert len(every) == 3 and len(tall) == 1
    assert idx.road_doors_by_key(f"{ZHONGLI}|中正路||", 9) == tall
    union = idx.road_doors_by_key(f"{ZHONGLI}|民族路*||", None)
    assert len(union) == 9


def test_district_at(idx):
    near_zl = idx.district_at(*Z1)
    assert near_zl.district == ZHONGLI and near_zl.decisive
    assert idx.district_at(P1[0] + 5, P1[1]).district != ZHONGLI
    nowhere = idx.district_at(255000.0, 2730000.0)
    assert nowhere.district is None and not nowhere.decisive


def test_district_at_is_not_decisive_near_a_boundary(tmp_path, ctx):
    doors = _door_lines(3) + [Door(BADE, "示範街", 1, (Z1[0] + 60, Z1[1]))]
    path = _write(tmp_path, csv_text(doors))
    out = tmp_path / "edge.sqlite"
    build_index(path, out, version="1", now=NOW, min_rows={})
    with DoorIndex.open(out) as index:
        found = index.district_at(Z1[0] + 2, Z1[1])
    assert found.district == TAOYUAN and not found.decisive


def test_road_frac_needs_context(city_index_path):
    with DoorIndex.open(city_index_path) as index, pytest.raises(DoorIndexError):
        index.road_frac([1], "taoyuan_core")


def test_road_frac_matches_point_metrics(idx, ctx):
    ids = idx.road_doors("桃園區", "國際路一段", None, None)
    xs, ys = zip(*idx.door_points(ids), strict=True)
    assert idx.road_frac(ids, "taoyuan_core") == ctx.road_frac_points(
        list(xs), list(ys), "taoyuan_core"
    )
    frac = idx.road_frac(ids, "taoyuan_core")
    assert set(frac.values()) == {0.5}  # 一半在中路特區內，一半離兩特區都很遠
    tall = idx.road_doors("桃園區", "國際路一段", None, 9)
    assert set(idx.road_frac(tall, "taoyuan_core").values()) == {1.0}


def test_doors_outside_area_districts_count_as_outside(idx):
    ids = idx.road_doors("中壢區", "民族路", None, None)
    frac = idx.road_frac(ids, "taoyuan_core")
    assert set(frac.values()) == {0.0}
    assert idx.road_frac([], "taoyuan_core") == {}


def test_metrics_are_recomputed_when_geo_changes(city_index_path, ctx):
    class Shifted:
        version = "other-version"

        def __getattr__(self, name):
            return getattr(ctx, name)

    with DoorIndex.open(city_index_path, Shifted()):
        pass
    with closing(sqlite3.connect(city_index_path)) as conn:
        assert (
            conn.execute("SELECT value FROM meta WHERE key='geo_version'").fetchone()[0]
            == "other-version"
        )
    with DoorIndex.open(city_index_path, ctx) as index:
        assert index.road_frac(index.road_doors("桃園區", "國際路一段", None, None), "taoyuan_core")
    with closing(sqlite3.connect(city_index_path)) as conn:
        assert (
            conn.execute("SELECT value FROM meta WHERE key='geo_version'").fetchone()[0]
            == ctx.version
        )
