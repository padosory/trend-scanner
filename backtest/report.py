"""백테스트 결과(거래 리스트) 요약 출력. run_backtest.py / run_signal_backtest.py 공용."""

import logging

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
    logger.info("평균 수익률: %.2f%%", sum(returns) / len(returns))
    if wins:
        logger.info("평균 수익(승): %.2f%%", sum(wins) / len(wins))
    if losses:
        logger.info("평균 손실(패): %.2f%%", sum(losses) / len(losses))
