"""Existing parser/adapter tests isolate pacing; test_network exercises real policy."""
import unittest
from unittest.mock import Mock, patch

from school_mcp.network import RequestLimiter


def isolate_pacing():
    stub = Mock(spec=RequestLimiter)
    stub.failure.return_value = 2.0
    replacement = patch("school_mcp.network.limiter", stub)
    replacement.start()
    unittest.addModuleCleanup(replacement.stop)
