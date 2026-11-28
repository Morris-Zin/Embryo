import unittest

from mixed_minute import score


class DeadlineTest(unittest.TestCase):
    def test_event_timestamp_at_subsecond_boundary(self):
        events = [{"event": "minute_clock", "start_ms": 340}]
        for number, millis in enumerate((339, 340, 341)):
            events.append({
                "event": "voucher_complete", "client_voucher_id": str(number),
                "type": "sale", "line_count": 10, "duration_ms": 1, "server_duration_ms": 1,
                "at": f"1970-01-01T00:01:00.{millis}Z",
                "observed_utc": "1970-01-01T00:01:00Z",
            })
        result = score(events)
        self.assertEqual(result["confirmed_in_minute"], 1)
        self.assertEqual(result["work"]["sale_10"]["confirmed_in_first_60s"], 1)
