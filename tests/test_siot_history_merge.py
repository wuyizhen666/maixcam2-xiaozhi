import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'apps/siot_monitor/model.py'
spec = importlib.util.spec_from_file_location('siot_history_model', SOURCE)
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)


class HistoryMergeTests(unittest.TestCase):
    def setUp(self):
        self.state = model.MonitorModel()
        self.topic = 'fixture/data'
        self.second = 1700000000

    def live(self, content, timestamp=None):
        self.state.receive(self.topic, content,
                           self.second+.25 if timestamp is None else timestamp)

    def saved(self, identifier, content, timestamp=None):
        return {'id': identifier, 'content': content,
                'timestamp': self.second if timestamp is None else timestamp}

    def merge(self, rows):
        self.state.add_history(self.topic, rows)
        return self.state.history()

    def test_eight_live_numbers_and_same_saved_rows_stay_eight(self):
        for number in range(8):self.live(str(number), self.second+number+.2)
        history = [self.saved(number, str(number), self.second+number) for number in range(8)]
        result = self.merge(history)
        self.assertEqual(len(result), 8)
        self.assertEqual([entry['text'] for entry in result], [str(n) for n in range(8)])
        self.assertEqual([entry['id'] for entry in result], list(range(8)))
        self.assertTrue(all(entry['source']=='历史' for entry in result))

    def test_identical_values_in_one_second_preserve_multiplicity(self):
        for _ in range(5):self.live('42')
        history = [self.saved(n, '42') for n in range(3)]
        result = self.merge(history)
        self.assertEqual(len(result), 5)
        self.assertEqual(sum(item['id'] is None for item in result), 2)
        result = self.merge(history+[self.saved(3, '42')])
        self.assertEqual(len(result), 5)
        self.assertEqual(sum(item['id'] is None for item in result), 1)

    def test_repeated_history_does_not_consume_a_new_identical_live_message(self):
        self.live('same')
        history = [self.saved(7, 'same')]
        self.merge(history)
        self.live('same', self.second+.75)
        result = self.merge(history)
        self.assertEqual(len(result), 2)
        self.assertEqual(sum(item['id'] is None for item in result), 1)
        self.assertEqual(result[-1]['timestamp'], self.second+.75)

    def test_live_messages_not_covered_by_history_are_preserved(self):
        self.live('saved')
        self.live('not saved', self.second+1)
        result = self.merge([self.saved(7, 'saved')])
        self.assertEqual([item['text'] for item in result], ['saved', 'not saved'])
        self.assertIsNone(result[1]['id'])

    def test_same_content_in_different_seconds_is_not_merged(self):
        self.live('same', self.second+1.01)
        result = self.merge([self.saved(7, 'same')])
        self.assertEqual(len(result), 2)

    def test_truncated_live_or_history_is_not_guessed_from_prefix(self):
        self.live('x'*4100)
        result = self.merge([self.saved(7, 'x'*4100)])
        self.assertEqual(len(result), 2)
        self.assertTrue(all(item['truncated'] for item in result))

    def test_original_byte_length_must_match_complete_utf8_text(self):
        self.live(b'\xff')
        result = self.merge([self.saved(7, '\ufffd')])
        self.assertEqual(len(result), 2)

    def test_invalid_utf8_with_equal_replacement_length_is_not_matched(self):
        self.live(b'\xf0\x90\x80')
        result = self.merge([self.saved(7, '\ufffd')])
        self.assertEqual(len(result), 2)

    def test_only_unidentified_live_rows_are_replaced(self):
        self.state.receive(self.topic, 'same', self.second, source='other')
        self.state.receive(self.topic, 'same', self.second, message_id=9)
        result = self.merge([self.saved(7, 'same')])
        self.assertEqual(len(result), 3)

    def test_history_id_remains_primary_identity_and_is_normalized(self):
        self.live('a')
        result = self.merge([self.saved(7, 'a'), self.saved('7', 'a')])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['id'], 7)

    def test_missing_server_id_does_not_replace_live_row(self):
        self.live('same')
        result = self.merge([{'content':'same', 'timestamp':self.second}])
        self.assertEqual(len(result), 2)

    def test_distinct_history_ids_with_more_rows_than_live_are_kept(self):
        self.live('42')
        result = self.merge([self.saved(7, '42'), self.saved(8, '42')])
        self.assertEqual(len(result), 2)
        self.assertEqual({item['id'] for item in result}, {7, 8})

    def test_same_second_reverse_rest_rows_sort_by_numeric_id_without_fake_time(self):
        result = self.merge([self.saved('3', 'third'), self.saved(2, 'second'), self.saved('1', 'first')])
        self.assertEqual([item['text'] for item in result], ['first', 'second', 'third'])
        self.assertEqual([item['timestamp'] for item in result], [self.second]*3)


if __name__ == '__main__':
    unittest.main()
