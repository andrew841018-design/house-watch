"""house_watch：桃園找房雷達。

定時整理售屋網站的開價物件與實價登錄成交，只輸出不含個人條件的事實；
所有條件判斷都在瀏覽器裡做（PLAN 審查 r4 §3.2）。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("house-watch")
except PackageNotFoundError:  # 直接從原始碼執行、沒有安裝時
    __version__ = "0.0.0"
