"""Pure fixture execution must never assess arbitrary customer bytes."""
import copy
from unittest.mock import patch
from django.test import SimpleTestCase
from recruiting.cloud.sample_contract import sample_catalog, sample_rubric


class FixtureExecutionTests(SimpleTestCase):
    def evaluate(self, item=None, criteria=None):
        from recruiting.cloud.fixtures import evaluate_fixture
        item = item or sample_catalog()[0]
        return evaluate_fixture(item['filename'], item['content'],
                                criteria if criteria is not None else sample_rubric())

    def test_canonical_outputs_have_grounded_scores_and_synthetic_interviews(self):
        results = [self.evaluate(item) for item in sample_catalog()]
        self.assertEqual([r['assessment']['alignment'] for r in results], [100, 50, 18.75])
        self.assertTrue(all(r['assessment']['sample'] for r in results))
        self.assertEqual(len(results[1]['assessment']['concerns']), 1)
        for result in results:
            self.assertTrue(result['synthetic'])
            self.assertTrue(result['interviews']['sample'])
            self.assertGreaterEqual(len(result['interviews']['questions']), 5)
            self.assertIn('no live research', result['interviews']['research_status'])
            blocks = {b['id']:b['text'] for b in result['blocks']}
            for finding in result['assessment']['evaluations']:
                for evidence in finding['evidence']:
                    self.assertIn(evidence['quote'], blocks[evidence['block_id']])

    def test_mutated_source_name_type_or_rubric_never_returns_fixture_result(self):
        row = sample_catalog()[0]
        from recruiting.cloud.fixtures import evaluate_fixture
        for filename, content in [(row['filename'], row['content']+b' changed'),
                                  ('Alex_Morgan_elsewhere.txt', row['content']),
                                  (row['filename'], row['content'].decode())]:
            with self.assertRaises(ValueError): evaluate_fixture(filename, content, sample_rubric())
        for key, value in [('weight', 10), ('name', 'Different requirement'), ('source_quote','invented')]:
            criteria = sample_rubric(); criteria[0][key] = value
            with self.assertRaises(ValueError): self.evaluate(criteria=criteria)

    def test_no_provider_research_or_parser_is_called(self):
        with patch('recruiting.providers.analyze_candidate', side_effect=AssertionError('paid provider')):
            self.assertTrue(self.evaluate()['synthetic'])

    def test_forged_evidence_is_rejected(self):
        from recruiting.samples import sample_assessment
        def forged(candidate, criteria):
            result = copy.deepcopy(sample_assessment(candidate, criteria))
            result['evaluations'][0]['evidence'][0]['quote'] = 'Invented source evidence'
            return result
        with patch('recruiting.cloud.fixtures.sample_assessment', side_effect=forged):
            with self.assertRaises(ValueError): self.evaluate()
