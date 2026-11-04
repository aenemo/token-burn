import unittest

from collector.effort import normalise_effort, split_cursor_model


class NormaliseTests(unittest.TestCase):
    def test_cases(self):
        cases = {"xhigh": "extra", "XHigh": "extra", "extra_high": "extra", "extra-high": "extra",
                 " High ": "high", "low": "low", "medium": "medium", "max": "max",
                 "": "", None: "", "   ": "", "weird": "weird", 5: ""}
        for raw, want in cases.items():
            self.assertEqual(normalise_effort(raw), want, raw)


class SplitCursorModelTests(unittest.TestCase):
    def test_contract_examples(self):
        cases = {
            "grok-4.7-high": ("grok-4.7", "high"),
            "claude-opus-5-5-medium": ("claude-opus-5-5", "medium"),
            "gpt-5-xhigh": ("gpt-5", "extra"),
            "Claude Opus 5.5 (Auto Balanced)": ("Claude Opus 5.5", "balanced"),
            "grok-4.7-high (Auto Balanced)": ("grok-4.7", "high (auto balanced)"),
            "Claude Opus 5.5 (High)": ("Claude Opus 5.5", "high"),
            "claude-4-sonnet-thinking": ("claude-4-sonnet", "thinking"),
            "gpt-5": ("gpt-5", ""),
            "Auto Cost": ("Auto Cost", ""),
        }
        for name, want in cases.items():
            self.assertEqual(split_cursor_model(name), want, name)

    def test_more(self):
        self.assertEqual(split_cursor_model("gpt-5-extra-high"), ("gpt-5", "extra"))
        self.assertEqual(split_cursor_model("gpt-5-low"), ("gpt-5", "low"))
        self.assertEqual(split_cursor_model("model-max"), ("model", "max"))
        self.assertEqual(split_cursor_model("  gpt-5-high  "), ("gpt-5", "high"))
        self.assertEqual(split_cursor_model(""), ("", ""))
        self.assertEqual(split_cursor_model(None), ("", ""))
        self.assertEqual(split_cursor_model("highway"), ("highway", ""))


if __name__ == "__main__":
    unittest.main()
