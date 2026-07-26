"""trend_scanner 스캐너/백테스트 파라미터.

백테스트하면서 더 나은 값이 보이면 여기만 바꾸면 된다.
"""

# STEP1: 추세 필터 (미너비니 Trend Template 변형)
MA_WINDOWS = (10, 20, 50, 150, 200)
MA200_SLOPE_LOOKBACK = 20  # ma200가 n일 전보다 높으면 상승 기울기로 판단

# STEP2: 돌파 스캔 (오닐 방식)
BREAKOUT_HIGH_PCT = 0.95        # 52주 고가의 95% 이상
BREAKOUT_VOLUME_MULT = 1.5      # 20일 평균거래량의 1.5배 이상
RESISTANCE_WINDOW = 60          # 직전 60일 저항선 돌파

# 돌파 준비 워치리스트 (매매 신호 아님, 시장 맥락 참고용)
# 52주 고가의 이 비율 이상~돌파(BREAKOUT_HIGH_PCT) 미만이면 '고가 근접' 후보
WATCH_PROXIMITY_LOW = 0.90

# 워치리스트 (STEP2 통과 후 STEP3 재검사 대기)
WATCHLIST_TTL_DAYS = 15

# STEP3: 눌림목 스캔 (단테/와인스타인 방식)
PULLBACK_BAND_LOW = 0.90        # MA20의 -10%
PULLBACK_BAND_HIGH = 1.03       # MA20의 +3%
PULLBACK_PRICE_BREAKOUT_WINDOW = 3   # 양봉마감 + 직전 N일 고가 돌파

# 청산
STOP_LOSS_PCT = 0.08             # entry가 대비 -8% (워치리스트 등록 이후 최저가와 비교해 더 타이트한 쪽 채택)

# 유동성 필터 (신호 발생 시점에만 체크)
MIN_AVG_TRADING_VALUE = 500_000_000   # 20일 평균 거래대금 5억원
MIN_MARKET_CAP = 50_000_000_000       # 시가총액 500억원

# 상대강도(RS) — 직전 60일 수익률 기준 전종목 percentile rank
RS_LOOKBACK_DAYS = 60
RS_PERCENTILE_THRESHOLD = 0.80  # 0.80 = 상위 20%만 통과. 0으로 두면 필터 비활성화와 동일
