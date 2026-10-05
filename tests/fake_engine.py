"""Compatibility shim: the fake engine now lives in ctxproxy.fake_engine (used by the demo and CI too)."""
from ctxproxy.fake_engine import FakeEngine, render  # noqa: F401
