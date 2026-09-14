# Phase 3 — valve control on the console API

**Date:** 2026-09-11
**Status:** NOT implemented. Blocked on live testing that needs a human watching the hardware.

## Why this is stopped here

The console API exposes a valve write:

```
POST http://iotcloud.tagmeter.com:8099/prod-api/meter/updateValveStatus
     {"address": "68753500170871", "valveFlag": 55}
```

`valveFlag` is **numeric**, where the documented API's `valve_control` takes
`forceValve` as a **string**. Two different command vocabularies for the same
hardware.

Mapping the numeric values means sending real downlinks to real valves on a
production water network and watching what happens. That is not something to do
unattended, so this phase is written down rather than built.

## Why it matters more than it looks

The README names one unresolved question as the most important in the project:

> **Consequence: the protocol's `44H` meter-controlled/prepaid mode is not
> reachable through `valve_control`.** That mode is what makes a Quinto meter
> manage its own valve against its credit balance — the entire reason those ten
> meters exist.

`forceValve` was probed across 43 candidate values and rejected `44`, `44H`,
`0x44`, `55`, `99`, `Prepaid`, `MeterControlled` and the rest. But the console
API accepts `valveFlag: 55` — a value the documented API refuses. So the two
APIs do not share a command set, and the mode that `forceValve` cannot reach may
well be reachable here.

Supporting evidence from the console's own meter detail for `68753500170871`:

```
icCardMeter.valveStatus  = "强制开阀"      (forced open valve)
icCardMeter.chargeStatus = "远程充值"      (remote recharge)
chargeInfo.valveFlag     = "手动"          (manual)
basicInfo.valveFlag      = "关闭"          (closed)
basicInfo.paymentType    = "预付费"        (prepaid)
```

Two distinct `valveFlag` fields with different values, and a separate prepaid
block carrying a "forced open" state. The vocabulary for meter-controlled mode
is visible in this API; it is simply not mapped.

## What has to happen first

**1. Confirm whether `Upande` is a shared account.** Every read so far works
with the service credentials, but nobody has checked whether a human logging
into the console evicts the app's token. The documented API behaves exactly
that way — it is open question 4 in the README, and the answer there was yes.
If this API behaves the same, valve control must not go through a shared
account: the app and a browser would fight, and the loser is a valve that does
not move.

Test: hold a service token, log into the console in a browser, then reuse the
token. If it 401s, a dedicated service account becomes a hard prerequisite.

**2. Get the vendor's blessing.** This is an undocumented internal API over
plain HTTP. Using it for reads is a calculated risk on data we can re-fetch.
Using it to actuate water valves is a different proposition: it can change
without notice, and there is no transport security on the command. Ask
explicitly before production valve control depends on it.

**3. Map `valveFlag` with someone watching.** The safe procedure, on one meter,
in a session where the valve is observable:

- Note which console button produced `55`. The capture that revealed it was an
  **Open**, and the valve did open, so `55` is *probably* force-open — probably
  is not good enough for a write path.
- Find the Close value the same way, from the console's own traffic, rather than
  by guessing numbers at live hardware.
- Only then consider whether a third value reaches `44H` meter-controlled mode,
  and confirm it by reading back `icCardMeter.valveStatus` rather than by
  inferring from the valve's position.

**Do not brute-force `valveFlag`.** The documented API tolerated 43 rejected
strings because rejection was free. Here the values are numeric and adjacent,
an unknown one may map to something with consequences — the documented API's own
`Reset` is accepted and its semantics are still unknown — and every attempt is a
real downlink.

## Questions for the vendor

1. May we use `prod-api` for production integration, or is it internal-only?
2. Can we have a **dedicated service account**, separate from the human console
   logins, on both APIs?
3. What are the accepted `valveFlag` values on `updateValveStatus`, and which
   one sets the `44H` meter-controlled/prepaid mode?
4. Why is `prod-api` plain HTTP on port 8099? Is there a TLS endpoint?
5. `get_freeze_record` returns `code 500, "Failed checking Authorization Token,
   Freeze Records meterID NOT fetched"` on a token that works for every other
   read. What does that endpoint need?
6. Is the ~2m04s gap between `meterTime` and `createTime` a fixed batch cycle,
   and is it configurable?

## What is already done

Phase 1 (`e1cd024`, `ff43572`) corrected the timing model and made the dashboard
watch a command confirm. Phase 2 (`51ac858`) added the read-only console client.
Neither depends on this phase, and both are safe to run today.
