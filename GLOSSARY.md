# Khitmee Profit Reporting

Khitmee profit reporting covers counter sales profit across shops, warehouses, and ERPNext. It exists to keep profit vocabulary aligned with ERPNext rather than inventing conflicting report terms.

## Language

**Shop**:
A physical selling location represented one-to-one by a **Warehouse** for stock and profit reporting.
_Avoid_: "Shop" group_by alias, store, branch

**Warehouse**:
A stock location ERPNext uses as the canonical profit-reporting grouping; when it serves a retail counter, it represents one **Shop**.
_Avoid_: location, stockroom

**Daily Profit Summary**:
A profit total for one company, day, and **Warehouse** that is trusted only for a clean past day.
_Avoid_: profit cache, summary table, additive profit row

**Clean Set A**:
The invoices for a day whose profit contributions are fixed once the day is complete, so they can belong to a **Daily Profit Summary**.
_Avoid_: additive invoices, safe invoices

**Replay Candidate (Set B)**:
An invoice whose profit contribution must be re-evaluated against the **ERPNext Oracle** because returns, stock valuation, precision, or request context can change its interpretation.
_Avoid_: replay invoice, exception invoice, dirty invoice

**Dirty Range**:
A span of days whose **Daily Profit Summary** entries are not trustworthy until the affected days are rebuilt.
_Avoid_: invalidation window, stale range

**ERPNext Oracle**:
The unmodified ERPNext profit calculation treated as the reference truth for profit results.
_Avoid_: oracle clone, upstream approximation
