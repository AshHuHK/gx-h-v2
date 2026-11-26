import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from acceptance.science_execution import approval


class OriginalOperatorReply(unittest.TestCase):
    def choice(self, *, call_id="original", role="user", text="授权所有，不设token上线",
               question=None, offered=None):
        shown = approval.QUESTION.split("＋2 分鐘")[0] if question is None else question
        ask = {"questions": [{"title": approval.QUESTION, "options": offered or list(approval.CHOICES)}]}
        call = {"type": "function_call", "name": "request_user_input_async", "call_id": "original",
                "arguments": json.dumps(ask, ensure_ascii=False)}
        answer = {"questionItemId": json.dumps(["request_user_input_async", call_id, 0]),
                  "question": shown, "answer": text}
        body = "<send_user_message_question_reply>\n" + json.dumps([answer], ensure_ascii=False) + "\n</send_user_message_question_reply>"
        message = {"type": "message", "role": role, "content": [{"type": "input_text", "text": body}]}
        with TemporaryDirectory() as directory:
            source = Path(directory) / "conversation.jsonl"
            source.write_text("".join(json.dumps({"type": "response_item", "timestamp": "t",
                                                  "payload": part}, ensure_ascii=False) + "\n" for part in (call, message)))
            with patch.object(approval.original, "_source", return_value=source):
                return approval.user_choice("thread")

    def test_all_scoped_work_with_original_prompt_and_abridged_echo(self):
        self.assertEqual(self.choice()["choice"], approval.CHOICES[0])
        self.assertEqual(self.choice(text=approval.CHOICES[1])["choice"], approval.CHOICES[1])

    def test_truncated_or_free_text_reply_requires_disclosed_scope_and_same_request(self):
        for change in ({"call_id": "other"}, {"role": "assistant"}, {"question": "同意嗎"},
                       {"text": "暂不授权所有，不设token上线"}, {"text": "只做第一案"},
                       {"offered": [approval.CHOICES[0]]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.choice(**change)
