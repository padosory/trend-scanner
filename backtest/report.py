"""백테스트 결과(거래 리스트) 요약 출력. run_backtest.py / run_signal_backtest.py 공용."""

import logging
from statistics import median

logger = logging.getLogger(__name__)


def summarize(trades) -> None:
    if not trades:
        logger.info("거래 없음")
        return

    returns = [t.return_pct for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    logger.info("총 거래 수: %d", len(trades))
    logger.info("승률: %.1f%% (%d승 %d패)", 100 * len(wins) / len(returns), len(wins), len(losses))
    # 불변 규율: 평균 · 중간값 · 상위20건 수익비중 3가지를 항상 출력한다.
    logger.info("평균 수익률: %.2f%%", sum(returns) / len(returns))
    logger.info("중간값 수익률: %.2f%%", median(returns))

    total = sum(returns)
    top20 = sorted(returns, reverse=True)[:20]
    if total != 0:
        logger.info(
            "상위20건 수익비중: %.1f%% (상위20건 합 %.2f%% / 전체 합 %.2f%%)",
            100 * sum(top20) / total,
            sum(top20),
            total,
        )
    else:
        logger.info("상위20건 수익비중: N/A (전체 수익률 합 0)")

    if wins:
        logger.info("평균 수익(승): %.2f%%", sum(wins) / len(wins))
    if losses:
        logger.info("평균 손실(패): %.2f%%", sum(losses) / len(losses))
