# Fixtures — Hock Lee Foods Pte Ltd

**These are synthetic documents for a fictional business.** They exist so the
engine has operands to compute against. They must be labelled as synthetic on
screen in any demo, every time.

The only real document in the pipeline is the **base wording**:
`wordings/chubb-selectplus-wording.pdf` — Chubb Select+ Insurance Policy, Policy
Wording, Singapore, published 07/2020, publicly downloadable. Every clause the
engine cites comes from that document. The fixtures below supply the *schedule*
and the *non-insurance documents*, which no public source can provide for a
fictional company.

## Why fixtures are needed at all

A base wording alone produces no findings. Every rule needs at least one operand
from a schedule or a filed document:

- `EXCL.THEFT_FORCIBLE_ENTRY` needs to know whether the Schedule elected
  Additional Extension 2.17. Base wording alone cannot say.
- `ADEQ.AVERAGE_CONDITION` needs a sum insured (schedule) and a stock value
  (accounts).
- `WICA-HEADCOUNT-UNDERDECLARED` needs a WIC schedule and a CPF record.
- `COND.ALTERATION_NOTICE` needs the schedule's premises description and the
  tenancy permitted use.

## The persona, and the defects planted in it

Hock Lee Foods Pte Ltd — small food manufacturer, Tai Seng, leased industrial
unit. 14 staff, 9 doing manual work, 4 on Work Permits. Supplies two supermarket
chains and a hotel group.

Defects planted deliberately, each mapping to a taxonomy code:

| Planted defect | Detected by | Evidence class |
|---|---|---|
| Schedule does not elect Full Theft | `EXCL.THEFT_FORCIBLE_ENTRY` | PROVEN |
| WIC schedule says 11 employees, CPF record shows 14 | `WIC.DECLARED_HEADCOUNT` vs `CPF.CONTRIBUTOR_COUNT` | PROVEN |
| Second unit occupied, not on schedule, no endorsement | `COND.ALTERATION_NOTICE` | PROVEN |
| Stock sum insured S$100,000, accounts show S$150,000 | `ADEQ.AVERAGE_CONDITION` | DECLARED (stock is self-authored) |
| Buildings value never assessed | `RCA.BUILDING_VALUE` absent | UNESTABLISHED |
| Premium paid day 71 of the policy period | `COND.PREMIUM_PAYMENT_WARRANTY` | PROVEN |
| Endorsement page 2 deliberately illegible | abstention gate | INFORMATION_MISSING |

The last row matters: one fixture is intentionally broken so the abstention gate
can be shown firing on stage. Demo beat 5 depends on it.

## Files

- `hock-lee-schedule.json` — the policy schedule
- `hock-lee-endorsement-01.json` — an endorsement that does *not* cure the alteration
- `hock-lee-cpf-record.json` — CPF contribution record (authority-authored)
- `hock-lee-workpass-list.json` — Work Permit holders (authority-authored)
- `hock-lee-accounts-extract.json` — balance sheet extract (self-authored)
- `hock-lee-tenancy-extract.json` — tenancy permitted use covenant (third-party)
- `hock-lee-wic-schedule.json` — work injury policy schedule
- `hock-lee-endorsement-02-illegible.json` — the deliberately unreadable one

Each file carries `_fixture: true` and `_author_class`, which is what Layer 3
reads to derive `evidence_class`. A developer cannot set the class by hand —
that is the point.

## Conflicting endorsements — the precedence tie

`hock-lee-conflicting-endorsements.json` adds END-03 and END-04. Both amend the
same election (the food-and-drink liability sub-limit, `LIMIT.SUB_LIMIT`) to
different figures, at the **same** authority rank, with the **same** effective date
and no statement that either supersedes the other. The resolver refuses to break the
tie and the fourth abstention gate escalates the finding to
`REQUIRES_AUTHORISED_REVIEW`. This is the fourth gate firing on real data, not only
in a unit test (definition of done item 5). The sub-limit is not consumed by any
settlement computation, so the tie is self-contained and creates no contradiction
with the arithmetic elsewhere in the demo.
