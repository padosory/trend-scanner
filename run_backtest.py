"""백테스트 실행 스크립트.

예시:
    python run_backtest.py --start 20230101 --end 20251231 --limit 50
    python run_backtest.py --start 20230101 --end 20251231 --tickers 005930,000660

--limit 없이 전체 유니버스(약 2,500종목)를 돌리면 pykrx 호출이 많아 시간이 오래 걸린다.
처음엔 --limit으로 소규모로 검증한 뒤 점차 늘리는 걸 권장.
"""

import argparse
import logging
from pathlib import Path

import pandas as pd

from backtest.data_cache import get_universe
from backtest.engine import BacktestEngine
from backtest.report import summarize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent / "backtest" / "results"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYYMMDD")
    parser.add_argument("--end", required=True, help="YYYYMMDD")
    parser.add_argument("--tickers", help="쉼표구분 종목코드. 없으면 --start 기준 전체 유니버스 조회")
    parser.add_argument("--limit", type=int, help="유니버스 종목 수 제한 (테스트용)")
    args = parser.parse_args()

    if args.tickers:
        tickers = args.tickers.split(",")
    else:
        tickers = get_universe()
        if args.limit:
            tickers = tickers[: args.limit]

    engine = BacktestEngine(tickers, args.start, args.end)
    trades = engine.run()

    summarize(trades)

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"trades_{args.start}_{args.end}.csv"
    pd.DataFrame([t.__dict__ for t in trades]).to_csv(out_path, index=False, encoding="utf-8-sig")
    logger.info("거래내역 저장: %s", out_path)

    if engine.rs_log:
        rs_path = RESULTS_DIR / f"rs_log_{args.start}_{args.end}.csv"
        pd.DataFrame(engine.rs_log).to_csv(rs_path, index=False, encoding="utf-8-sig")
        logger.info("RS 기록 저장: %s", rs_path)


if __name__ == "__main__":
    main()
