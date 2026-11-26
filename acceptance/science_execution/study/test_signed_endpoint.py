"""Synthetic tests. No network, coordinates or live GX host required."""
import unittest

from pathlib import Path
import runpy

METHODS = Path(__file__).resolve().parents[3] / "gx-h/plugins/installed/org.gx.block-signed/1.0.0/methods"
signed = runpy.run_path(str(METHODS / "signed.py"))
PAIRS, summarize = signed["PAIRS"], signed["summarize"]


ENDPOINT = "rmsd_A"
BASELINE = (1.0, 2.0, 3.0)


def table(after_rows, before=BASELINE):
    def pairs(values):
        return {pair: {ENDPOINT: value} for pair, value in zip(PAIRS, values, strict=True)}

    blocks = []
    for index, values in enumerate(after_rows):
        row = {"index": index, "eligible": values is not None}
        if values is not None:
            row["pairs"] = pairs(values)
        blocks.append(row)
    return {"schema": "gx.block-influence/1", "baseline": pairs(before), "blocks": blocks}


def evaluate(after_rows, minimum=1):
    return summarize(table(after_rows), ENDPOINT, 0.5, 0.125, minimum)


class SignedEndpointTests(unittest.TestCase):
    def test_thresholds_and_near_zero_classifications(self):
        cases = [
            ((1.0, 2.0, 3.125), "quiet"),
            ((1.0, 2.5, 3.5), "represented"),
            ((0.5, 2.5, 3.5), "opposed"),
            ((0.5, 2.0, 3.0), "near_zero"),
            ((0.5, 2.0625, 3.0), "near_zero"),
            ((0.5, 2.125, 3.0), "attenuated"),
            ((0.5, 1.5, 2.5), "represented"),
        ]
        for values, classification in cases:
            with self.subTest(values=values):
                result = evaluate([values])
                self.assertEqual(result["blocks"][0]["classification"], classification)

    def test_median_order_can_change(self):
        # Before=(1,2,3), after=(4,2,2.5): median displacement +0.5.
        # Median of displacements is zero. Using it would switch the classification.
        result = evaluate([(4.0, 2.0, 2.5)])
        self.assertEqual(result["blocks"][0]["classification"], "opposed")

    def test_zero_is_valid_only_with_sufficient_coverage(self):
        quiet = BASELINE
        missing = (1.0, None, 3.0)
        positive = (0.5, 2.5, 3.5)
        for rows, minimum, expected, reason in [
            ([quiet, None], 1, 0, None),
            ([quiet, missing], 1, None, "unmeasured_eligible_blocks"),
            ([quiet, positive, missing], 2, 1, None),
            ([positive, missing], 2, None, "too_few_evaluable_blocks"),
            ([None], 1, None, "too_few_evaluable_blocks"),
            ([], 1, None, "too_few_evaluable_blocks"),
        ]:
            with self.subTest(rows=rows):
                summary = evaluate(rows, minimum)
                self.assertEqual(summary["missed_present"], expected)
                self.assertEqual(summary["unavailable_reason"], reason)

    def test_units_and_denominators(self):
        summary = evaluate([BASELINE, None, (1.0, None, 3.0), (0.5, 2.5, 3.5)])
        self.assertEqual(summary["unit"], "Å")
        self.assertEqual([summary[key] for key in (
            "blocks_total", "blocks_eligible", "blocks_evaluable",
            "blocks_unmeasured", "blocks_missed_observed")], [4, 3, 2, 1, 1])
        self.assertIs(type(summary["missed_present"]), int)

    def test_invalid_settings_or_output_are_rejected(self):
        for e, z, n in [(0.1, 0.2, 1), (0, 0, 1), (0.1, -0.1, 1), (0.2, 0.1, False),
                        (0.2, 0.1, 0), (0.2, float("nan"), 1), (True, 0.1, 1)]:
            with self.subTest(e=e, z=z, n=n), self.assertRaises(ValueError):
                summarize(table([BASELINE]), ENDPOINT, e, z, n)
        with self.assertRaises(ValueError):
            evaluate([(float("inf"), 2, 3)])
        duplicate = table([BASELINE, BASELINE])
        duplicate["blocks"][1]["index"] = 0
        with self.assertRaises(ValueError):
            summarize(duplicate, ENDPOINT, 0.5, 0.125, 1)


class SufficientSignedTableTests(unittest.TestCase):
    def test_actual_registered_value_is_required_for_table_availability(self):
        from gx_core.plugins import loader

        package = ("org.gx.block-signed", "1.0.0")
        registry = loader.bootstrap(selected=[package])
        self.assertFalse(registry.failures)
        plugin, = registry.plugins.values()
        project = plugin.contributions.goal_methods["deposited-block-signed@1"].sufficient_metric
        mask = "common_ca"
        parameters = dict(mask=mask, endpoint=ENDPOINT, effect_minimum=0.5, zero_limit=0.125,
                          minimum_evaluable=2)
        quiet, missing, hit = BASELINE, (1, None, 3), (0.5, 2.5, 3.5)
        for after, expected, evaluable in (([missing, missing], None, 0), ([hit, None], None, 1),
                                            ([quiet, quiet, missing], None, 2), ([hit, quiet, missing], 1, 2),
                                            ([quiet, quiet, None], 0, 2)):
            legacy = dict(table(after), mask=mask)
            # Produce the existing signed detail using very different scientific cutoffs;
            # the availability callback must reapply the actual claim settings.
            prior = summarize(legacy, ENDPOINT, 10, 1, 1)
            details = dict(schema="gx.signed-blocks/1", endpoint=ENDPOINT, mask=mask, blocks=prior["blocks"])
            for cap, name, document in (("deposited-block-influence@1", "block-influence.json", legacy),
                                        ("deposited-block-signed@1", "signed-blocks.json", details)):
                with self.subTest(known=expected, available=evaluable, source=cap):
                    self.assertEqual(project(parameters, "missed_present", cap, name, document), expected is not None)
                    self.assertTrue(project(parameters, "blocks_evaluable", cap, name, document))
                    self.assertFalse(project({**parameters, "minimum_evaluable": 4}, "missed_present", cap, name,
                                             document))
                    self.assertFalse(project({**parameters, "mask": "common_backbone"}, "missed_present", cap, name,
                                             document))
                    if expected == 1:
                        # Increasing the floor makes all measured rows quiet. The missing row
                        # could then change a negative answer, so the new value is unknown.
                        self.assertFalse(project({**parameters, "effect_minimum": 10}, "missed_present", cap, name,
                                                 document))


if __name__ == "__main__":
    unittest.main()
