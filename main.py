#!/usr/bin/env python3
"""make.fun btc-cent-forecast-01 forecaster.

Guesses the BTC/USDC close 240 minutes after each checkpoint as persistence plus
a faded 24-hour momentum term:

    m = c - prices[-1 - 1440]           # 24-hour move, in dollars
    price = c - 0.30 * m                # 30% of the move expected to revert

The clip on `m` at +/-300 dollars is what makes that fade safe. 0.30 is fitted
to ordinary daily swings, where four hours of partial reversion is a fair bet;
past $300 the move is more likely a regime change than noise, and extrapolating
it would stake a large contrarian bet on a trend still running. Clipping bounds
the adjustment at $90 while leaving normal days untouched.

Unreadable input and degenerate markets fall back to the last close, and a bad
case never stops the run.
"""

import glob
import json
import os
import sys

MOMENTUM_MINUTES = 1440  # one day of one-minute closes
MOMENTUM_CLIP = 300.0  # dollars of 24h move the model is willing to act on
REVERSAL = 0.30  # share of the clipped move expected to revert
MAX_PRICE = 9_999_999.99999  # largest price below the scorer's 10,000,000 cap


def is_price(value):
    """True for a usable finite price: a real number, not a bool, not inf/nan."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value
        and -1e18 < value < 1e18
    )


def last_finite(prices):
    """The last finite number in a price list, or None."""
    if not isinstance(prices, list):
        return None
    for value in reversed(prices):
        if is_price(value):
            return float(value)
    return None


def checkpoint_series(case):
    """The series and checkpoint close to forecast from, or (None, fallback).

    Uses the first market that has a usable close, so an empty market list or a
    market missing "prices" cannot crash the forecast. If no market has one, the
    last finite price seen anywhere in the case comes back as the fallback.
    """
    markets = case.get("markets") if isinstance(case, dict) else None
    if not isinstance(markets, list):
        return None, None

    fallback = None
    for market in markets:
        if not isinstance(market, dict):
            continue
        prices = market.get("prices")
        close = last_finite(prices)
        if close is not None:
            fallback = close
            break

    if fallback is None:
        for market in markets:
            if isinstance(market, dict):
                close = last_finite(market.get("prices"))
                if close is not None:
                    fallback = close
        return None, fallback
    return prices, fallback


def forecast_price(case):
    """The case's forecast, or None when the case holds no usable price."""
    prices, close = checkpoint_series(case)
    if close is None:
        return None

    price = close
    if prices is not None and len(prices) >= MOMENTUM_MINUTES + 1:
        latest = prices[-1]
        earlier = prices[-1 - MOMENTUM_MINUTES]
        if is_price(latest) and is_price(earlier):
            momentum = float(latest) - float(earlier)
            if momentum > MOMENTUM_CLIP:
                momentum = MOMENTUM_CLIP
            elif momentum < -MOMENTUM_CLIP:
                momentum = -MOMENTUM_CLIP
            candidate = float(latest) - REVERSAL * momentum
            if is_price(candidate) and candidate > 0.0:
                price = candidate

    if price > MAX_PRICE:
        price = MAX_PRICE
    if price <= 0.0:
        price = close if close > 0.0 else None
    return price


def write_forecast(output_dir, case_id, price):
    """Write one case's .out in a single fully built buffer, then rename it."""
    payload = json.dumps(
        {"forecasts": [{"case": case_id, "price": price}]},
        allow_nan=False,
        separators=(",", ":"),
    )
    data = memoryview((payload + "\n").encode("utf-8"))
    final_path = os.path.join(output_dir, case_id + ".out")
    temp_path = final_path + ".tmp"

    handle = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        while data:
            data = data[os.write(handle, data):]
        os.fsync(handle)
    finally:
        os.close(handle)
    os.replace(temp_path, final_path)


def case_id_of(case, stem):
    """The id of a case: its file's stem, falling back to the JSON `case` field.

    The grader pairs /output/<id>.out with /input/<id>.in, so the stem decides
    the file name; the two agree in every real case.
    """
    if stem:
        return stem
    if isinstance(case, dict):
        value = case.get("case")
        if isinstance(value, str) and value and os.sep not in value and value not in (".", ".."):
            return value
    return "case"


def main():
    input_dir = os.environ.get("MAKEFUN_INPUT_DIR") or "/input"
    output_dir = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output"

    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        print("cannot create output dir %s: %s" % (output_dir, exc))
        return 0

    paths = sorted(glob.glob(os.path.join(input_dir, "*.in")))
    written = 0
    for path in paths:
        stem = os.path.basename(path)[: -len(".in")]
        try:
            with open(path, "r", encoding="utf-8") as handle:
                case = json.load(handle)
        except (OSError, ValueError) as exc:
            print("skip %s: unreadable case (%s)" % (stem, exc))
            continue

        case_id = case_id_of(case, stem)
        try:
            price = forecast_price(case)
        except Exception as exc:  # noqa: BLE001 - a bad case must not stop the run
            print("skip %s: forecast failed (%s)" % (stem, exc))
            continue
        if price is None:
            print("skip %s: no usable price" % stem)
            continue

        try:
            write_forecast(output_dir, case_id, price)
        except (OSError, ValueError) as exc:
            print("skip %s: cannot write output (%s)" % (stem, exc))
            continue
        written += 1
        print("%s: %r" % (case_id, price))

    print("%d case(s) written to %s" % (written, output_dir))
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - never exit non-zero
        print("unexpected failure: %s" % exc, file=sys.stderr)
    sys.exit(0)
