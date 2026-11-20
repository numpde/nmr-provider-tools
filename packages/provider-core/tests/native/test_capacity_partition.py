"""Static shares preserve aggregate allowances without acquiring execution."""

from copy import deepcopy
import unittest

from provider_core.hosting import require_capacity_partition


class CapacityPartitionTests(unittest.TestCase):
    def setUp(self):
        self.total = {"input_bytes": 100, "result_bytes": 200, "state_values": 4, "progress_records": 1}
        self.shares = {
            "baseline": {"input_bytes": 40, "result_bytes": 100, "state_values": 2},
            "peaks": {"input_bytes": 60, "result_bytes": 100, "state_values": 2},
        }

    def test_exact_and_underallocated_partitions_do_not_mutate_the_grants(self):
        before = deepcopy((self.total, self.shares))
        require_capacity_partition(total=self.total, shares=self.shares)
        self.assertEqual(before, (self.total, self.shares))
        require_capacity_partition(total=self.total, shares={"baseline": self.shares["baseline"]})
        require_capacity_partition(total=self.total, shares={})

    def test_each_aggregate_dimension_rejects_overgrant_with_assigned_and_total_evidence(self):
        for name in ("input_bytes", "result_bytes", "state_values"):
            with self.subTest(name=name):
                shares = deepcopy(self.shares)
                shares["peaks"][name] += 1
                with self.assertRaises(ValueError) as raised:
                    require_capacity_partition(total=self.total, shares=shares)
                message = str(raised.exception)
                self.assertIn(name, message)
                self.assertIn(f"assigned={self.total[name] + 1}", message)
                self.assertIn(f"allowance={self.total[name]}", message)

    def test_totals_and_shares_reject_noninteger_or_nonpositive_capacity(self):
        for value, error_type in ((True, TypeError), (1.5, TypeError), (0, ValueError), (-1, ValueError)):
            for target in ("total", "share"):
                with self.subTest(value=value, target=target):
                    total, shares = deepcopy((self.total, self.shares))
                    if target == "total":
                        total["state_values"] = value
                    else:
                        shares["peaks"]["state_values"] = value
                    with self.assertRaises(error_type) as raised:
                        require_capacity_partition(total=total, shares=shares)
                    self.assertIn("state_values", str(raised.exception))

    def test_share_fields_exclude_per_operation_progress_and_require_all_aggregate_dimensions(self):
        for share in (
            {"input_bytes": 1, "result_bytes": 1},
            {"input_bytes": 1, "result_bytes": 1, "state_values": 1, "progress_records": 1},
        ):
            with self.subTest(share=share):
                with self.assertRaises(ValueError) as raised:
                    require_capacity_partition(total=self.total, shares={"peaks": share})
                self.assertIn("peaks", str(raised.exception))

    def test_total_and_share_containers_are_mappings(self):
        for total, shares in (([], self.shares), (self.total, []), (self.total, {"peaks": []})):
            with self.subTest(total=total, shares=shares), self.assertRaises(TypeError):
                require_capacity_partition(total=total, shares=shares)
