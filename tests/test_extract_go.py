"""Go symbol extraction — interfaces and receiver-qualified methods."""

from __future__ import annotations

from mira.index.extract import extract_symbols, find_symbol_by_name

GRAFANA_STYLE = """\
package anonimpl

import "context"

type AnonDeviceService interface {
	TagDevice(ctx context.Context, device *Device) error
	CountDevices(ctx context.Context) (int64, error)
}

type AnonSessionService struct {
	limit int64
}

func (a *AnonSessionService) TagDevice(ctx context.Context, device *Device) error {
	if err := a.validate(device); err != nil {
		return err
	}
	return nil
}

func (a AnonSessionService) Limit() int64 {
	return a.limit
}

func ProvideAnonymousDeviceService(limit int64) *AnonSessionService {
	return &AnonSessionService{limit: limit}
}
"""


def test_go_interfaces_are_extracted():
    symbols = extract_symbols(GRAFANA_STYLE, "go")
    by_name = {s.name: s for s in symbols}
    assert by_name["AnonDeviceService"].kind == "interface"
    assert "TagDevice(ctx context.Context" in by_name["AnonDeviceService"].source
    assert by_name["AnonSessionService"].kind == "struct"


def test_go_methods_get_receiver_qualified_names():
    symbols = extract_symbols(GRAFANA_STYLE, "go")
    methods = {s.qualified_name: s for s in symbols if s.kind == "method"}
    assert "AnonSessionService.TagDevice" in methods  # pointer receiver
    assert "AnonSessionService.Limit" in methods  # value receiver
    assert methods["AnonSessionService.TagDevice"].name == "TagDevice"


def test_go_plain_functions_unqualified():
    symbols = extract_symbols(GRAFANA_STYLE, "go")
    by_name = {s.name: s for s in symbols}
    fn = by_name["ProvideAnonymousDeviceService"]
    assert fn.kind == "function"
    assert fn.qualified_name == ""


def test_go_find_by_qualified_name():
    span = find_symbol_by_name(GRAFANA_STYLE, "go", "AnonSessionService.TagDevice")
    assert span is not None
    assert "a.validate(device)" in span.source


def test_go_generic_receiver():
    src = "func (c *Cache[K, V]) Get(key K) (V, bool) {\n\treturn c.get(key)\n}\n"
    symbols = extract_symbols(src, "go")
    assert symbols[0].qualified_name == "Cache.Get"
