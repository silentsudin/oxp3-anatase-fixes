# SPDX-License-Identifier: MIT
"""Tests for tools/lib/dxvk-steam-default.py.

The installer's status line names the Proton Steam would actually launch a game with, and
config.vdf nests one block per app inside CompatToolMapping: a naive regex picks the first
entry instead of the "0" (default) one, which is how this parser came to be a file of its own.
"""
from __future__ import annotations

import unittest

import support

MOD = support.load("dxvk_steam_default", "lib/dxvk-steam-default.py")

VDF = '''
"InstallConfigStore"
{
	"Software"
	{
		"Valve"
		{
			"Steam"
			{
				"CompatToolMapping"
				{
					"1245620"
					{
						"name"		"proton-stable"
						"config"		""
						"Priority"		"250"
					}
					"0"
					{
						"name"		"Proton-CachyOS Latest"
						"config"		""
						"Priority"		"250"
					}
					"1623730"
					{
						"name"		"proton_experimental"
						"config"		""
						"Priority"		"250"
					}
				}
			}
		}
	}
}
'''


class DefaultToolTest(unittest.TestCase):
    def test_returns_the_zero_entry_not_the_first_app(self):
        self.assertEqual(MOD.default_tool(VDF), "Proton-CachyOS Latest")

    def test_missing_mapping_returns_empty(self):
        self.assertEqual(MOD.default_tool('"Steam" { "Other" "x" }'), "")

    def test_missing_default_key_returns_empty(self):
        only_app = VDF.replace('"0"', '"999"')
        self.assertEqual(MOD.default_tool(only_app), "")

    def test_nested_blocks_do_not_break_the_brace_walk(self):
        # an app entry containing a nested block must not be mistaken for the end of the mapping
        nested = VDF.replace('"config"		"",\n						"Priority"		"250"\n					}\n					"0"',
                             '"config"		"",\n						"Priority"		"250",\n						"Extra" { "a" "b" }\n					}\n					"0"')
        self.assertEqual(MOD.default_tool(nested), "Proton-CachyOS Latest")


if __name__ == "__main__":
    unittest.main()
