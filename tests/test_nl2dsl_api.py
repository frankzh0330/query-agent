import unittest

from fastapi.testclient import TestClient

import app as app_module
from service.llm_extractions import Extraction, ExtractionsJson


class NL2DSLApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)

    def test_nl2dsl_region_and_country_filter_from_germany(self):
        def fake_extract(_: str) -> ExtractionsJson:
            return ExtractionsJson(
                metric_extractions=[Extraction(text="PV")],
                time_extractions=[Extraction(text="近7天")],
                event_extractions=[Extraction(text="app_launch")],
                region_filter=["EUTTP"],
                group_by_extractions=[Extraction(text="country")],
            )

        original = app_module.extract_extractions_llm
        app_module.extract_extractions_llm = fake_extract
        try:
            resp = self.client.post(
                "/nl2dsl",
                json={"text": "查询德国近7天 app_launch 的PV 按 country 分组", "project_id": 55},
            )
        finally:
            app_module.extract_extractions_llm = original

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["semantic"]["region_filter"], ["EUTTP"])
        self.assertEqual(body["exec_dsl"]["content"]["option"]["finder"]["region_filter"], ["EUTTP"])
        self.assertEqual(
            body["exec_dsl"]["content"]["queries"][0]["filters"],
            [{"property_name": "country", "op": "=", "value": "de"}],
        )
        self.assertIn("resolver_explain", body["explain"])

    def test_nl2dsl_europe_has_no_country_filter(self):
        def fake_extract(_: str) -> ExtractionsJson:
            return ExtractionsJson(
                metric_extractions=[Extraction(text="PV")],
                time_extractions=[Extraction(text="近7天")],
                event_extractions=[Extraction(text="app_launch")],
                region_filter=["EUTTP"],
                group_by_extractions=[Extraction(text="country")],
            )

        original = app_module.extract_extractions_llm
        app_module.extract_extractions_llm = fake_extract
        try:
            resp = self.client.post(
                "/nl2dsl",
                json={"text": "计算欧洲近7天 app_launch 的PV 按 country 分组", "project_id": 55},
            )
        finally:
            app_module.extract_extractions_llm = original

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["semantic"]["region_filter"], ["EUTTP"])
        self.assertEqual(body["exec_dsl"]["content"]["queries"][0]["filters"], [])


if __name__ == "__main__":
    unittest.main()
