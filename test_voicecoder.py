"""Unit tests for voicecoder. No microphone, speaker or agent needed.

Run:  python -m unittest test_voicecoder -v
"""
import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import voicecoder as vc


def new_state():
    return {"session": None, "message": "", "error": None}


class CleanForSpeechTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(vc.clean_for_speech(""), "")
        self.assertEqual(vc.clean_for_speech(None), "")

    def test_code_block_replaced(self):
        out = vc.clean_for_speech("Here:\n```python\nprint(1)\n```\nDone.")
        self.assertIn("code block, see screen", out)
        self.assertNotIn("print(1)", out)

    def test_inline_code_links_and_urls(self):
        out = vc.clean_for_speech("Edit `main.py`, see [docs](http://x.com) or https://example.com/a")
        self.assertEqual(out, "Edit main.py, see docs or a link")

    def test_headings_bullets_and_emphasis(self):
        out = vc.clean_for_speech("# Title\n- **one**\n2. two\n> quote")
        self.assertEqual(out, "Title one two quote")

    def test_long_text_truncated_at_sentence(self):
        text = "First sentence here. " * 10 + "x" * 200
        out = vc.clean_for_speech(text, limit=100)
        self.assertTrue(out.endswith("... The rest is on screen."))
        self.assertTrue(out.startswith("First sentence here."))
        self.assertLessEqual(len(out), 100 + len(" ... The rest is on screen."))

    def test_short_text_not_truncated(self):
        self.assertEqual(vc.clean_for_speech("All done.", limit=100), "All done.")


class NormTests(unittest.TestCase):
    def test_strips_punctuation_and_case(self):
        self.assertEqual(vc._norm("  Stop! "), "stop")
        self.assertEqual(vc._norm("New session."), "new session")
        self.assertEqual(vc._norm(None), "")

    def test_matches_command_words(self):
        self.assertIn(vc._norm("Repeat that?"), vc.REPEAT_WORDS)
        self.assertIn(vc._norm("Bye!"), vc.STOP_WORDS)
        self.assertTrue(set(vc._norm("Yes, send it").split()) & vc.YES_WORDS)


class CodexEventTests(unittest.TestCase):
    def setUp(self):
        self.out = mock.patch("sys.stdout", new_callable=io.StringIO).start()
        self.addCleanup(mock.patch.stopall)

    def test_thread_started_sets_session(self):
        st = new_state()
        vc._codex_event({"type": "thread.started", "thread_id": "abc"}, st)
        self.assertEqual(st["session"], "abc")

    def test_agent_message(self):
        st = new_state()
        vc._codex_event({"type": "item.completed",
                         "item": {"type": "agent_message", "text": "Fixed it."}}, st)
        self.assertEqual(st["message"], "Fixed it.")

    def test_command_and_file_change_are_printed(self):
        st = new_state()
        vc._codex_event({"type": "item.started",
                         "item": {"type": "command_execution", "command": "ls"}}, st)
        vc._codex_event({"type": "item.completed",
                         "item": {"type": "file_change", "changes": [{"path": "a.py"}]}}, st)
        self.assertIn("$ ls", self.out.getvalue())
        self.assertIn("edited a.py", self.out.getvalue())

    def test_error_dict_and_string(self):
        st = new_state()
        vc._codex_event({"type": "turn.failed", "error": {"message": "boom"}}, st)
        self.assertEqual(st["error"], "boom")
        vc._codex_event({"type": "error", "message": "bad"}, st)
        self.assertEqual(st["error"], "bad")


class ClaudeEventTests(unittest.TestCase):
    def setUp(self):
        self.out = mock.patch("sys.stdout", new_callable=io.StringIO).start()
        self.addCleanup(mock.patch.stopall)

    def test_session_and_result(self):
        st = new_state()
        vc._claude_event({"type": "result", "session_id": "s1", "result": "Done."}, st)
        self.assertEqual(st["session"], "s1")
        self.assertEqual(st["message"], "Done.")
        self.assertIsNone(st["error"])

    def test_error_result(self):
        st = new_state()
        vc._claude_event({"type": "result", "result": "rate limited", "is_error": True}, st)
        self.assertEqual(st["error"], "rate limited")

    def test_assistant_text_and_tool_use_printed(self):
        st = new_state()
        vc._claude_event({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Looking now"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
        ]}}, st)
        self.assertIn("Looking now", self.out.getvalue())
        self.assertIn("> Read x.py", self.out.getvalue())


class StreamTests(unittest.TestCase):
    def test_parses_json_lines_from_subprocess(self):
        # A fake agent: echoes stdin back as a Claude result event, plus a non-JSON line.
        script = ("import sys, json, os; p = sys.stdin.read(); print('plain text'); "
                  "print(json.dumps({'type': 'result', 'session_id': os.environ['VOICECODER_LOOP'],"
                  " 'result': 'got ' + p}))")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code, st = vc._stream([sys.executable, "-c", script], "hello & 'quotes'", ".", vc._claude_event)
        self.assertEqual(code, 0)
        self.assertEqual(st["message"], "got hello & 'quotes'")
        self.assertEqual(st["session"], "1")  # loop env var is passed to the agent
        self.assertIn("plain text", out.getvalue())


class HookTests(unittest.TestCase):
    def setUp(self):
        self.speak = mock.patch.object(vc, "speak_detached").start()
        mock.patch.dict(os.environ, {}, clear=False).start()
        os.environ.pop(vc.IN_LOOP_ENV, None)
        self.addCleanup(mock.patch.stopall)

    def test_codex_notify_speaks_message(self):
        payload = json.dumps({"type": "agent-turn-complete", "last-assistant-message": "**Done**"})
        vc.cmd_codex_notify(SimpleNamespace(payload=payload))
        self.speak.assert_called_once_with("Codex has finished. Done")

    def test_codex_notify_ignores_other_events_and_bad_json(self):
        vc.cmd_codex_notify(SimpleNamespace(payload=json.dumps({"type": "other"})))
        vc.cmd_codex_notify(SimpleNamespace(payload="not json"))
        self.speak.assert_not_called()

    def test_hooks_silent_inside_loop(self):
        os.environ[vc.IN_LOOP_ENV] = "1"
        vc.cmd_codex_notify(SimpleNamespace(payload=json.dumps({"type": "agent-turn-complete"})))
        with mock.patch("sys.stdin", io.StringIO("{}")):
            vc.cmd_claude_stop(SimpleNamespace())
        self.speak.assert_not_called()

    def test_claude_stop_speaks_message(self):
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"last_assistant_message": "All good"}))):
            vc.cmd_claude_stop(SimpleNamespace())
        self.speak.assert_called_once_with("Claude has finished. All good")

    def test_claude_stop_without_message_or_bad_input(self):
        with mock.patch("sys.stdin", io.StringIO("garbage")):
            vc.cmd_claude_stop(SimpleNamespace())
        self.speak.assert_called_once_with("Claude has finished.")

    def test_claude_stop_skips_when_stop_hook_active(self):
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"stop_hook_active": True}))):
            vc.cmd_claude_stop(SimpleNamespace())
        self.speak.assert_not_called()


class FakeEars:
    voice = True

    def __init__(self, *answers):
        self.answers = list(answers)

    def hear(self):
        return self.answers.pop(0)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        mock.patch.object(vc, "speak").start()
        mock.patch("sys.stdout", new_callable=io.StringIO).start()
        self.addCleanup(mock.patch.stopall)

    def desk(self, *answers):
        return vc.ApprovalDesk(FakeEars(*answers), "Claude", listen=False)

    def test_yes_no_and_unclear(self):
        self.assertEqual(self.desk("yes please").decide("run ls"), (True, False))
        self.assertEqual(self.desk("no").decide("run ls"), (False, False))
        self.assertEqual(self.desk("hmm", "what", "banana").decide("run ls"), (False, False))

    def test_no_wins_over_yes(self):
        self.assertEqual(self.desk("yes no don't").decide("run ls"), (False, False))

    def test_always_remembers_key(self):
        d = self.desk("always")
        self.assertEqual(d.decide("edit a", key="Edit"), (True, True))
        self.assertEqual(d.decide("edit b", key="Edit"), (True, True))  # no second question

    def test_always_refused_for_risky_command(self):
        d = self.desk("always", "always", "always")
        self.assertEqual(d.decide("run rm -rf x", key="Bash:rm", risky_text="rm -rf x"), (False, False))
        self.assertNotIn("Bash:rm", d.always)

    def test_describe_claude_request(self):
        s, _, key, risky = vc.describe_claude_request("Bash", {"command": "npm test"})
        self.assertEqual((s, key, risky), ("run the command: npm test", "Bash:npm", "npm test"))
        self.assertEqual(vc.describe_claude_request("Write", {"file_path": "/a/b.txt"})[0],
                         "create or overwrite the file b.txt")
        self.assertEqual(vc.describe_claude_request("WebFetch", {"url": "https://x.org/p"})[0],
                         "open the web page x.org")

    def test_desk_over_socket_and_unreachable(self):
        d = vc.ApprovalDesk(FakeEars("yes"), "Claude")
        self.assertEqual(vc.ask_desk(d.address, "run ls"), (True, False))
        self.assertEqual(vc.ask_desk("127.0.0.1:1:bad", "x"), (False, False))

    def test_wrong_token_is_rejected(self):
        d = vc.ApprovalDesk(FakeEars("yes"), "Claude")
        bad = d.address.rsplit(":", 1)[0] + ":wrongtoken"
        self.assertEqual(vc.ask_desk(bad, "run ls"), (False, False))

    def test_codex_server_request_answers(self):
        srv = vc.CodexAppServer.__new__(vc.CodexAppServer)
        srv.cwd, srv.items, sent = "/p", {"f1": {"changes": [{"path": "/p/a.py"}]}}, []
        srv._send = sent.append
        srv.desk = self.desk("yes", "no")
        srv._answer_server_request(7, "item/commandExecution/requestApproval", {"command": "npm i"})
        srv._answer_server_request(8, "item/fileChange/requestApproval", {"itemId": "f1"})
        srv._answer_server_request(9, "weird/request", {})
        self.assertEqual(sent[0], {"id": 7, "result": {"decision": "accept", "acceptSettings": {"forSession": False}}})
        self.assertEqual(sent[1], {"id": 8, "result": {"decision": "decline"}})
        self.assertEqual(sent[2]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
