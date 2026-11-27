import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


PACKAGE = Path(__file__).resolve().parents[2] / "gx-h/plugins/installed/org.gx.scholarly-sources/1.0.0"
SPEC = importlib.util.spec_from_file_location("gx_social_intake", PACKAGE / "methods/social.py")
SOCIAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SOCIAL)


def export(url, **fields):
    return json.dumps({"url": url, **fields}, ensure_ascii=False).encode()


class SocialSources(unittest.TestCase):
    def test_wechat_export_records_original_bytes_and_locator(self):
        raw = export(
            "https://mp.weixin.qq.com/s/abc_D?scene=3",
            text="读书笔记。\r\n\r\n线索 PMID: 12345678。",
            title="酵母读书笔记", captured_at="2026-10-11T08:00:00Z",
        )
        result = SOCIAL.social_intake(raw)
        self.assertEqual(result["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["provider"], "wechat")
        self.assertEqual(result["acquisition_mode"], "manual_import")
        self.assertEqual(result["evidence_kind"], "community_lead")
        self.assertEqual(result["disposition"], "requires_primary_source_followup")
        self.assertEqual(result["paragraphs"][1], {"locator": "paragraph:2", "text": "线索 PMID: 12345678。"})
        self.assertEqual(result["primary_source_candidates"], [{"identifier": "PMID:12345678", "locator": "paragraph:2"}])
        self.assertEqual(result["canonical_url"], "https://mp.weixin.qq.com/s/abc_D")
        self.assertEqual(result["captured_at"], "2026-10-11T08:00:00Z")

    def test_wechat_legacy_query_identity_without_transient_parameters(self):
        prefix = "https://mp.weixin.qq.com/s?"
        one = SOCIAL.social_intake(export(prefix + "idx=1&mid=100&__biz=MjA=&pass_ticket=example"))
        two = SOCIAL.social_intake(export(prefix + "sn=z&__biz=MjA%3D&mid=100&idx=1"))
        self.assertEqual(one["source_id"], two["source_id"])
        self.assertEqual(one["canonical_url"], prefix + "__biz=MjA%3D&mid=100&idx=1")

    def test_xhs_link_only_and_export_have_same_identity_and_distinct_provenance(self):
        note_id = "6a8d0d450000000028031a5d"
        raw = export("https://www.xiaohongshu.com/discovery/item/" + note_id + "?xsec_token=example#view")
        link = SOCIAL.social_intake(raw)
        self.assertEqual(link["acquisition_mode"], "link_only")
        self.assertEqual(link["provider"], "xiaohongshu")
        self.assertEqual(link["paragraphs"], [])
        self.assertEqual(link["primary_source_candidates"], [])
        self.assertNotIn("xsec_token", str(link))
        public = "https://www.rednote.com/explore/" + note_id
        exported = SOCIAL.social_intake(export(public, text="本地导出的公开笔记。\n\n" + public + "?xsec_token=temporary"))
        self.assertEqual(exported["source_id"], link["source_id"])
        self.assertEqual(exported["acquisition_mode"], "manual_import")
        self.assertEqual(exported["paragraphs"][0]["text"], "本地导出的公开笔记。")
        self.assertEqual(exported["paragraphs"][1]["text"], public)
        self.assertNotIn("temporary", str(exported))
        self.assertNotEqual(exported["source_sha256"], link["source_sha256"])

    def test_invalid_origin_missing_identity_and_spoofed_inputs(self):
        for url in (
            "https://mp.weixin.qq.com.evil.example/s/x", "https://user@mp.weixin.qq.com/s/y",
            "https://www.xiaohongshu.com/explore/", "https://mp.weixin.qq.com/s?scene=123",
            "https://mp.weixin.qq.com/s?__biz=a&mid=123&mid=124&idx=1",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                SOCIAL.social_intake(export(url))
        with self.assertRaises(ValueError):
            SOCIAL.social_intake(export("https://mp.weixin.qq.com/s/y", text={"file": "/etc/hosts"}))
        with self.assertRaises(ValueError):
            SOCIAL.social_intake(export("https://mp.weixin.qq.com/s/y", evidence_kind="primary"))
