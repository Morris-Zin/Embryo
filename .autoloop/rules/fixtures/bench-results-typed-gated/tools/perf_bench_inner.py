import time

from khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit import execute as fast_gp_execute


def benchmark(filters):
    benchmarks = {}
    for label, report_filter in filters:
        start = time.perf_counter()
        _columns, rows, *_ = fast_gp_execute(report_filter)
        benchmarks[label] = {"wall_ms": (time.perf_counter() - start) * 1000, "row_count": len(rows)}
    return benchmarks
