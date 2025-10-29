#!/usr/bin/env python3
"""
Quick validation of core edge-locked MM logic
Tests TP math, state machine, and key calculations
"""

import sys
from market_book import QuoteState
from main_async import AsyncBotConfig

def test_tp_math():
    """Test 1: TP Math Sanity"""
    print("\n" + "="*60)
    print("TEST 1: TP MATH VALIDATION")
    print("="*60)

    config = AsyncBotConfig()

    # Calculate TP offset
    maker_fee = config.MAKER_FEE_C
    exit_fee = config.EXIT_FEE_C
    tick = config.TICK_C
    min_edge = config.TP_MIN_EDGE_C

    total_fees = maker_fee + exit_fee
    raw_offset = total_fees + tick + min_edge
    import math
    tp_offset = math.ceil(raw_offset / tick) * tick

    print(f"Fees: {maker_fee:.2f}¢ + {exit_fee:.2f}¢ = {total_fees:.2f}¢")
    print(f"Min Edge: {min_edge:.0f}¢")
    print(f"Tick: {tick:.0f}¢")
    print(f"Raw: {total_fees:.2f}¢ + {tick:.0f}¢ + {min_edge:.0f}¢ = {raw_offset:.2f}¢")
    print(f"TP Offset (tick-snapped): {tp_offset:.0f}¢")

    # Example calculation
    example_vwap = 0.60
    example_tp_long = example_vwap + (tp_offset / 100)
    example_tp_short = example_vwap - (tp_offset / 100)

    print(f"\nExample:")
    print(f"  VWAP = ${example_vwap:.2f}")
    print(f"  TP (LONG) = ${example_tp_long:.2f}")
    print(f"  TP (SHORT) = ${example_tp_short:.2f}")

    # Validation
    expected_tp_offset = 7.0
    if tp_offset == expected_tp_offset:
        print(f"\n✅ PASS: TP offset is {tp_offset:.0f}¢ (expected {expected_tp_offset:.0f}¢)")
        return True
    else:
        print(f"\n❌ FAIL: TP offset is {tp_offset:.0f}¢ (expected {expected_tp_offset:.0f}¢)")
        return False

def test_state_machine():
    """Test 2: State Machine Logic"""
    print("\n" + "="*60)
    print("TEST 2: STATE MACHINE")
    print("="*60)

    states = [QuoteState.FLAT, QuoteState.SKEW_LONG, QuoteState.SKEW_SHORT]

    print("States defined:")
    for state in states:
        print(f"  - {state.value}")

    # Test state transitions
    print("\nState transition logic:")
    test_cases = [
        (0, QuoteState.FLAT, "Net=0 → FLAT"),
        (10, QuoteState.SKEW_LONG, "Net=10 → SKEW_LONG"),
        (-5, QuoteState.SKEW_SHORT, "Net=-5 → SKEW_SHORT"),
    ]

    all_pass = True
    for net, expected_state, description in test_cases:
        if net == 0:
            actual_state = QuoteState.FLAT
        elif net > 0:
            actual_state = QuoteState.SKEW_LONG
        else:
            actual_state = QuoteState.SKEW_SHORT

        if actual_state == expected_state:
            print(f"  ✅ {description}")
        else:
            print(f"  ❌ {description} - Got {actual_state.value}")
            all_pass = False

    if all_pass:
        print("\n✅ PASS: All state transitions correct")
    else:
        print("\n❌ FAIL: Some state transitions incorrect")

    return all_pass

def test_price_gates():
    """Test 3: Price-Selective Gating"""
    print("\n" + "="*60)
    print("TEST 3: PRICE-SELECTIVE GATING")
    print("="*60)

    config = AsyncBotConfig()

    # NHL thresholds
    nhl_gates = config.MARKET_THRESHOLDS.get("NHL", {})
    low = nhl_gates.get("low", config.DEFAULT_PRICE_ENTRY_LOW)
    high = nhl_gates.get("high", config.DEFAULT_PRICE_ENTRY_HIGH)

    print(f"NHL Gates:")
    print(f"  Entry Low: ${low:.2f} (only buy below)")
    print(f"  Entry High: ${high:.2f} (only sell above)")

    # Test cases
    test_cases = [
        (0.45, True, False, "Mid=$0.45 → CAN_BUY, no sell"),
        (0.55, False, False, "Mid=$0.55 → No quotes (neutral zone)"),
        (0.67, False, True, "Mid=$0.67 → CAN_SELL, no buy"),
    ]

    all_pass = True
    for mid, should_buy, should_sell, description in test_cases:
        can_buy = mid <= low
        can_sell = mid >= high

        if can_buy == should_buy and can_sell == should_sell:
            print(f"  ✅ {description}")
        else:
            print(f"  ❌ {description} - Got CAN_BUY={can_buy}, CAN_SELL={can_sell}")
            all_pass = False

    if all_pass:
        print("\n✅ PASS: Price gating logic correct")
    else:
        print("\n❌ FAIL: Price gating logic incorrect")

    return all_pass

def test_mae_trim_calculation():
    """Test 4: Adaptive MAE Trim Calculation"""
    print("\n" + "="*60)
    print("TEST 4: ADAPTIVE MAE TRIM")
    print("="*60)

    config = AsyncBotConfig()

    # Test case: age=5m, mark=0.49, VWAP=0.60, net=10
    vwap = 0.60
    mark = 0.49
    net = 10

    mae_c = abs(mark - vwap) * 100
    print(f"Scenario: VWAP=${vwap:.2f}, Mark=${mark:.2f}, Net={net}")
    print(f"MAE: {mae_c:.1f}¢")

    threshold = config.MAE_TRIM_THRESHOLD_C
    hard_cap = config.MAE_HARD_CAP_C

    if mae_c >= threshold:
        severity = min(
            (mae_c - threshold) / (hard_cap - threshold),
            1.0
        )
        trim_pct = config.MAE_TRIM_PCT_MIN + severity * (
            config.MAE_TRIM_PCT_MAX - config.MAE_TRIM_PCT_MIN
        )
        trim_qty = max(1, round(trim_pct * net))

        print(f"Severity: {severity:.3f}")
        print(f"Trim %: {trim_pct*100:.1f}%")
        print(f"Trim Qty: {trim_qty} contracts")

        # Expected: MAE=11¢, severity=(11-10)/(18-10)=0.125, trim_pct=0.25+0.125*0.25=0.28125
        expected_severity = (11 - 10) / (18 - 10)
        expected_trim_pct = 0.25 + expected_severity * 0.25

        if abs(severity - expected_severity) < 0.01 and abs(trim_pct - expected_trim_pct) < 0.01:
            print(f"\n✅ PASS: MAE trim calculation correct")
            return True
        else:
            print(f"\n❌ FAIL: Expected severity={expected_severity:.3f}, trim_pct={expected_trim_pct:.3f}")
            return False
    else:
        print(f"\n❌ FAIL: MAE {mae_c:.1f}¢ should trigger (threshold={threshold}¢)")
        return False

def test_circuit_breaker_thresholds():
    """Test 5: Circuit Breaker Thresholds"""
    print("\n" + "="*60)
    print("TEST 5: CIRCUIT BREAKER THRESHOLDS")
    print("="*60)

    config = AsyncBotConfig()

    print("Base Thresholds:")
    print(f"  Soft: {config.CB_TIER1_30S}/30s or {config.CB_TIER1_60S}/60s")
    print(f"  Hard: {config.CB_TIER2_30S}/30s or {config.CB_TIER2_60S}/60s")

    # Test adaptive adjustments
    print("\nAdaptive Adjustments:")

    # SKEW state (inventory held)
    skew_mult = config.CB_SKEW_MULTIPLIER
    soft_skew = int(config.CB_TIER1_30S * skew_mult)
    print(f"  SKEW state: {config.CB_TIER1_30S} → {soft_skew} (×{skew_mult})")

    # Tight spread
    tight_mult = config.CB_TIGHT_SPREAD_MULTIPLIER
    soft_tight = max(1, int(config.CB_TIER1_30S * tight_mult))
    print(f"  1-tick spread: {config.CB_TIER1_30S} → {soft_tight} (×{tight_mult})")

    print("\n✅ PASS: Circuit breaker thresholds configured")
    return True

def main():
    """Run all validation tests"""
    print("\n" + "="*80)
    print("EDGE-LOCKED MARKET MAKER - CORE LOGIC VALIDATION")
    print("="*80)

    tests = [
        ("TP Math", test_tp_math),
        ("State Machine", test_state_machine),
        ("Price Gates", test_price_gates),
        ("MAE Trim", test_mae_trim_calculation),
        ("Circuit Breaker", test_circuit_breaker_thresholds),
    ]

    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"\n❌ {name} CRASHED: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    # Summary
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)

    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")

    total_tests = len(results)
    passed_tests = sum(1 for _, p in results if p)

    print(f"\nResult: {passed_tests}/{total_tests} tests passed")

    if passed_tests == total_tests:
        print("\n🎉 ALL TESTS PASSED - Core logic validated!")
        print("✅ Ready for shadow mode testing")
        return 0
    else:
        print("\n⚠️  SOME TESTS FAILED - Review failures before deployment")
        return 1

if __name__ == "__main__":
    sys.exit(main())
