"""trend_scanner 스캐너/백테스트 파라미터.

백테스트하면서 더 나은 값이 보이면 여기만 바꾸면 된다.
"""

# 시장 기준 타임존. '오늘/어제'는 반드시 이 기준으로 계산한다.
# (CI 러너는 UTC라 naive now()를 쓰면 KST보다 하루 뒤처져 스캔일이 밀린다)
MARKET_TZ = "Asia/Seoul"

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

# 워치리스트 이탈 확정 기준 (watch_tracker) — 경계선 진동을 '이탈'로 세지 않기
# 위한 히스테리시스. 없으면 90% 경계를 왕복하는 종목이 매일 이탈로 집계돼
# 전일 대비 카운터가 노이즈로 전락한다.
WATCH_EXIT_PROXIMITY = 0.88    # 고가근접이 이 아래로 떨어지면 1회로 즉시 이탈 확정
WATCH_EXIT_GRACE_SCANS = 2     # 그 위(88~90%)는 N회 연속 밴드 밖일 때만 이탈 확정

# 워치리스트 체류 '형태' 판정 (watch_tracker) — 같은 대기 5일이라도 변동폭이
# 좁아지며 거래량이 마르는 수축형(매물 소화, 미너비니 VCP)과 변동폭이 줄지 않은
# 정체형은 의미가 정반대다. 경과일수만으로는 이 둘이 구분되지 않는다.
WATCH_SHAPE_MIN_DAYS = 3        # 체류가 이 미만이면 표본 부족 — 판정하지 않음
WATCH_SHAPE_MAX_DAYS = 30       # 판정에 쓸 최대 체류 구간(오래된 구간은 희석)
WATCH_SHAPE_BASELINE_DAYS = 20  # 체류 직전 비교 구간(이미 좁게 굳은 베이스 인식용)
WATCH_COIL_RANGE_RATIO = 0.85   # 체류 후반 변동폭 < 전반 × 이 값 → 수축
WATCH_COIL_TIGHT_RATIO = 0.75   # 체류 후반 변동폭 < 직전 베이스라인 × 이 값 → 수축
WATCH_COIL_VOL_RATIO = 0.90     # 거래량도 이 배 미만으로 말라야 수축으로 인정

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
