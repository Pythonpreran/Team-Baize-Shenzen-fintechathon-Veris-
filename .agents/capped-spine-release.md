# Capped-spine release summary

## Approval and scope

The capped-spine review returned **APPROVED** for the T10–T17 local demo spine, with T16 explicitly deferred. The approved flow carries an owner from cited portfolio evidence through a permissioned synthetic claim, a disputed settlement, a tenant-bound correction receipt, and local audit verification.

The review confirmed that tier-2 confirmations expose canonical, hash-bound owner action terms before step-up; corrected funds and receipts remain tenant-isolated; reconciliation verifies a receipt's amount, correction reference, and beneficiary destination rather than trusting a claim field; and deletion of mutable history fails closed against a separate local anchor. These are local demo safeguards and do not claim external or blockchain durability.

Final shared review evidence: the independently rerun focused suite passed 57 tests; the DOM smoke suite passed 63/63 checks; repository scan, i18n, notices, and diff-hygiene checks passed.

## T16 limitation — India Motor Vehicles Act section 196

T16 is **deferred**, not complete. `IN-STAT-MVA-196` remains `CANDIDATE` without a verified operative primary-source quote. A human must verify a current India Code provision or an official amendment Gazette, record the operative quote and provenance, and run the pack-source verifier before any penalty output can be enabled.

Until that evidence exists, the product safely withholds the penalty: `motor.third_party_alert()` returns `penalty: None`, and the plan-layout card shows a withholding notice. It must not display a penalty figure, date, or legal consequence for section 196.
