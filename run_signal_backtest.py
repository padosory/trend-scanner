"""개별 신호(MA크로스/STEP1/STEP2/STEP3/RS) 단독 백테스트 실행 스크립트.

예시:
    python run_signal_backtest.py --strategy ma20 --start 20230101 --end 20251231
    python run_signal_backtest.py --strategy step3 --start 20230101 --end 20251231 --limit 50

설계서.md §6.x "개별 신호 검증" 참고.
"""

import argparse
import logging
from pathlib import Path

import pandas as pd

from backtest.data_cache import get_universe
from backtest.experiments import STRATEGIES
from backtest.report import summarize
from backtest.signal_engine import FILL_MODES, SignalBacktestEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent / "backtest" / "results"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    parser.add_argument("--start", required=True, help="YYYYMMDD")
    parser.add_argument("--end", required=True, help="YYYYMMDD")
    parser.add_argument("--tickers", help="쉼표구분 종목코드. 없으면 --start 기준 전체 유니버스 조회")
    parser.add_argument("--limit", type=int, help="유니버스 종목 수 제한 (테스트용)")
    parser.add_argument(
        "--exclude-prefix",
        help="쉼표구분 종목코드 접두사 — 이걸로 시작하는 종목은 유니버스에서 제외 (예: 900번대 우회상장 종목 제외시 '900')",
    )
    parser.add_argument("--tag", help="결과 파일명에 붙일 구분자 (예: --tag no900 -> signal_step2_rs_no900_...)")
    parser.add_argument(
        "--fill",
        choices=FILL_MODES,
        default="close",
        help="체결 시점: close(신호일 종가) | next_open(T+1 시가) | next_high(T+1 고가) | next_vwap(T+1 VWAP근사)",
    )
    args = parser.parse_args()

    if args.tickers:
        tickers = args.tickers.split(",")
    else:
        tickers = get_universe()
        if args.limit:
            tickers = tickers[: args.limit]

    if args.exclude_prefix:
        prefixes = tuple(args.exclude_prefix.split(","))
        before = len(tickers)
        tickers = [t for t in tickers if not t.startswith(prefixes)]
        logger.info("접두사 %s 종목 제외: %d -> %d개", prefixes, before, len(tickers))

    entry_fn, exit_fn = STRATEGIES[args.strategy]()
    engine = SignalBacktestEngine(tickers, args.start, args.end, entry_fn, exit_fn, fill_mode=args.fill)
    trades = engine.run()

    summarize(trades)

    RESULTS_DIR.mkdir(exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    out_path = RESULTS_DIR / f"signal_{args.strategy}{suffix}_{args.fill}_{args.start}_{args.end}.csv"
    pd.DataFrame([t.__dict__ for t in trades]).to_csv(out_path, index=False, encoding="utf-8-sig")
    logger.info("거래내역 저장: %s", out_path)


if __name__ == "__main__":
    main()
