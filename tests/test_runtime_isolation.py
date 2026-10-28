"""Fresh-process check, including startup imports, with no real config or network."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class RuntimeIsolationTests(unittest.TestCase):
    def test_startup_ignores_optional_sdk_and_global_credentials_without_key(self):
        source = r'''
import asyncio
import builtins
import socket
import sys
from unittest.mock import AsyncMock, Mock, patch

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.startswith(("opik", "relay_bot_opik", "sentry_sdk")):
        raise AssertionError("optional SDK imported")
    return original_import(name, *args, **kwargs)
def no_network(*args, **kwargs):
    raise AssertionError("unexpected network")
builtins.__import__ = guarded_import
socket.socket.connect = no_network
socket.getaddrinfo = no_network

from tg_summary_bot import bot, config, observability
with patch.object(config, "load_dotenv"):
    settings = config.load_settings()
async def check():
    with patch.object(bot, "load_settings", return_value=settings), \
         patch.object(bot, "setup_logging"), patch.object(bot, "run_bot", new_callable=AsyncMock) as run:
        await bot.main()
        run.assert_awaited_once_with(settings)
    assert not any(name.startswith(("opik", "relay_bot_opik", "sentry_sdk")) for name in sys.modules)
asyncio.run(check())
'''
        root = Path(__file__).resolve().parents[1]
        env = {"PATH": os.defpath, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1",
               "TELEGRAM_BOT_TOKEN": "123:synthetic", "OPIK_ENABLED": "true",
               "OPIK_CONFIG_PATH": "/must-not-read-sdk-credentials", "TRACING_API_KEY": ""}
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-c", source], cwd=directory, env=env,
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
