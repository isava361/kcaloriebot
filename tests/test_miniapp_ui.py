"""Opt-in browser checks: RUN_MINIAPP_UI=1 python -m unittest tests.test_miniapp_ui."""

import asyncio
import os
import unittest
from pathlib import Path

from tests import test_web


@unittest.skipUnless(os.environ.get("RUN_MINIAPP_UI") == "1", "Opt-in Playwright check")
class BrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_web.WebTests.asyncSetUp

    async def test_mobile_flows(self):
        self.store.set_timezone(123, "Europe/Moscow")
        process = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).with_name("miniapp_ui.cjs")),
            str(self.client.make_url("/")),
            test_web.signed(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 120)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        self.assertEqual(
            process.returncode, 0, output.decode("utf-8", errors="replace")
        )
