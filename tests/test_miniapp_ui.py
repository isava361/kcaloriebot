"""Opt-in browser checks: RUN_MINIAPP_UI=1 python -m unittest tests.test_miniapp_ui."""

import asyncio
import os
import unittest
from pathlib import Path

from kcaloriebot.domain import SessionState
from tests import test_web


@unittest.skipUnless(os.environ.get("RUN_MINIAPP_UI") == "1", "Opt-in Playwright check")
class BrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_web.WebTests.asyncSetUp

    async def test_mobile_flows(self):
        self.store.set_timezone(123, "Europe/Moscow")
        self.store.add_favorite(123, "Бедро куриное", 170, 20, 10, None)
        favorite = self.store.add_favorite(
            123, "Батончик ореховый", 400, None, None, None
        )
        self.store.start_session(
            123,
            123,
            SessionState.WAIT_FAVORITE_TO_SERVING,
            selected_favorite_id=favorite.favorite_id,
        )
        self.store.convert_favorite_to_serving(123, 123, 50)
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
