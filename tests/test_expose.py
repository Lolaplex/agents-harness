"""expose_commands: discovered commands become their own module rows."""

import unittest

from runner.cordis_tools import _arguments_to_argv
from runner.helpjson import call_effects
from runner.modules import find_module, list_modules


class TestExposeCommands(unittest.TestCase):
    def test_exposed_rows_keep_names(self) -> None:
        for name in (
            "mcp.calendar.add",
            "mcp.calendar.list",
            "mcp.calendar.delete",
            "mcp.traces.audit",
            "mcp.traces.seal",
        ):
            self.assertIsNotNone(find_module(name), name)

    def test_exposed_row_pins_command(self) -> None:
        mod = find_module("mcp.calendar.add")
        self.assertEqual(mod.get("command_pin"), "add")
        self.assertEqual(mod.get("exposed_from"), "mcp.calendar")
        argv = _arguments_to_argv(mod, {"summary": "S", "dtstart": "a", "dtend": "b"})
        self.assertEqual(argv[0], "add")
        self.assertIn("S", argv)

    def test_parent_module_keeps_all_commands(self) -> None:
        mod = find_module("mcp.calendar")
        self.assertIsNotNone(mod)
        self.assertEqual(mod.get("command_pin"), None)
        argv = _arguments_to_argv(mod, {"command": "delete", "href": "/e", "etag": "1"})
        self.assertEqual(argv[:1], ["delete"])

    def test_exposed_side_effects(self) -> None:
        self.assertEqual(
            call_effects(find_module("mcp.calendar.list"), {"command": "list"}),
            "read-only",
        )
        self.assertEqual(
            call_effects(find_module("mcp.traces.seal"), {"command": "seal"}),
            "mutating",
        )

    def test_removed_sub_manifests_replaced_by_exposure(self) -> None:
        names = {r["name"] for r in list_modules()}
        self.assertIn("mcp.calendar", names)
        self.assertIn("mcp.traces", names)
        # old files deleted; same names now synthesized from help-json
        self.assertIn("mcp.calendar.add", names)
        self.assertIn("mcp.traces.audit", names)


if __name__ == "__main__":
    unittest.main()
