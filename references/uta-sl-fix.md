# UTA Stop-Loss Fix — 2026-05-21

## Symptom
Error 170130: `"Data sent for paramter '' is not valid."` on stop-loss order creation.

Also: `"trigger order does not support triggerDirection for spot markets yet"`

## Root Cause
Bybit's **Unified Trading Account (UTA)** handles stop-loss orders differently:

| Param | Non-UTA | UTA (spot) |
|---|---|---|
| `triggerDirection` | Required (1=above, 2=below) | ❌ **Not supported** — returns error |
| `positionIdx` | Optional | Required: set to `0` |
| `reduceOnly` | Common for futures | ❌ **Not for spot** — use `category: "spot"` instead |

The direction of the trigger is **implicit** from the order side:
- **sell** + `triggerPrice` below market = stop-loss
- **buy** + `triggerPrice` above market = stop-entry

## Fix
Remove `triggerDirection` from `create_stop_loss_order()` in `exchange.py`.

**Before:**
```python
{
    "triggerPrice": stop_price,
    "triggerDirection": 2,  # ❌ CAUSES ERROR 170130 on UTA spot
    "triggerBy": "last",
    "category": "spot",
    "positionIdx": 0,
}
```

**After:**
```python
{
    "triggerPrice": stop_price,
    # No triggerDirection — implicit from sell side
    "triggerBy": "last",
    "category": "spot",
    "positionIdx": 0,
}
```

## Verification
After fix: `create_stop_loss_order()` returns order ID successfully.
On exchange: open orders show trigger order with status "open".

## Also Affects
`create_market_buy()` — uses `marketUnit: "quoteCoin"` (NOT `quoteOrderQty`).
This was already correct but the error 170130 on buy at 11:00 may have been due to a transient API issue during the restart chaos.
