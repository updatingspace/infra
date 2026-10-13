import datetime
import importlib.util
from pathlib import Path
import unittest
import uuid

spec = importlib.util.spec_from_file_location('export_ydb', Path(__file__).with_name('export-ydb.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ExportTypesTests(unittest.TestCase):
    def test_epoch_and_native_datetime_match_without_losing_microseconds(self):
        timestamp = datetime.datetime(1970, 1, 1, 0, 0, 1, 234567, tzinfo=datetime.timezone.utc)
        self.assertEqual(module.normalize(1234567, 'Timestamp'), module.normalize(timestamp, 'Timestamp'))
        self.assertEqual(module.normalize(1, 'Datetime'), '1970-01-01T00:00:01+00:00')
        self.assertEqual(module.normalize(1, 'Date'), '1970-01-02')

    def test_uuid_and_json_preserve_types(self):
        value = uuid.UUID('a1c54758-b59f-44c0-9bd7-8761813d0ae5')
        self.assertEqual(module.normalize(value, 'UUID'), str(value))
        self.assertEqual(module.normalize('{"a":[1,true,null]}', 'Json'), {'a': [1, True, None]})
        self.assertIsNone(module.normalize(None, 'UUID?'))
        with self.assertRaises(TypeError):
            module.normalize(b'unknown', 'String')

    def test_digest_is_order_independent_but_preserves_duplicate_rows(self):
        rows = [{'a': 1, 'b': 'тест'}, {'a': 2, 'b': 'other'}]
        self.assertEqual(module.digest(rows), module.digest(list(reversed(rows))))
        self.assertNotEqual(module.digest(rows), module.digest(rows + [rows[0]]))


if __name__ == '__main__':
    unittest.main()
