## PROPOSE: 마켓 레짐 베어 감지에 ADX 조건 추가

**현황:** 
- BTC 75,540 USDT, EMA50=77,822 (-2.9%). Lower band=76,336 → 현재가가 하단밴드 아래 → bear_regime → 전체 진입 차단
- ADX(14)=13.5 (매우 낮음, 횡보/비추세 시장)
- bot이 4시간째 진입 불가 상태 (67건 entry_skip)
- Total PnL +8.07 USDT, 최근 60% 승률 — 트레이딩 자체는 양호

**문제:**
- `detect_market_regime()`에서 상승장(bull)은 `ADX >= 25` 조건이 있지만, 하락장(bear)은 ADX 조건 없이 가격만으로 판단
- ADX 13.5 = 추세 강도가 거의 없음. 실제 하락 추세가 아닌 횡보/조정 구간
- Bull 레짐만 ADX 체크하는 비대칭 로직 → 추세 없는 횡보장에서 bear_regime 과잉 반응

**제안:**
`data/market_data.py` 74행: bear 체크에도 ADX 조건 추가

```python
# 변경 전:
elif close < lower_band:
    return "bear"

# 변경 후:
elif close < lower_band and btc_adx >= config.ADX_TRENDING_THRESHOLD:
    return "bear"
```

**기대 효과:**
- ADX < 25인 횡보장에서는 bear_regime 발동하지 않고 neutral → 기존 필터(RSI, 거래량, BTC soft-pass 등)로만 진입 판단
- ADX >= 25인 실제 강한 하락 추세에서는 bear_regime 정상 작동
- 불필요한 진입 차단 해소로 트레이딩 기회 회복

**리스크:**
- ADX가 낮아도 가격이 밴드 아래로 크게 이탈하면(bear 추세 초기) 진입 허용될 수 있음
  - 대응: BTC_CRASH_FILTER_PCT_4H (-3%)가 여전히 작동. 4h 동안 -3% 이상 하락 시 별도 차단
  - 대응: BTC soft-pass 필터(EMA50 sideband)가 여전히 작동. BTC가 EMA50에서 3% 이상 이탈 시 일반 모드로 복귀
- But 현재 ADX 13.5는 추세 부재를 강하게 시사. 횡보장에서 bear 차단이 전혀 없는 것보다 낫다.

**질문:**
1. 위 변경이 타당한가? ADX < 25에서 bear_regime을 해제해도 무방한가?
2. BTC_BAND_ATR_MULTI를 2.0→1.5로 낮추는 방안보다 ADX 조건 추가가 더 나은가?
3. 다른 보완 장치(BTC 크래시 필터, soft-pass)로 리스크가 충분히 통제되는가?
