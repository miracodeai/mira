from mira.core.file_types import (
    Language,
    extension_from_path,
    is_indexable_path,
    language_from_extension,
    language_from_path,
    normalize_extension,
)


def test_extension_normalization_is_case_insensitive():
    assert normalize_extension(" TSX ") == ".tsx"
    assert extension_from_path("src/Widget.TSX") == ".tsx"


def test_language_lookup_accepts_extensions_and_paths():
    assert language_from_extension(".cs") == Language.CSHARP.value
    assert language_from_path("src/main.KTS") == Language.KOTLIN.value
    assert language_from_path("README", default="text") == "text"


def test_indexability_uses_the_shared_normalized_extensions():
    assert is_indexable_path("src/main.PY")
    assert is_indexable_path("schema.graphql")
    assert not is_indexable_path("README.md")


def test_indexability_covers_broader_platform_languages():
    for path in (
        "src/App.vue",
        "src/main.ex",
        "src/main.dart",
        "src/main.hs",
        "infra/main.tf",
        "scripts/deploy.ps1",
        "Dockerfile",
        "Makefile",
        "CMakeLists.txt",
    ):
        assert is_indexable_path(path)


def test_filename_detection_takes_precedence_over_extension():
    assert language_from_path("Dockerfile") == Language.DOCKERFILE.value
    assert language_from_path("Dockerfile.dev") == Language.DOCKERFILE.value
    assert language_from_path("Makefile") == Language.MAKEFILE.value
    assert language_from_path("Makefile.am") == Language.MAKEFILE.value
    assert language_from_path("CMakeLists.txt") == Language.CMAKE.value


def test_prose_and_dotenv_files_are_not_indexed():
    assert not is_indexable_path("docs/guide.rst")
    assert not is_indexable_path(".env")
