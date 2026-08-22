"""对选股主链路做 cProfile 热点剖析（80s 用例的耗时归因）。"""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR.parent))

from Kuantix.adapters.factor_bridge import L1Reader  # noqa: E402
from Kuantix.config import get_config  # noqa: E402
from Kuantix.data.market_store import MarketStore  # noqa: E402
from Kuantix.factor.store import FactorStore  # noqa: E402
from Kuantix.screen.service import ScreenService  # noqa: E402

config = get_config()
store = MarketStore()
reader = L1Reader(config.paths.vipdoc, backend="auto", store=store)
factor_store = FactorStore(config.paths.factors, config.paths.db)
screen = ScreenService(config, store=factor_store, reader=reader)


def target() -> None:
    screen.screen_factor(
        factor="momentum_60d", market="CN", top_n=50, order="desc",
        as_of=None, tech_cond={"min_close": 5.0}, chanlun_cond={},
    )


pr = cProfile.Profile()
pr.enable()
target()
pr.disable()

buf = io.StringIO()
st = pstats.Stats(pr, stream=buf).sort_stats("cumulative")
st.print_stats(35)
print(buf.getvalue())

buf2 = io.StringIO()
st2 = pstats.Stats(pr, stream=buf2).sort_stats("tottime")
st2.print_stats(25)
print("\n\n================ TOTTIME（自身耗时）================\n")
print(buf2.getvalue())
