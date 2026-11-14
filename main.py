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

Unreadable input and degenerate markets fall back to the last close, and every
case still gets an output file: a case with no usable price at all is written
with the last finite price found anywhere in it, or 1.0 when there is none.
A bad case never stops the run.
"""

import glob
import json
import os
import sys

MOMENTUM_MINUTES = 1440  # one day of one-minute closes
MOMENTUM_CLIP = 300.0  # dollars of 24h move the model is willing to act on
REVERSAL = 0.30  # share of the clipped move expected to revert
MAX_PRICE = 9_999_999.99999  # largest price below the scorer's 10,000,000 cap
FALLBACK_PRICE = 1.0  # last resort for a case holding no usable positive price


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


def last_case_price(case):
    """The last finite price in any "prices" list anywhere in the case, or None.

    Walks the whole structure in document order so the latest series wins; this
    is the price a degenerate case falls back to.
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            value = last_finite(node.get("prices"))
            if value is not None:
                found.append(value)
            for key, child in node.items():
                if key != "prices":
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(case)
    return found[-1] if found else None


def fallback_price(case):
    """A positive finite forecast for a case with no usable checkpoint close.

    Uses the last finite price anywhere in the case when it is positive, and
    1.0 otherwise, so the written price is always positive and finite.
    """
    value = last_case_price(case)
    if value is not None and value > 0.0:
        return min(value, MAX_PRICE)
    return FALLBACK_PRICE


def checkpoint_series(case):
    """The price series and its checkpoint close, or (None, None).

    Uses the first market that has a usable close, so an empty market list or a
    market missing "prices" cannot crash the forecast.
    """
    markets = case.get("markets") if isinstance(case, dict) else None
    if not isinstance(markets, list):
        return None, None

    for market in markets:
        if isinstance(market, dict):
            prices = market.get("prices")
            close = last_finite(prices)
            if close is not None:
                return prices, close
    return None, None


def forecast_price(case):
    """The case's forecast; never None, so every case gets an output file.

    A case with no usable close at all falls back to the last finite price
    anywhere in it, or 1.0 when the case holds no positive finite price.
    """
    prices, close = checkpoint_series(case)
    if close is None:
        return fallback_price(case)

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
            print("note %s: unreadable case (%s); using fallback" % (stem, exc))
            case = None

        case_id = case_id_of(case, stem)
        try:
            price = forecast_price(case)
        except Exception as exc:  # noqa: BLE001 - a bad case must not stop the run
            print("note %s: forecast failed (%s); using fallback" % (stem, exc))
            price = FALLBACK_PRICE
        if price is None:
            # A missing output is an invalid case, so never leave one unwritten.
            price = FALLBACK_PRICE

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
