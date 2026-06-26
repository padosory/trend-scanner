"""Plotly 캔들스틱+거래량 차트 → HTML div 문자열.

include_plotlyjs=False 를 사용하므로 템플릿에서 Plotly CDN 스크립트를 로드해야 한다.
"""

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def make_chart_div(ticker: str, df: pd.DataFrame, scan_date: pd.Timestamp,
                   high_52w: float | None = None) -> str:
    """최근 120 거래일 캔들스틱+거래량 차트를 HTML div 문자열로 반환한다."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        logger.warning("plotly 미설치 — 차트 생략")
        return ""

    recent = df.tail(120).copy()

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.7, 0.3], vertical_spacing=0.03,
    )

    fig.add_trace(
        go.Candlestick(
            x=recent.index,
            open=recent["open"], high=recent["high"],
            low=recent["low"], close=recent["close"],
            name="OHLC",
            increasing_line_color="#ef5350",
            decreasing_line_color="#26a69a",
        ),
        row=1, col=1,
    )

    if "resistance_60" in recent.columns:
        fig.add_trace(
            go.Scatter(
                x=recent.index, y=recent["resistance_60"],
                mode="lines", name="저항(60일)",
                line=dict(color="#FF6B00", width=1.5, dash="dash"),
            ),
            row=1, col=1,
        )

    # high_52w: 스캔에서 계산된 값을 수평선으로 표시 (차트 기간이 짧아 rolling이 NaN되는 문제 회피)
    h52 = high_52w
    if h52 is None and "high_52w" in recent.columns:
        h52 = recent["high_52w"].dropna().iloc[-1] if not recent["high_52w"].dropna().empty else None
    if h52 is not None:
        fig.add_hline(
            y=h52,
            line=dict(color="#00BFFF", width=2, dash="dot"),
            annotation_text="52주고가",
            annotation_position="top right",
            annotation_font=dict(color="#00BFFF", size=11),
            row=1, col=1,
        )

    colors = [
        "#ef5350" if c >= o else "#26a69a"
        for c, o in zip(recent["close"], recent["open"])
    ]
    fig.add_trace(
        go.Bar(x=recent.index, y=recent["volume"], name="거래량", marker_color=colors),
        row=2, col=1,
    )

    if scan_date in recent.index:
        scan_row = recent.loc[scan_date]
        fig.add_trace(
            go.Scatter(
                x=[scan_date],
                y=[float(scan_row["high"]) * 1.02],
                mode="markers",
                marker=dict(symbol="triangle-down", size=14, color="red"),
                name="신호",
            ),
            row=1, col=1,
        )

    fig.update_layout(
        title=dict(text=ticker, x=0.02),
        height=420,
        xaxis_rangeslider_visible=False,
        margin=dict(l=40, r=20, t=35, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        paper_bgcolor="white",
        plot_bgcolor="#f9f9f9",
        font=dict(size=11),
    )

    return fig.to_html(full_html=False, include_plotlyjs=False)
