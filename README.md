# KOSPI Top 200 — CCI(9) + DMI/ADX + CVD Proxy Backtest

현재 시점의 **KOSPI 시가총액 상위 200개 종목**을 유니버스로 잡아 세 가지 전략을 같은 조건에서 비교하는 백테스트입니다.

> 요청 조건에 따라 생존편향(survivorship bias)은 보정하지 않습니다.

## 비교 전략

1. `cci_dmi`
   - 진입: CCI(9)가 0선을 상향 돌파 + `+DI > -DI` + ADX >= 20
   - 청산: CCI가 0선을 하향 돌파 또는 `-DI > +DI`

2. `cci_dmi_cvd_filter`
   - 1번 조건 + CVD Proxy 강세 확인까지 만족해야 진입
   - 따라서 거래 횟수/회전율이 가장 낮아질 가능성이 큼

3. `cci_dmi_cvd_sizing`
   - 진입 자체는 CCI+DMI로 허용
   - CVD Proxy가 강하면 상대 비중 1.0, 약하면 0.5
   - CVD를 강제 필터로 쓰지 않아 회전율 저하를 완화하기 위한 버전

## CVD에 대한 중요한 제한

이 코드는 무료 일봉 OHLCV 데이터를 사용합니다. 따라서 실제 매수체결/매도체결량을 분리한 **진짜 CVD(Cumulative Volume Delta)** 는 계산할 수 없습니다.

기본 구현은 다음 proxy입니다.

```text
signed_volume = Volume × sign(Close - Open)
CVD_proxy = cumulative_sum(signed_volume)
```

즉 결과에서 CVD는 반드시 **CVD Proxy**로 해석해야 합니다. 향후 증권사 API/체결 데이터를 확보하면 `cvd_proxy()` 함수만 실제 CVD 계산으로 교체할 수 있습니다.

## 룩어헤드 방지

지표와 신호는 t일 종가까지의 데이터로 계산한 뒤 `shift(1)` 하여 다음 거래일부터 포지션에 반영합니다. 같은 봉의 종가를 보고 같은 봉 수익률을 먹는 형태의 룩어헤드는 피했습니다.

## 기본 설정

- Universe: 현재 KOSPI 시가총액 상위 200
- 기간: 최근 5년
- CCI: 9
- DMI/ADX: 14
- ADX 최소값: 20
- CVD slope: 5일
- CVD EMA: 10일
- 최대 동시 보유: 30종목
- 수수료: 1.5bp
- 슬리피지: 3bp
- Long only
- 포트폴리오: 활성 종목 간 균등비중, sizing 버전은 CVD 확인 여부에 따라 상대비중 조정

## 로컬 실행

```bash
pip install -r requirements.txt
python backtest.py
```

예:

```bash
python backtest.py \
  --years 7 \
  --top-n 200 \
  --cci-period 9 \
  --dmi-period 14 \
  --adx-threshold 20 \
  --cvd-slope-period 5 \
  --cvd-ema-period 10 \
  --max-positions 30 \
  --commission-bps 1.5 \
  --slippage-bps 3
```

## GitHub Actions에서 실행

파일 전체를 GitHub 저장소에 올린 뒤:

1. 저장소의 **Actions**
2. `KOSPI CCI-DMI-CVD Backtest`
3. **Run workflow**
4. 기간/종목수/최대 보유종목 입력
5. 완료된 실행의 `Artifacts`에서 `kospi-backtest-results` 다운로드

GitHub에 쓰기 권한을 ChatGPT에 줄 필요가 없습니다. 사용자가 직접 파일만 업로드하면 됩니다.

## 결과 파일

`results/summary.csv`
- 세 전략 + KOSPI benchmark 성과 비교
- Total Return
- CAGR
- Annual Volatility
- Sharpe
- MDD
- Calmar
- 평균 일일 회전율
- 연환산 회전율 근사치
- 평균 보유 종목 수

`results/portfolio_*.csv`
- 일별 gross/net return
- 거래비용
- turnover
- equity curve
- 보유 종목 수

`results/weights_*.csv`
- 일자별 종목별 포트폴리오 비중

`results/signals_*.csv`
- CCI, +DI, -DI, ADX, CVD Proxy, CVD slope, 포지션 신호

`results/universe_current_top200.csv`
- 실행 시점 현재 KOSPI 시총 상위 종목

## 해석할 때 특히 볼 것

단순 누적수익률 하나만 보지 말고 아래를 같이 비교하세요.

- CAGR
- MDD
- Sharpe
- Calmar
- 평균 회전율
- 비용 차감 전/후 수익률
- 평균 동시 보유 종목 수

특히 이번 실험의 핵심은:

```text
CCI+DMI
vs
CCI+DMI+CVD 강제필터
vs
CCI+DMI + CVD 포지션사이징
```

중 어떤 방식이 **CVD를 추가하면서도 회전율을 지나치게 죽이지 않는지** 확인하는 것입니다.

## 데이터

- KOSPI 유니버스/시가총액: pykrx
- 가격/거래량 및 KOSPI benchmark: yfinance/Yahoo Finance

Yahoo Finance 데이터는 연구/개인 분석용으로 사용하세요.


## GitHub Actions에서 pykrx 오류가 날 때

GitHub Actions의 해외/클라우드 IP에서 KRX가 정상 JSON 대신 빈 응답이나 차단 페이지를 반환하면
`Expecting value: line 1 column 1 (char 0)` 오류가 발생할 수 있습니다.

수정 버전은 유니버스를 아래 순서로 자동 조회합니다.

1. `pykrx` / KRX 시가총액 데이터
2. 실패 시 Yahoo Finance Korea screener에서 `.KS` 종목만 골라 시가총액 상위 종목 구성

따라서 KRX 접속 실패만으로 전체 백테스트가 종료되지 않습니다.
`results/universe_current_top200.csv`의 `universe_source` 열에서 실제 사용된 소스를 확인할 수 있습니다.


## v3: Yahoo 401 오류 수정

이 버전에서는 Yahoo Finance Screener fallback을 제거했습니다.

GitHub Actions에서:
- KRX/pykrx가 정상 작동하면 KRX 시가총액 데이터를 사용
- KRX가 JSON 오류로 실패하면 네이버 금융 `KOSPI 시가총액` 페이지를 읽어 상위 200종목을 구성

네이버 금융은 시가총액 순으로 종목을 페이지별 표시하므로 첫 페이지부터 필요한 개수만큼 순서대로
수집합니다. `results/universe_current_top200.csv`의 `universe_source`가
`naver_finance_market_cap`이면 fallback이 사용된 것입니다.

이 방식은 Yahoo Screener의 401 인증 오류를 사용하지 않습니다.
