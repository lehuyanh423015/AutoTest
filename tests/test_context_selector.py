"""Offline, no-execution contracts for Phase 5C context selection."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from autotest.context_selector import (
    ContextSelectionError,
    ContextSelectionPolicy,
    ContextSelector,
    ContextTarget,
)
from autotest.main import main
from autotest.project_inspector import ProjectInspector

FIXTURE = Path(__file__).parent / "fixtures/projects/context_project"
TARGET = ContextTarget(Path("src/shop/pricing.py"), "calculate_total")


def _select(root: Path = FIXTURE, policy: ContextSelectionPolicy | None = None):
    profile = ProjectInspector().inspect(root)
    return ContextSelector().select(
        profile.root, profile, TARGET, policy or ContextSelectionPolicy()
    )


def _names(bundle):
    return {(item.path, item.symbol) for item in bundle.items}


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "sample"\nversion = "0.1"\n')
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return tmp_path


def test_fixture_static_selection_and_no_execution():
    bundle = _select()
    names = _names(bundle)
    assert ("src/shop/pricing.py", "calculate_total") in names
    assert ("src/shop/pricing.py", "normalize_total") in names
    assert ("src/shop/tax.py", "compute_tax") in names
    assert ("src/shop/tax.py", "TAX_RATE") in names
    assert ("src/shop/models.py", "Order") in names
    assert ("src/shop/config.py", "DEFAULT_DISCOUNT") in names
    assert ("src/shop/pricing.py", "unrelated_helper") not in names
    assert ("src/shop/tax.py", "unused_tax_helper") not in names
    assert not any(item.path.endswith("utils.py") for item in bundle.items)
    assert not any("unrelated_module" in item.source for item in bundle.items)
    assert not any("MUST NEVER BE IMPORTED" in item.source for item in bundle.items)
    assert bundle.direct_local_dependencies == 4
    assert bundle.recursive_local_dependencies == 1
    assert any(item.classification == "STDLIB" for item in bundle.external_references)
    assert any(item.reason == "STAR_IMPORT_UNRESOLVED" for item in bundle.unresolved_references)
    assert bundle.status == "PARTIAL"


def test_depth_zero_one_two():
    zero = _select(policy=ContextSelectionPolicy(max_dependency_depth=0))
    one = _select(policy=ContextSelectionPolicy(max_dependency_depth=1))
    two = _select()
    assert ("src/shop/tax.py", "compute_tax") not in _names(zero)
    assert ("src/shop/tax.py", "compute_tax") in _names(one)
    assert ("src/shop/tax.py", "TAX_RATE") not in _names(one)
    assert ("src/shop/tax.py", "TAX_RATE") in _names(two)
    assert any(item.reason == "DEPTH_LIMIT" for item in one.omitted_items)


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("src/shop/pricing.py:calculate_total", True),
        ("src/shop/pricing.py", False),
        ("src/shop/pricing.py:", False),
        ("src/shop/pricing.py:bad-name", False),
        ("../other.py:target", False),
        ("src/shop/pricing.txt:target", False),
    ],
)
def test_target_syntax(value, valid):
    if valid:
        assert ContextTarget.parse(value) == TARGET
    else:
        with pytest.raises(ContextSelectionError):
            ContextTarget.parse(value)


@pytest.mark.parametrize(
    "target",
    [
        ContextTarget(Path("src/shop/missing.py"), "calculate_total"),
        ContextTarget(Path("src/shop/pricing.py"), "missing"),
        ContextTarget(Path("../outside.py"), "function"),
        ContextTarget(Path("src/shop/models.py"), "__init__"),
    ],
)
def test_invalid_target(target):
    profile = ProjectInspector().inspect(FIXTURE)
    with pytest.raises(ContextSelectionError):
        ContextSelector().select(profile.root, profile, target)


def test_duplicate_target_and_malformed_target(tmp_path):
    root = _project(
        tmp_path, {"target.py": "def selected():\n    pass\ndef selected():\n    pass\n"}
    )
    profile = ProjectInspector().inspect(root)
    with pytest.raises(ContextSelectionError, match="ambiguously"):
        ContextSelector().select(root, profile, ContextTarget(Path("target.py"), "selected"))
    (root / "target.py").write_text("def selected(:\n")
    profile = ProjectInspector().inspect(root)
    with pytest.raises(ContextSelectionError):
        ContextSelector().select(root, profile, ContextTarget(Path("target.py"), "selected"))


def test_budget_omissions_and_no_partial_declarations():
    full = _select()
    target_size = len(full.items[0].source)
    with pytest.raises(ContextSelectionError, match="TARGET_EXCEEDS"):
        _select(policy=ContextSelectionPolicy(max_chars=target_size - 1))
    small = _select(policy=ContextSelectionPolicy(max_chars=target_size + 10))
    assert all(item.source in (FIXTURE / item.path).read_text() for item in small.items)
    assert any(item.reason == "CHARACTER_BUDGET" for item in small.omitted_items)
    one_file = _select(policy=ContextSelectionPolicy(max_files=1))
    assert len(one_file.selected_file_sha256) == 1
    assert any(item.reason == "FILE_BUDGET" for item in one_file.omitted_items)
    one_item = _select(policy=ContextSelectionPolicy(max_items=1))
    assert len(one_item.items) == 1
    assert any(item.reason == "ITEM_BUDGET" for item in one_item.omitted_items)


def test_deterministic_portable_hash_and_render(tmp_path):
    first = _select()
    second = _select()
    assert first.to_json() == second.to_json()
    assert first.render() == second.render()
    assert first.to_dict()["bundle_sha256"] == second.to_dict()["bundle_sha256"]
    assert first.bundle_sha256 == first.to_dict()["bundle_sha256"]
    copied = tmp_path / "copy"
    shutil.copytree(FIXTURE, copied)
    assert first.to_json() == _select(copied).to_json()
    assert str(FIXTURE.resolve()) not in first.to_json()
    assert "TARGET\n" in first.render()
    assert "RELEVANT IMPORTS" in first.render()
    assert "SUPPORTING CONTEXT" in first.render()
    assert "EXTERNAL REFERENCES" in first.render()
    assert "UNRESOLVED REFERENCES" in first.render()
    assert "Generate pytest" not in first.render()


def test_selected_hash_changes_but_unselected_content_does_not(tmp_path):
    copied = tmp_path / "copy"
    shutil.copytree(FIXTURE, copied)
    before = _select(copied).to_dict()["bundle_sha256"]
    unused = copied / "src/shop/utils.py"
    unused.write_text(unused.read_text().replace("star imports", "star-imports"))
    assert _select(copied).to_dict()["bundle_sha256"] == before
    selected = copied / "src/shop/tax.py"
    selected.write_text(selected.read_text().replace('"0.1"', '"0.2"'))
    assert _select(copied).to_dict()["bundle_sha256"] != before


def test_alias_module_import_relative_import_and_async(tmp_path):
    root = _project(
        tmp_path,
        {
            "src/shop/__init__.py": "",
            "src/shop/tax.py": "RATE = 2\nasync def tax(x):\n    return x * RATE\n",
            "src/shop/nested/__init__.py": "",
            "src/shop/nested/target.py": (
                "import shop.tax as taxes\nfrom ..tax import tax as other\n"
                "async def selected(x):\n    return await taxes.tax(x) + await other(x)\n"
            ),
        },
    )
    profile = ProjectInspector().inspect(root)
    bundle = ContextSelector().select(
        root, profile, ContextTarget(Path("src/shop/nested/target.py"), "selected")
    )
    assert ("src/shop/tax.py", "tax") in _names(bundle)
    assert ("src/shop/tax.py", "RATE") in _names(bundle)
    assert bundle.items[0].kind == "ASYNC_FUNCTION"


def test_unaliased_module_import_and_annotated_assignment(tmp_path):
    root = _project(
        tmp_path,
        {
            "src/p/__init__.py": "",
            "src/p/helper.py": "VALUE: int = 2\ndef compute(): return VALUE\n",
            "src/p/target.py": ("import p.helper\ndef selected(): return p.helper.compute()\n"),
        },
    )
    profile = ProjectInspector().inspect(root)
    bundle = ContextSelector().select(
        root, profile, ContextTarget(Path("src/p/target.py"), "selected")
    )
    assert ("src/p/helper.py", "compute") in _names(bundle)
    assert ("src/p/helper.py", "VALUE") in _names(bundle)
    assert any(i.kind == "CONSTANT_OR_ASSIGNMENT" for i in bundle.items)


def test_from_package_import_module_and_conditional_import(tmp_path):
    root = _project(
        tmp_path,
        {
            "src/p/__init__.py": "",
            "src/p/helper.py": "def compute(): return 1\n",
            "src/p/target.py": (
                "if True:\n    from . import helper as h\ndef selected(): return h.compute()\n"
            ),
        },
    )
    profile = ProjectInspector().inspect(root)
    bundle = ContextSelector().select(
        root, profile, ContextTarget(Path("src/p/target.py"), "selected")
    )
    assert ("src/p/helper.py", "compute") in _names(bundle)
    assert any(i.kind == "IMPORT" and "from . import helper" in i.source for i in bundle.items)


def test_missing_symbol_and_dynamic_lookup_are_recorded(tmp_path):
    root = _project(
        tmp_path,
        {
            "src/p/__init__.py": "",
            "src/p/helper.py": "other = 1\n",
            "src/p/target.py": (
                "from .helper import missing\ndef selected(): return getattr(missing, 'thing')\n"
            ),
        },
    )
    profile = ProjectInspector().inspect(root)
    bundle = ContextSelector().select(
        root, profile, ContextTarget(Path("src/p/target.py"), "selected")
    )
    reasons = {i.reason for i in bundle.unresolved_references}
    assert "MISSING_OR_AMBIGUOUS_LOCAL_SYMBOL" in reasons
    assert "DYNAMIC_REFERENCE_UNRESOLVED" in reasons
    assert not any(i.path.endswith("helper.py") for i in bundle.items)


def test_third_party_external_and_unbound_global(tmp_path):
    root = _project(
        tmp_path,
        {"target.py": "import requests\ndef selected(): return requests.get(url)\n"},
    )
    profile = ProjectInspector().inspect(root)
    bundle = ContextSelector().select(root, profile, ContextTarget(Path("target.py"), "selected"))
    assert any(i.classification == "THIRD_PARTY_OR_UNKNOWN" for i in bundle.external_references)
    assert any(
        i.reference == "url" and i.reason == "UNBOUND_GLOBAL" for i in bundle.unresolved_references
    )


def test_test_module_not_target_or_context(tmp_path):
    root = _project(
        tmp_path,
        {
            "target.py": "def selected(): return 1\n",
            "tests/test_target.py": "def test_selected(): assert True\n",
        },
    )
    profile = ProjectInspector().inspect(root)
    with pytest.raises(ContextSelectionError):
        ContextSelector().select(
            root, profile, ContextTarget(Path("tests/test_target.py"), "test_selected")
        )
    assert not any(
        i.path.startswith("tests/")
        for i in ContextSelector()
        .select(root, profile, ContextTarget(Path("target.py"), "selected"))
        .items
    )


def test_nested_function_and_method_are_not_targets(tmp_path):
    root = _project(
        tmp_path,
        {
            "target.py": (
                "def outer():\n    def nested(): pass\nclass C:\n    def method(self): pass\n"
            )
        },
    )
    profile = ProjectInspector().inspect(root)
    for name in ("nested", "method"):
        with pytest.raises(ContextSelectionError):
            ContextSelector().select(root, profile, ContextTarget(Path("target.py"), name))


def test_oversized_support_is_partial(tmp_path):
    root = _project(
        tmp_path,
        {
            "target.py": "from helper import value\ndef selected(): return value\n",
            "helper.py": "value = 1\n",
        },
    )
    profile = ProjectInspector().inspect(root)
    bundle = ContextSelector(max_source_file_bytes=65).select(
        root, profile, ContextTarget(Path("target.py"), "selected")
    )
    assert bundle.status == "COMPLETE"
    (root / "helper.py").write_text("value = 1\n" + "#" * 100)
    bundle = ContextSelector(max_source_file_bytes=65).select(
        root, profile, ContextTarget(Path("target.py"), "selected")
    )
    assert any(i.reason == "SUPPORT_MODULE_UNREADABLE" for i in bundle.unresolved_references)


def test_decorators_defaults_annotations_and_local_filtering(tmp_path):
    root = _project(
        tmp_path,
        {
            "target.py": (
                "RATE = 3\nclass Model: pass\n"
                "def decorator(f): return f\n"
                "@decorator\ndef selected(x: Model, y=RATE):\n"
                "    local = 1\n    return len([local for local in range(x)]) + y\n"
            )
        },
    )
    profile = ProjectInspector().inspect(root)
    bundle = ContextSelector().select(root, profile, ContextTarget(Path("target.py"), "selected"))
    assert {"RATE", "Model", "decorator"}.issubset({i.symbol for i in bundle.items})
    assert bundle.items[0].source.startswith("@decorator")
    assert bundle.items[0].start_line == 4
    assert not any(i.symbol in ("local", "len", "range", "x") for i in bundle.items[1:])


def test_cycle_and_missing_local_symbol(tmp_path):
    root = _project(
        tmp_path,
        {
            "src/p/__init__.py": "",
            "src/p/a.py": "from .b import helper_b\ndef selected(): return helper_b()\n",
            "src/p/b.py": "from .a import selected\ndef helper_b(): return selected()\n",
        },
    )
    profile = ProjectInspector().inspect(root)
    bundle = ContextSelector().select(root, profile, ContextTarget(Path("src/p/a.py"), "selected"))
    assert sum(i.symbol == "selected" for i in bundle.items) == 1
    assert ("src/p/b.py", "helper_b") in _names(bundle)
    assert len(bundle.items) < 10


def test_malformed_support_is_partial(tmp_path):
    root = _project(
        tmp_path,
        {
            "src/p/__init__.py": "",
            "src/p/a.py": "from .b import helper\ndef selected(): return helper()\n",
            "src/p/b.py": "def helper(:\n",
        },
    )
    profile = ProjectInspector().inspect(root)
    bundle = ContextSelector().select(root, profile, ContextTarget(Path("src/p/a.py"), "selected"))
    assert bundle.status == "PARTIAL"
    assert any(i.reason == "SUPPORT_MODULE_UNREADABLE" for i in bundle.unresolved_references)


def test_ambiguous_module_name(tmp_path):
    root = _project(
        tmp_path,
        {
            "a.py": "from x import helper\ndef selected(): return helper()\n",
            "x.py": "def helper(): return 1\n",
            "y.py": "def helper(): return 2\n",
        },
    )
    profile = ProjectInspector().inspect(root)
    x = next(i for i in profile.modules if i.path.name == "x.py")
    profile = replace(profile, modules=(*profile.modules, replace(x, path=root / "y.py")))
    bundle = ContextSelector().select(root, profile, ContextTarget(Path("a.py"), "selected"))
    assert any(i.reason == "AMBIGUOUS_LOCAL_MODULE" for i in bundle.unresolved_references)


def test_cli_persists_fresh_static_artifacts(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("No LLM or environment provisioning in context mode")

    monkeypatch.setattr("autotest.main.OllamaProvider", forbidden)
    monkeypatch.setattr("autotest.main.EnvironmentProvisioner", forbidden)
    output = tmp_path / "bundles"
    args = [
        "--select-context",
        str(FIXTURE),
        "--context-target",
        "src/shop/pricing.py:calculate_total",
        "--context-output-root",
        str(output),
        "--context-max-depth",
        "1",
        "--context-max-chars",
        "5000",
        "--context-max-files",
        "8",
        "--context-max-items",
        "24",
    ]
    assert main(args) == 0
    assert main(args) == 0
    assert "Context selection: PARTIAL" in capsys.readouterr().out
    directories = list(output.iterdir())
    assert len(directories) == 2
    for directory in directories:
        data = json.loads((directory / "context_bundle.json").read_text())
        assert data["policy"]["max_dependency_depth"] == 1
        assert "TARGET" in (directory / "context.txt").read_text()


@pytest.mark.parametrize(
    "args",
    [
        ["--select-context", str(FIXTURE)],
        ["--select-context", str(FIXTURE), "--context-target", "bad"],
        ["--select-context", str(FIXTURE), "--context-target", "src/shop/pricing.py:missing"],
        [
            "--select-context",
            str(FIXTURE),
            "--inspect-project",
            str(FIXTURE),
            "--context-target",
            "src/shop/pricing.py:calculate_total",
        ],
    ],
)
def test_cli_errors_are_exit_two(args):
    try:
        result = main(args)
    except SystemExit as exc:
        result = exc.code
    assert result == 2
