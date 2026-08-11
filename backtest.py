#!/usr/bin/env python3
"""
KOSPI Market-Cap Top 200: CCI(9) + DMI/ADX + CVD Proxy backtest

Important:
- Universe = CURRENT KOSPI market-cap top N as of the latest KRX business day.
  Survivorship bias is intentionally NOT corrected, per the requested setup.
- Daily OHLCV cannot reconstruct true bid/ask aggressor CVD.
  This script uses a clearly labeled CVD PROXY:
      signed_volume = volume * sign(close - open)
      CVD_proxy = cumulative sum(signed_volume)
- Signals are calculated with information available at day t close and applied
  to the next trading day's close-to-close return via a 1-day shift.
  This avoids same-bar look-ahead in signal application.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import yfinance as yf
from pykrx import stock


@dataclass
class Config:
    years: int = 5
    top_n: int = 200
    cci_period: int = 9
    dmi_period: int = 14
    adx_threshold: float = 20.0
    cvd_slope_period: int = 5
    cvd_ema_period: int = 10
    commission_bps: float = 1.5
    slippage_bps: float = 3.0
    max_positions: int = 30
    benchmark: str = "^KS11"
    output_dir: str = "results"
    min_history_days: int = 120


def latest_krx_business_day(max_lookback: int = 14) -> str:
    """
    Find a recent KRX business day.

    NOTE:
    pykrx can intermittently fail on GitHub Actions / overseas cloud IPs
    because KRX endpoints may return non-JSON/blocked responses. Therefore
    this helper is only used as the FIRST universe source and failure is
    allowed; current_kospi_top_n() has a Yahoo fallback.
    """
    today = datetime.now()
    last_error = None
    for i in range(max_lookback):
        d = (today - timedelta(days=i)).strftime("%Y%m%d")
        try:
            cap = stock.get_market_cap_by_ticker(d, market="KOSPI")
            if cap is not None and not cap.empty:
                return d
        except Exception as e:
            last_error = e
            continue
    raise RuntimeError(
        "Could not query a recent KRX business day via pykrx. "
        f"Last error: {last_error}"
    )


def _universe_from_pykrx(n: int) -> pd.DataFrame:
    """Primary source: KRX/pykrx market-cap table."""
    asof = latest_krx_business_day()
    cap = stock.get_market_cap_by_ticker(asof, market="KOSPI").copy()
    if cap.empty or "시가총액" not in cap.columns:
        raise RuntimeError("pykrx returned an empty/unexpected market-cap table.")

    cap = cap.sort_values("시가총액", ascending=False)
    rows = []
    for ticker, row in cap.head(max(n + 30, n)).iterrows():
        try:
            name = stock.get_market_ticker_name(ticker)
        except Exception:
            name = str(ticker)
        rows.append(
            {
                "ticker": str(ticker).zfill(6),
                "name": name,
                "market_cap": float(row["시가총액"]),
                "asof": asof,
                "universe_source": "pykrx",
            }
        )
    out = pd.DataFrame(rows).head(n).reset_index(drop=True)
    out["yf_ticker"] = out["ticker"] + ".KS"
    return out


def _universe_from_naver(n: int) -> pd.DataFrame:
    """
    Fallback source for GitHub Actions:
    Naver Finance KOSPI market-cap ranking pages.

    Naver Finance exposes KOSPI market-cap rankings with roughly 50 stocks
    per page. We scrape enough pages to cover n names, extract the 6-digit
    ticker from the stock detail link, and preserve the displayed ranking.

    This avoids Yahoo Screener authentication (401) and does not depend on
    KRX JSON endpoints that may fail from overseas/cloud IPs.
    """
    import requests
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": "https://finance.naver.com/",
        "Connection": "keep-alive",
    }

    # Page normally contains 50 ranked securities.
    pages = max(4, math.ceil(n / 50) + 1)
    rows = []
    seen = set()

    session = requests.Session()
    session.headers.update(headers)

    for page in range(1, pages + 1):
        url = (
            "https://finance.naver.com/sise/"
            f"sise_market_sum.naver?sosok=0&page={page}"
        )
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()

            # Naver Finance pages are commonly EUC-KR/CP949.
            r.encoding = r.apparent_encoding or "euc-kr"
            soup = BeautifulSoup(r.text, "html.parser")

            table = soup.select_one("table.type_2")
            if table is None:
                print(f"[warn] Naver page {page}: market-cap table not found")
                continue

            page_count = 0
            for tr in table.select("tr"):
                a = tr.select_one('a.tltle[href*="code="]')
                if a is None:
                    continue

                href = a.get("href", "")
                m = re.search(r"code=(\d{6})", href)
                if not m:
                    continue

                ticker = m.group(1)
                if ticker in seen:
                    continue

                tds = tr.find_all("td")
                # Naver market-cap table order commonly:
                # N, name, current, change, %, par, mcap, shares, ...
                market_cap = np.nan
                try:
                    # Find numeric cells and use the market-cap column by CSS-independent
                    # position: after name, current, diff, rate, par.
                    # Because columns can change, market cap is informational only;
                    # ranking comes from Naver's displayed order.
                    numeric_texts = [
                        td.get_text(" ", strip=True).replace(",", "")
                        for td in tds
                    ]
                    if len(numeric_texts) >= 7:
                        mc_txt = numeric_texts[6].replace("조", "").replace("억", "")
                        market_cap = float(re.sub(r"[^0-9.\-]", "", mc_txt) or "nan")
                except Exception:
                    market_cap = np.nan

                rows.append(
                    {
                        "ticker": ticker,
                        "name": a.get_text(strip=True),
                        "market_cap": market_cap,
                        "asof": datetime.now().strftime("%Y%m%d"),
                        "universe_source": "naver_finance_market_cap",
                        "yf_ticker": ticker + ".KS",
                    }
                )
                seen.add(ticker)
                page_count += 1

            print(f"[universe] Naver page {page}: {page_count} symbols")

            if len(rows) >= n:
                break

            time.sleep(0.35)

        except Exception as e:
            print(f"[warn] Naver page {page} failed: {type(e).__name__}: {e}")

    out = pd.DataFrame(rows)
    if out.empty:
        raise RuntimeError("Naver Finance fallback returned no KOSPI symbols.")

    # The page itself is already market-cap-ranked. Preserve page/row order.
    out = out.drop_duplicates("ticker", keep="first").reset_index(drop=True)

    if len(out) < n:
        raise RuntimeError(
            f"Naver Finance returned only {len(out)} KOSPI symbols; expected at least {n}."
        )

    return out.head(n).reset_index(drop=True)


def current_kospi_top_n(n: int) -> pd.DataFrame:
    """
    Current KOSPI market-cap top N.

    Source priority:
      1) pykrx/KRX
      2) Naver Finance KOSPI market-cap ranking

    Survivorship bias is intentionally NOT corrected.
    """
    try:
        print("[universe] trying pykrx/KRX...")
        out = _universe_from_pykrx(n)
        if len(out) >= n:
            print(f"[universe] pykrx succeeded: {len(out)} symbols")
            return out.head(n).reset_index(drop=True)
        raise RuntimeError(f"pykrx returned only {len(out)} symbols")
    except Exception as e:
        print(f"[warn] pykrx universe failed: {type(e).__name__}: {e}")
        print("[universe] falling back to Naver Finance market-cap pages...")

    out = _universe_from_naver(n)
    print(f"[universe] Naver succeeded: {len(out)} symbols")
    return out.head(n).reset_index(drop=True)


def download_ohlcv(tickers: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
    """
    Download in chunks to reduce Yahoo request failures.
    Returns dict[yf_ticker] -> OHLCV frame.
    """
    result: Dict[str, pd.DataFrame] = {}
    chunk_size = 40

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i : i + chunk_size]
        print(f"[download] {i+1}-{min(i+chunk_size, len(tickers))}/{len(tickers)}")
        try:
            raw = yf.download(
                tickers=chunk,
                start=start,
                end=end,
                auto_adjust=False,
                group_by="ticker",
                threads=True,
                progress=False,
                actions=False,
                timeout=30,
            )
        except Exception as e:
            print(f"[warn] chunk download failed: {e}")
            raw = pd.DataFrame()

        for t in chunk:
            try:
                if len(chunk) == 1 and not isinstance(raw.columns, pd.MultiIndex):
                    df = raw.copy()
                else:
                    df = raw[t].copy()
                df = normalize_ohlcv(df)
                if not df.empty:
                    result[t] = df
            except Exception:
                pass

        # Fallback per ticker for anything missed.
        missing = [t for t in chunk if t not in result]
        for t in missing:
            try:
                df = yf.download(
                    t,
                    start=start,
                    end=end,
                    auto_adjust=False,
                    progress=False,
                    actions=False,
                    timeout=20,
                )
                df = normalize_ohlcv(df)
                if not df.empty:
                    result[t] = df
            except Exception as e:
                print(f"[warn] {t}: {e}")

        time.sleep(0.25)

    return result


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        # Single-ticker yfinance may still return a MultiIndex in some versions.
        out.columns = out.columns.get_level_values(0)

    wanted = ["Open", "High", "Low", "Close", "Volume"]
    if not all(c in out.columns for c in wanted):
        return pd.DataFrame()

    out = out[wanted].copy()
    for c in wanted:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out = out[~out.index.duplicated(keep="last")]
    out.index = pd.to_datetime(out.index).tz_localize(None)
    return out.sort_index()


def cci(df: pd.DataFrame, period: int = 9) -> pd.Series:
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    sma = tp.rolling(period, min_periods=period).mean()
    mad = tp.rolling(period, min_periods=period).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True
    )
    denom = 0.015 * mad.replace(0, np.nan)
    return (tp - sma) / denom


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    # Wilder smoothing is equivalent to EMA alpha=1/period.
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def dmi_adx(df: pd.DataFrame, period: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    high, low, close = df["High"], df["Low"], df["Close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index,
    )

    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = wilder_smooth(tr, period)
    plus_di = 100.0 * wilder_smooth(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100.0 * wilder_smooth(minus_dm, period) / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = wilder_smooth(dx, period)
    return plus_di, minus_di, adx


def cvd_proxy(df: pd.DataFrame) -> pd.DataFrame:
    """
    Daily OHLCV CVD proxy, NOT true bid/ask CVD.

    A positive day (close > open) assigns positive volume,
    a negative day assigns negative volume.
    """
    direction = np.sign(df["Close"] - df["Open"])
    signed_volume = df["Volume"].fillna(0.0) * direction
    cvd = signed_volume.cumsum()
    return pd.DataFrame(
        {
            "signed_volume_proxy": signed_volume,
            "cvd_proxy": cvd,
        },
        index=df.index,
    )


def add_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    x = df.copy()
    x["cci"] = cci(x, cfg.cci_period)
    x["plus_di"], x["minus_di"], x["adx"] = dmi_adx(x, cfg.dmi_period)

    cvd = cvd_proxy(x)
    x = x.join(cvd)
    x["cvd_ema"] = x["cvd_proxy"].ewm(
        span=cfg.cvd_ema_period, adjust=False
    ).mean()
    x["cvd_slope"] = x["cvd_proxy"].diff(cfg.cvd_slope_period)

    # CCI baseline cross.
    x["cci_cross_up"] = (x["cci"] > 0) & (x["cci"].shift(1) <= 0)
    x["cci_cross_down"] = (x["cci"] < 0) & (x["cci"].shift(1) >= 0)

    # DMI trend filter.
    x["dmi_bull"] = (
        (x["plus_di"] > x["minus_di"]) &
        (x["adx"] >= cfg.adx_threshold)
    )
    x["dmi_bear"] = x["minus_di"] > x["plus_di"]

    # CVD confirmation.
    x["cvd_bull"] = (
        (x["cvd_slope"] > 0) &
        (x["cvd_proxy"] > x["cvd_ema"])
    )
    x["cvd_bear"] = (
        (x["cvd_slope"] < 0) &
        (x["cvd_proxy"] < x["cvd_ema"])
    )

    return x


def make_stateful_signal(x: pd.DataFrame, variant: str) -> pd.DataFrame:
    """
    Long-only state machine.

    Variants:
      cci_dmi:
        enter: CCI crosses above 0 AND bullish DMI/ADX
        exit : CCI crosses below 0 OR bearish DMI

      cci_dmi_cvd_filter:
        same entry plus CVD confirmation

      cci_dmi_cvd_sizing:
        entry is CCI+DMI; CVD controls target weight:
        - CVD confirmed: score 1.0
        - otherwise:     score 0.5
        exits same as cci_dmi
    """
    out = x.copy()
    state = 0.0
    targets = []

    for _, r in out.iterrows():
        if state > 0:
            if bool(r.get("cci_cross_down", False)) or bool(r.get("dmi_bear", False)):
                state = 0.0
            elif variant == "cci_dmi_cvd_sizing":
                state = 1.0 if bool(r.get("cvd_bull", False)) else 0.5
        else:
            base_entry = bool(r.get("cci_cross_up", False)) and bool(r.get("dmi_bull", False))
            if variant == "cci_dmi":
                if base_entry:
                    state = 1.0
            elif variant == "cci_dmi_cvd_filter":
                if base_entry and bool(r.get("cvd_bull", False)):
                    state = 1.0
            elif variant == "cci_dmi_cvd_sizing":
                if base_entry:
                    state = 1.0 if bool(r.get("cvd_bull", False)) else 0.5
            else:
                raise ValueError(f"Unknown variant: {variant}")

        targets.append(state)

    out["raw_target"] = targets

    # Signal at close t becomes exposure for day t+1.
    out["target"] = out["raw_target"].shift(1).fillna(0.0)
    out["ret"] = out["Close"].pct_change().fillna(0.0)
    return out


def build_panel(
    universe: pd.DataFrame,
    prices: Dict[str, pd.DataFrame],
    cfg: Config,
    variant: str,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build daily portfolio weights and per-symbol diagnostic rows.
    Cross-sectional score is used to cap concurrent holdings.
    """
    target_series = {}
    score_series = {}
    diagnostics = []

    name_map = dict(zip(universe["yf_ticker"], universe["name"]))
    code_map = dict(zip(universe["yf_ticker"], universe["ticker"]))

    for yf_ticker, df in prices.items():
        if len(df) < cfg.min_history_days:
            continue
        x = add_indicators(df, cfg)
        x = make_stateful_signal(x, variant)

        # Ranking only matters when more names signal than max_positions.
        # Prefer stronger trend + stronger CCI + positive CVD slope.
        adx_rank = x["adx"].clip(0, 60) / 60.0
        cci_rank = x["cci"].clip(-200, 200) / 200.0
        vol_scale = x["Volume"].rolling(20).mean().replace(0, np.nan)
        cvd_rank = (x["signed_volume_proxy"] / vol_scale).clip(-3, 3) / 3.0
        x["rank_score"] = (
            0.45 * adx_rank.fillna(0)
            + 0.35 * cci_rank.fillna(0)
            + 0.20 * cvd_rank.fillna(0)
        )

        target_series[yf_ticker] = x["target"]
        score_series[yf_ticker] = x["rank_score"]

        tmp = x[
            [
                "Close", "cci", "plus_di", "minus_di", "adx",
                "cvd_proxy", "cvd_slope", "cvd_bull", "target"
            ]
        ].copy()
        tmp["yf_ticker"] = yf_ticker
        tmp["ticker"] = code_map.get(yf_ticker, "")
        tmp["name"] = name_map.get(yf_ticker, yf_ticker)
        tmp["date"] = tmp.index
        diagnostics.append(tmp.reset_index(drop=True))

    if not target_series:
        raise RuntimeError("No valid price histories were downloaded.")

    targets = pd.DataFrame(target_series).sort_index().fillna(0.0)
    scores = pd.DataFrame(score_series).reindex(targets.index).fillna(-999.0)

    # Select up to max_positions each day.
    selected = pd.DataFrame(0.0, index=targets.index, columns=targets.columns)
    for dt in targets.index:
        active = targets.loc[dt]
        active = active[active > 0]
        if active.empty:
            continue

        rank = scores.loc[dt, active.index].sort_values(ascending=False)
        chosen = rank.head(cfg.max_positions).index
        raw = targets.loc[dt, chosen].astype(float)

        # Equal-weight, but sizing variant retains 1.0 vs 0.5 relative strength.
        denom = raw.sum()
        if denom > 0:
            selected.loc[dt, chosen] = raw / denom

    diag = pd.concat(diagnostics, ignore_index=True) if diagnostics else pd.DataFrame()
    return selected, diag


def calculate_returns(
    weights: pd.DataFrame,
    prices: Dict[str, pd.DataFrame],
    cfg: Config,
) -> pd.DataFrame:
    closes = {}
    for t in weights.columns:
        if t in prices:
            closes[t] = prices[t]["Close"]
    close_df = pd.DataFrame(closes).reindex(weights.index).ffill()

    asset_returns = close_df.pct_change().fillna(0.0)

    # Today's weights are already shifted from yesterday's close signal.
    gross = (weights * asset_returns).sum(axis=1)

    turnover = weights.diff().abs().sum(axis=1).fillna(weights.abs().sum(axis=1))
    cost_rate = (cfg.commission_bps + cfg.slippage_bps) / 10000.0
    costs = turnover * cost_rate
    net = gross - costs

    out = pd.DataFrame(
        {
            "gross_return": gross,
            "turnover": turnover,
            "cost": costs,
            "net_return": net,
        }
    )
    out["equity"] = (1.0 + out["net_return"]).cumprod()
    out["gross_equity"] = (1.0 + out["gross_return"]).cumprod()
    out["n_positions"] = (weights > 0).sum(axis=1)
    return out


def performance_stats(ret: pd.Series, turnover: pd.Series) -> Dict[str, float]:
    ret = ret.dropna()
    if ret.empty:
        return {}

    equity = (1 + ret).cumprod()
    n = len(ret)
    years = n / 252.0
    total_return = equity.iloc[-1] - 1.0
    cagr = equity.iloc[-1] ** (1.0 / years) - 1.0 if years > 0 else np.nan
    ann_vol = ret.std(ddof=1) * np.sqrt(252)
    sharpe = (ret.mean() * 252) / ann_vol if ann_vol and ann_vol > 0 else np.nan
    drawdown = equity / equity.cummax() - 1.0
    max_dd = drawdown.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else np.nan

    return {
        "days": int(n),
        "total_return": float(total_return),
        "CAGR": float(cagr),
        "annual_volatility": float(ann_vol),
        "Sharpe_0rf": float(sharpe),
        "max_drawdown": float(max_dd),
        "Calmar": float(calmar),
        "avg_daily_turnover": float(turnover.mean()),
        "annualized_turnover_approx": float(turnover.mean() * 252),
        "positive_day_ratio": float((ret > 0).mean()),
    }


def fetch_benchmark(start: str, end: str, ticker: str) -> pd.Series:
    try:
        x = yf.download(
            ticker,
            start=start,
            end=end,
            auto_adjust=False,
            progress=False,
            actions=False,
            timeout=30,
        )
        x = normalize_ohlcv(x)
        if not x.empty:
            return x["Close"].pct_change().fillna(0.0)
    except Exception as e:
        print(f"[warn] benchmark download failed: {e}")
    return pd.Series(dtype=float)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--top-n", type=int, default=200)
    p.add_argument("--cci-period", type=int, default=9)
    p.add_argument("--dmi-period", type=int, default=14)
    p.add_argument("--adx-threshold", type=float, default=20.0)
    p.add_argument("--cvd-slope-period", type=int, default=5)
    p.add_argument("--cvd-ema-period", type=int, default=10)
    p.add_argument("--commission-bps", type=float, default=1.5)
    p.add_argument("--slippage-bps", type=float, default=3.0)
    p.add_argument("--max-positions", type=int, default=30)
    p.add_argument("--output-dir", default="results")
    args = p.parse_args()

    cfg = Config(
        years=args.years,
        top_n=args.top_n,
        cci_period=args.cci_period,
        dmi_period=args.dmi_period,
        adx_threshold=args.adx_threshold,
        cvd_slope_period=args.cvd_slope_period,
        cvd_ema_period=args.cvd_ema_period,
        commission_bps=args.commission_bps,
        slippage_bps=args.slippage_bps,
        max_positions=args.max_positions,
        output_dir=args.output_dir,
    )

    outdir = Path(cfg.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    end_dt = datetime.now() + timedelta(days=1)
    start_dt = end_dt - timedelta(days=int(cfg.years * 365.25) + 90)
    start = start_dt.strftime("%Y-%m-%d")
    end = end_dt.strftime("%Y-%m-%d")

    print("[1/5] Getting current KOSPI market-cap top universe...")
    universe = current_kospi_top_n(cfg.top_n)
    universe.to_csv(outdir / "universe_current_top200.csv", index=False, encoding="utf-8-sig")
    print(universe.head(10).to_string(index=False))

    print("[2/5] Downloading OHLCV...")
    prices = download_ohlcv(universe["yf_ticker"].tolist(), start, end)
    print(f"Downloaded {len(prices)}/{len(universe)} symbols")

    variants = [
        "cci_dmi",
        "cci_dmi_cvd_filter",
        "cci_dmi_cvd_sizing",
    ]

    benchmark_ret = fetch_benchmark(start, end, cfg.benchmark)
    summary_rows = []

    print("[3/5] Running strategy variants...")
    for variant in variants:
        print(f"  - {variant}")
        weights, diag = build_panel(universe, prices, cfg, variant)
        portfolio = calculate_returns(weights, prices, cfg)

        # Trim warmup to requested years.
        cutoff = pd.Timestamp(datetime.now() - timedelta(days=int(cfg.years * 365.25)))
        portfolio = portfolio.loc[portfolio.index >= cutoff]
        weights = weights.reindex(portfolio.index).fillna(0.0)

        stats = performance_stats(portfolio["net_return"], portfolio["turnover"])
        stats["variant"] = variant
        stats["avg_positions"] = float(portfolio["n_positions"].mean())
        stats["max_positions_observed"] = int(portfolio["n_positions"].max())
        summary_rows.append(stats)

        portfolio.to_csv(outdir / f"portfolio_{variant}.csv", encoding="utf-8-sig")
        weights.to_csv(outdir / f"weights_{variant}.csv", encoding="utf-8-sig")

        # Diagnostics can be large; save one combined file per variant.
        if not diag.empty:
            diag = diag[diag["date"] >= cutoff]
            diag.to_csv(
                outdir / f"signals_{variant}.csv",
                index=False,
                encoding="utf-8-sig",
            )

    print("[4/5] Benchmark...")
    if not benchmark_ret.empty:
        benchmark_ret = benchmark_ret.loc[benchmark_ret.index >= cutoff]
        bstats = performance_stats(
            benchmark_ret,
            pd.Series(0.0, index=benchmark_ret.index),
        )
        bstats["variant"] = "KOSPI_benchmark"
        summary_rows.append(bstats)
        pd.DataFrame(
            {
                "return": benchmark_ret,
                "equity": (1 + benchmark_ret).cumprod(),
            }
        ).to_csv(outdir / "benchmark_kospi.csv", encoding="utf-8-sig")

    summary = pd.DataFrame(summary_rows)
    cols = ["variant"] + [c for c in summary.columns if c != "variant"]
    summary = summary[cols]
    summary.to_csv(outdir / "summary.csv", index=False, encoding="utf-8-sig")
    print("\n=== SUMMARY ===")
    print(summary.to_string(index=False))

    with open(outdir / "config.json", "w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, ensure_ascii=False, indent=2)

    note = {
        "universe_definition": "Current KOSPI market-cap top N; survivorship bias intentionally not corrected.",
        "cvd_definition": "Daily OHLCV proxy: cumulative volume * sign(close-open). This is NOT true bid/ask CVD.",
        "signal_timing": "Indicators use day t close; target exposure is shifted by one day.",
        "transaction_costs": "Turnover * (commission_bps + slippage_bps).",
    }
    with open(outdir / "methodology.json", "w", encoding="utf-8") as f:
        json.dump(note, f, ensure_ascii=False, indent=2)

    print("[5/5] Done. Results written to:", outdir.resolve())


if __name__ == "__main__":
    main()
