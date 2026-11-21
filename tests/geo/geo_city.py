"""tests/geo 共用的合成資料：門牌 CSV 與已知位置。

坐標是 TM2（公尺），刻意放在真實範圍檔的已知位置附近（中壢交流道周邊、中路特區、藝文里、
蘆竹側、八德、平鎮），所以範圍指標用套件裡真正的 geo 檔算。門牌、路名組合、樓層都是合成的。
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from house_watch.geo.doorindex import HEADER

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "geo"
NOW = dt.datetime(2026, 1, 5, 3, 0, tzinfo=dt.UTC)

TAOYUAN, ZHONGLI, PINGZHEN, BADE, LUZHU = "6800100", "6800200", "6801000", "6800800", "6800500"

# 已知位置（TM2）。數值由真實 geo 檔核對過：
Z1 = (272718.0, 2761920.0)  # 中壢區內，最小車程檔內
Z3 = (267164.0, 2762465.0)  # 中壢區內，最大車程檔內
Z4 = (271201.0, 2763579.0)  # 中壢區內
P1 = (270197.0, 2759701.0)  # 中壢區界外（平鎮側）
B1 = (280299.0, 2758058.0)  # 八德側，離中壢區很遠
ZL = (279448.0, 2766014.0)  # 中路特區內（形心）
YW = (280141.0, 2767746.0)  # 藝文里內
FAR_TY = (282304.0, 2764708.0)  # 桃園區，離兩特區都超過最大周邊選項
LZ = (277810.0, 2766514.0)  # 蘆竹側，在被裁掉的那一塊裡

FW = str.maketrans("0123456789", "０１２３４５６７８９")
CN_FLOORS = "一二三四五六七八九十"


def fw(text: object) -> str:
    """數字轉全形，和門牌檔一樣。"""
    return str(text).translate(FW)


def cn_floor(n: int) -> str:
    if n <= 10:
        return CN_FLOORS[n - 1]
    if n < 20:
        return "十" + CN_FLOORS[n - 11]
    return CN_FLOORS[n // 10 - 1] + "十" + (CN_FLOORS[n % 10 - 1] if n % 10 else "")


@dataclass(frozen=True)
class Door:
    code: str
    road: str
    num: int
    at: tuple[float, float]
    floors: int = 1
    lane: str = ""
    sub: int = 0
    dx: float = 0.0
    dy: float = 0.0


def door_rows(door: Door) -> list[str]:
    number = fw(door.num) + (f"之{fw(door.sub)}" if door.sub else "") + "號"
    lane = f"{fw(door.lane)}巷" if door.lane else ""
    x, y = door.at[0] + door.dx, door.at[1] + door.dy
    rows = []
    for floor in range(1, door.floors + 1):
        text = number if floor == 1 else number + cn_floor(floor) + "樓"
        rows.append(f"68,{door.code},甲里,001,{door.road},,{lane},,{text},{x:.3f},{y:.3f}")
    return rows


def csv_text(doors: list[Door]) -> str:
    lines = [",".join(HEADER)]
    for door in doors:
        lines.extend(door_rows(door))
    return "\n".join(lines) + "\n"


def city_doors() -> list[Door]:
    """一個小小的合成城市。"""
    doors: list[Door] = []

    def road(code, name, nums, at, step=(10.0, 0.0), floors=1, lane=""):
        for i, num in enumerate(nums):
            doors.append(
                Door(code, name, num, at, floors=floors, lane=lane, dx=step[0] * i, dy=step[1] * i)
            )

    # 中壢區
    road(ZHONGLI, "民族路", [1, 3, 5], Z1)
    road(ZHONGLI, "民族路二段", [100, 102, 104], Z4)
    road(ZHONGLI, "民族路三段", [3, 200, 202], Z3)
    road(ZHONGLI, "中正路", [10, 14], Z4, step=(0.0, 15.0), floors=2)
    road(ZHONGLI, "中正路", [30], Z1, floors=15)
    road(ZHONGLI, "中園路", [7, 9], Z1, step=(0.0, -20.0))
    road(ZHONGLI, "中園路二段", [41, 43], Z4, step=(0.0, -20.0))
    road(ZHONGLI, "中豐北路一段", [61, 63], Z4, step=(-20.0, 0.0))
    road(ZHONGLI, "中豐北路二段", [81], Z3)
    road(ZHONGLI, "新生路", [2, 4, 8], Z1, step=(0.0, 20.0))
    road(ZHONGLI, "新生路", [1, 3], Z1, lane="121", step=(0.0, 30.0))
    road(ZHONGLI, "新興路", [2], Z1)
    road(ZHONGLI, "新興路", [6], Z3)
    # 平鎮區：同名的民族路二段，以及中壢區沒有的南平路
    road(PINGZHEN, "民族路二段", [300, 302], P1)
    road(PINGZHEN, "南平路", [5, 7], P1, step=(0.0, 10.0))
    # 八德區
    road(BADE, "中華路", [1, 3, 5], B1)
    # 桃園區
    road(TAOYUAN, "中正路", [1188, 1190], YW, floors=2)
    road(TAOYUAN, "莊敬路一段", [11, 13], YW, step=(0.0, 30.0))
    road(TAOYUAN, "莊敬路二段", [21], YW, step=(0.0, 30.0))
    road(TAOYUAN, "南平路", [54], YW)
    road(TAOYUAN, "國際路一段", [1, 3, 5, 7], ZL, step=(10.0, 10.0), floors=9)
    road(TAOYUAN, "國際路一段", [101, 103, 105, 107], FAR_TY, step=(10.0, 0.0), floors=2)
    # 蘆竹區
    road(LUZHU, "南崁路", [1, 3], LZ)
    return doors


MIN_ROWS = {TAOYUAN: 1, ZHONGLI: 1}
