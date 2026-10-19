"""Compatibility shim: the fake engine now lives in ctxproxy.fake_engine (used by the demo and CI too)."""
from ctxproxy.fake_engine import FakeEngine, FakeRouter, render  # noqa: F401
