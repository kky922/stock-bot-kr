# PROPOSE: KIS Shared Token Cache Fix

**Date:** 2026-05-26
**Author:** Hermes Agent (closed-loop cron job #214b45852ad9)

## Problem

KIS API token expires on server side after ~18.5h (verified by log timestamps), but the shared class-level token cache (`KISAPI._shared_access_token`) retains the expired token for 23h per code's expiry calculation (`time.time() + 82800`). 

When EGW00123 is detected:
1. Instance token (`self._access_token`) is cleared (line 130-131)
2. `_get_access_token()` is called (line 133)
3. It checks shared class token → `KISAPI._shared_access_token` is NOT empty + `now < KISAPI._shared_token_expires` (still 23h window) → **returns the expired token**
4. Balance endpoint retry fails again with EGW00123
5. Repeats indefinitely — tokenP endpoint is **never actually called** to refresh

**Evidence:** 
- Last successful token: 2026-05-25 13:48 KST
- First failure: 2026-05-26 08:23 KST (~18.6h later)
- Current time: 12:20 KST — shared cache still "valid" until 12:48 KST
- 82 consecutive EGW00123 errors today, ZERO tokenP refresh attempts logged
- Historical: Rate limit (EGW00133) errors DID show tokenP call logs — proving the cache path is bypassing tokenP

## Fix

When EGW00123 is detected, also clear `KISAPI._shared_access_token` and `KISAPI._shared_token_expires`, forcing `_get_access_token()` to call the `tokenP` endpoint for a fresh token.

## Impact

- **Scope:** Paper trading (virtual mode). Identical logic exists for real mode.
- **Reversibility:** Trivial revert of 2 lines.
- **Risk:** None — worst case, tokenP fails (HTTP 500) and returns failure as before.
- **Verification required:** Bot restart (PID 6647) to pick up code change.

## Rollback

```bash
git checkout -- kis_api.py
```

## Observation Metric

After restart + next pipeline run:
- `agent_system.log` should show "✅ KIS 접근토큰 발급 성공" 
- Balance sync status should return to "ok"