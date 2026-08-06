import importlib
import os
import unittest
from unittest.mock import patch


os.environ.setdefault("AIRTABLE_API_KEY", "test-key")
os.environ.setdefault("BASE_ID", "test-base")
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("CHUNI_TOKEN", "test-token")

app_module = importlib.import_module("app")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeTable:
    def __init__(self, records=None):
        self.records = records or []
        self.all_calls = 0
        self.update_batch_sizes = []
        self.create_batch_sizes = []

    def all(self):
        self.all_calls += 1
        return self.records

    def batch_update(self, records):
        self.update_batch_sizes.append(len(records))
        return records

    def batch_create(self, fields_list):
        self.create_batch_sizes.append(len(fields_list))
        start = len(self.records)
        created = [
            {"id": f"new-{start + i}", "fields": fields}
            for i, fields in enumerate(fields_list)
        ]
        self.records.extend(created)
        return created


class AppTestCase(unittest.TestCase):
    def setUp(self):
        app_module._memory_cache = {"records": None, "expires_at": 0}
        app_module._redis_client = None
        app_module.REDIS_URL = None
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def test_index_reuses_cache(self):
        fake_table = FakeTable([{"id": "rec-1", "fields": {"定数": "14.0"}}])
        app_module.table = fake_table

        self.client.get("/")
        self.client.get("/")

        self.assertEqual(fake_table.all_calls, 1)

    def test_player_updates_are_sent_in_batches_of_ten(self):
        records = [
            {
                "id": f"rec-{i}",
                "fields": {"ID": str(i), "難易度": "MAS", "mea_Score": 0},
            }
            for i in range(23)
        ]
        fake_table = FakeTable(records)
        app_module.table = fake_table
        api_records = [
            {
                "id": i,
                "diff": "MAS",
                "const": 14.0,
                "score": 1_007_500 + i,
                "title": f"song-{i}",
            }
            for i in range(23)
        ]

        with patch.object(app_module.requests, "get", return_value=FakeResponse({"records": api_records})):
            response = self.client.get(
                "/update/mea/test-user",
                headers={"Accept": "text/event-stream"},
            )
            response.get_data()

        self.assertEqual(fake_table.update_batch_sizes, [10, 10, 3])
        self.assertEqual(records[0]["fields"]["mea_Score"], 1_007_500)

    def test_master_creates_are_sent_in_batches_of_ten(self):
        fake_table = FakeTable([])
        app_module.table = fake_table
        music_data = [
            {
                "meta": {"id": i, "title": f"song-{i}"},
                "data": {"MAS": {"const": 14.0}},
            }
            for i in range(23)
        ]

        with patch.object(app_module.requests, "get", return_value=FakeResponse(music_data)):
            response = self.client.get(
                "/update_master",
                headers={"Accept": "text/event-stream"},
            )
            response.get_data()

        self.assertEqual(fake_table.create_batch_sizes, [10, 10, 3])


if __name__ == "__main__":
    unittest.main()
