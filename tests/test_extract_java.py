"""Java symbol extraction — methods inside classes, qualified names.

Regression coverage for the bug where _extract_java skipped entire class
bodies, so Java methods were never extracted (keycloak cross-file misses).
"""

from __future__ import annotations

from mira.index.extract import extract_symbols, find_symbol_by_name

KEYCLOAK_STYLE = """\
package org.keycloak.common.crypto;

import java.security.Provider;
import java.security.Security;

public class CryptoIntegration {

    private static volatile CryptoProvider cryptoProvider;

    public static CryptoProvider getProvider() {
        if (cryptoProvider == null) {
            init(CryptoIntegration.class.getClassLoader());
        }
        return cryptoProvider;
    }

    public static Provider getSecurityProvider(String name) {
        Provider provider = Security.getProvider(name);
        if (provider == null) {
            provider = cryptoProvider.getBouncyCastleProvider();
        }
        return provider;
    }

    private static void init(ClassLoader classLoader) {
        cryptoProvider = lookup(classLoader);
    }
}
"""


def test_java_methods_are_extracted():
    symbols = extract_symbols(KEYCLOAK_STYLE, "java")
    names = {s.name for s in symbols}
    assert "CryptoIntegration" in names
    assert {"getProvider", "getSecurityProvider", "init"} <= names


def test_java_methods_get_qualified_names():
    symbols = extract_symbols(KEYCLOAK_STYLE, "java")
    by_name = {s.name: s for s in symbols}
    assert by_name["getSecurityProvider"].qualified_name == "CryptoIntegration.getSecurityProvider"
    assert by_name["getSecurityProvider"].kind == "method"
    assert "getBouncyCastleProvider" in by_name["getSecurityProvider"].source


def test_find_symbol_by_qualified_name():
    span = find_symbol_by_name(KEYCLOAK_STYLE, "java", "CryptoIntegration.getProvider")
    assert span is not None
    assert span.name == "getProvider"
    assert find_symbol_by_name(KEYCLOAK_STYLE, "java", "getProvider") is not None


def test_java_statements_are_not_methods():
    symbols = extract_symbols(KEYCLOAK_STYLE, "java")
    names = {s.name for s in symbols}
    # `return cryptoProvider;` / `init(...)` calls inside bodies must not match
    assert "cryptoProvider" not in names
    assert "lookup" not in names


def test_java_interface_enum_record():
    source = """\
public interface CryptoProvider {
    Provider getBouncyCastleProvider();
    <T> T getAlgorithmProvider(Class<T> clazz, String algorithm);
}

enum Mode { STRICT, LENIENT }

public record KeyWrapper(String kid, String algorithm) {
    public String describe() {
        return kid + "/" + algorithm;
    }
}
"""
    symbols = extract_symbols(source, "java")
    by_name = {s.name: s for s in symbols}
    assert by_name["CryptoProvider"].kind == "class"
    assert "Mode" in by_name
    assert "KeyWrapper" in by_name
    assert by_name["describe"].qualified_name == "KeyWrapper.describe"
    # Braceless interface method: span is its own line, doesn't swallow siblings
    bc = by_name["getBouncyCastleProvider"]
    assert bc.start_line == bc.end_line == 2
    assert bc.qualified_name == "CryptoProvider.getBouncyCastleProvider"


def test_java_constructor_and_nested_class():
    source = """\
public class Outer {
    private final int x;

    public Outer(int x) {
        this.x = x;
    }

    public static class Builder {
        public Outer build() {
            return new Outer(1);
        }
    }

    public int value() {
        return x;
    }
}
"""
    symbols = extract_symbols(source, "java")
    by_qual = {s.qualified_name: s for s in symbols if s.qualified_name}
    assert "Outer.Outer" in by_qual  # constructor
    assert by_qual["Builder.build"].name == "build"
    # value() comes after Builder closes — must re-qualify against Outer
    assert "Outer.value" in by_qual
    assert {s.name for s in symbols if s.kind == "class"} == {"Outer", "Builder"}
