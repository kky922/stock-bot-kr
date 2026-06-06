import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

import config
from core.data_store import DataStore


class DataStoreCacheTests(unittest.TestCase):
    def test_load_reflects_external_writes_from_another_store_instance(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch.object(config, "DATA_DIR", data_dir):
                store1 = DataStore()
                store2 = DataStore()

                store1.safe_save("all_slots", {"slots": {}})
                self.assertEqual(store1.get_open_slot_count("KR"), 0)

                store2.save_slot(
                    "KR_005930",
                    {"market": "KR", "code": "005930", "name": "삼성전자", "quantity": 1},
                )

                self.assertEqual(store1.get_open_slot_count("KR"), 1)


if __name__ == "__main__":
    unittest.main()
