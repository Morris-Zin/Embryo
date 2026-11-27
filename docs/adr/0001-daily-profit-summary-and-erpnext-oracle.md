# Keep ERPNext as the profit oracle and replay exceptions

ERPNext's core profit calculation remains untouched because it is the permanent **ERPNext Oracle** for valuation, returns, permissions, precision, and report semantics. The **Daily Profit Summary** stores only clean-day **Clean Set A** invoice contributions and re-evaluates every **Replay Candidate (Set B)** as a whole invoice, instead of modifying core Gross Profit or storing naive additive cross-day deltas. This avoids false profit when linked returns, valuation reposts, precision boundaries, or request-dependent valuation make invoice contributions non-additive across days.

## Consequences

- Core ERPNext can be upgraded and compared without maintaining a forked Gross Profit implementation.
- Dirty or incomplete days cannot be treated as additive profit and must be rebuilt or evaluated live before they are trusted.
