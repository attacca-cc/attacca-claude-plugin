"""The manifests agree with each other, and CI fails when they stop agreeing.

Three ways a plugin manifest drifts silently, all of which ship a plugin that
installs cleanly and then does nothing:

  * `.mcp.json` names a `${user_config.x}` the manifest does not declare, so the
    MCP server gets the literal string and never connects;
  * `plugin.json` declares an option nothing reads (a typo in `hooks.json` or in
    the script's env name looks exactly like this);
  * `hooks.json` points at a file that is not in the repository.

`claude plugin validate` checks the manifest against its own schema, not against
the other two files, so this is a separate test and it runs in CI.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
MCP = ROOT / ".mcp.json"
HOOKS = ROOT / "hooks" / "hooks.json"
SCRIPT = ROOT / "hooks" / "attacca_memory.py"

USER_CONFIG_REF = re.compile(r"\$\{user_config\.([A-Za-z0-9_]+)\}")
PLUGIN_OPTION_ENV = re.compile(r"CLAUDE_PLUGIN_OPTION_([A-Z0-9_]+)")


def load(path):
    return json.loads(path.read_text())


def walk_strings(node):
    """Every string in a JSON document, keys included."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from walk_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_strings(item)


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifest = load(MANIFEST)
        self.user_config = self.manifest.get("userConfig", {})

    def test_every_user_config_reference_is_declared(self):
        refs = set()
        for path in (MCP, HOOKS, MANIFEST):
            for text in walk_strings(load(path)):
                refs.update(USER_CONFIG_REF.findall(text))
        self.assertTrue(refs, "no ${user_config.*} reference at all; the check would pass vacuously")
        undeclared = sorted(refs - set(self.user_config))
        self.assertEqual(
            undeclared,
            [],
            "these ${user_config.*} references are not declared in plugin.json userConfig, "
            "so Claude Code substitutes nothing and the server or hook gets a literal string: "
            + ", ".join(undeclared),
        )

    def test_every_plugin_option_env_is_declared(self):
        """The hook reads CLAUDE_PLUGIN_OPTION_<KEY>; <KEY> is the option, uppercased."""
        keys = {key.upper() for key in self.user_config}
        read = set(PLUGIN_OPTION_ENV.findall(SCRIPT.read_text()))
        self.assertTrue(read, "the hook reads no CLAUDE_PLUGIN_OPTION_* variable")
        undeclared = sorted(read - keys)
        self.assertEqual(
            undeclared,
            [],
            "the hook reads option names plugin.json does not declare (a typo here reads as "
            "'the option is unset'): " + ", ".join(undeclared),
        )

    def test_the_one_required_option_is_the_token(self):
        required = sorted(k for k, v in self.user_config.items() if v.get("required"))
        self.assertEqual(required, ["attacca_token"])
        self.assertTrue(self.user_config["attacca_token"].get("sensitive"),
                        "the token must be marked sensitive so it is stored as a credential")

    def test_defaults_match_the_documented_base(self):
        # A bare host answers 405 at the edge proxy and the hooks fail open, so
        # the default has to carry the /api prefix (attacca decision, 2026-11-23).
        self.assertEqual(self.user_config["attacca_url"]["default"], "https://attacca.cc/api")

    def test_hook_commands_point_at_files_in_the_repository(self):
        hooks = load(HOOKS)["hooks"]
        self.assertEqual(sorted(hooks), ["SessionEnd", "SessionStart", "Stop", "UserPromptSubmit"])
        for event, entries in hooks.items():
            for entry in entries:
                for hook in entry["hooks"]:
                    for arg in hook.get("args", []):
                        path = arg.replace("${CLAUDE_PLUGIN_ROOT}/", "")
                        if path.endswith(".py"):
                            self.assertTrue(
                                (ROOT / path).is_file(),
                                f"{event} runs {arg}, which is not in the repository",
                            )

    def test_the_marketplace_serves_this_plugin_from_its_root(self):
        market = load(MARKETPLACE)
        self.assertEqual(market["name"], "attacca-claude-plugin")
        entries = {p["name"]: p for p in market["plugins"]}
        self.assertIn(self.manifest["name"], entries,
                      "the marketplace does not list the plugin it ships")
        self.assertEqual(entries[self.manifest["name"]]["source"], "./")

    def test_versions_agree(self):
        market = load(MARKETPLACE)
        versions = {p["name"]: p.get("version") for p in market["plugins"]}
        declared = versions.get(self.manifest["name"])
        if declared is not None:
            self.assertEqual(declared, self.manifest["version"],
                             "marketplace.json and plugin.json disagree on the version")

    def test_no_stray_scratch_files_are_committed(self):
        """A manual hook run leaves hook_out.json behind; it should never be committed."""
        scratch = re.compile(r"^(hook_(out|err)|out|err).*\.(json|txt|log)$")
        stray = sorted(p.name for p in ROOT.iterdir() if p.is_file() and scratch.match(p.name))
        self.assertEqual(stray, [], "scratch files at the repository root: " + ", ".join(stray))


if __name__ == "__main__":
    unittest.main()
