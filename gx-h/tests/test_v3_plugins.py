"""Operator-selected plugin snapshots, same-process versioning and failure boundaries."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import py_compile
import shutil
import sys
import tempfile
import threading
import unittest

from gx_core.plugins.author_api import RegistrationContext
from gx_core.plugins.catalog import Pin, package_digest
from gx_core.plugins.loader import bootstrap, namespace_for
from gx_core.plugins.manifest import parse_manifest


EXAMPLE = Path(__file__).resolve().parents[2] / "examples/plugins/hello_research"
VERSION = "1.0.0"
TOOL = {"id": "hello_research", "contract": "tool", "version": "1.0"}


def manifest(name, version=VERSION, **fields):
    return {
        "id": name, "version": version, "api_version": "1.0", "kind": "bundle",
        "entrypoint": "plugin:register", "gx_compat": ">=2.1,<4.0",
        "dependencies": [], "extensions": [], **fields,
    }


def dependency(name, *, optional=False):
    return {"id": name, "version": VERSION, "optional": optional}


def under_namespace(name):
    return {key for key in sys.modules if key == name or key.startswith(name + ".")}


class PluginSnapshots(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.root = self.work / "installed"
        self.catalog = self.work / "workflow_packages.json"
        self.entries = []
        self.save_catalog()

    def save_catalog(self):
        self.catalog.write_text(json.dumps({"plugins": self.entries}))

    def approve(self, path):
        locator = path.relative_to(self.root).as_posix()
        record = {"path": locator, "sha256": package_digest(path)}
        self.entries = [row for row in self.entries if row["path"] != locator] + [record]
        self.save_catalog()
        return record["sha256"]

    def package(self, name, version=VERSION, source="def register(ctx): pass\n", **fields):
        path = self.root / name / version
        path.mkdir(parents=True)
        (path / "gx_plugin.json").write_text(json.dumps(manifest(name, version, **fields)))
        (path / "plugin.py").write_text(source)
        self.approve(path)
        return path

    def load(self, *names):
        pairs = [(item, VERSION) if isinstance(item, str) else item for item in names]
        return bootstrap(self.root, self.catalog, pairs)

    def example(self, name, version, salutation):
        directory = self.root / name / version
        shutil.copytree(EXAMPLE, directory)
        meta = directory / "gx_plugin.json"
        doc = json.loads(meta.read_bytes())
        meta.write_text(json.dumps({**doc, "id": name, "version": version}))
        messages = directory / "package/text/messages.py"
        messages.write_text("calls = 0\n\ndef greeting(name):\n"
                            "    global calls\n    calls += 1\n"
                            f"    return f'{salutation} {{name}} ({{calls}})'\n")
        return directory, self.approve(directory)

    def marker_source(self, marker):
        return f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"

    def test_strict_manifest_and_compatibility(self):
        base = manifest("org.test.manifest")
        self.assertEqual(parse_manifest(json.dumps(base).encode()).id, base["id"])
        invalid = [
            {key: value for key, value in base.items() if key != "entrypoint"},
            {**base, "unknown": 1}, {**base, "id": "bad id"}, {**base, "version": "1.0"},
            {**base, "api_version": "2.0"}, {**base, "kind": "runner"},
            {**base, "entrypoint": ".name:register"}, {**base, "gx_compat": ">=4.0"},
            {**base, "gx_compat": ">=2.1,typo"}, {**base, "dependencies": {}},
            {**base, "dependencies": [{"id": "org.test.dep", "version": ">=1.0"}]},
            {**base, "dependencies": [{**dependency("org.test.dep"), "optional": "yes"}]},
            {**base, "dependencies": [dependency("org.test.dep")] * 2},
            {**base, "extensions": [{"id": "thing", "contract": "tool"}]},
            {**base, "extensions": [TOOL, TOOL]}, {**base, "labels": {"tier": 1}},
        ]
        for doc in invalid:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                parse_manifest(json.dumps(doc).encode())
        duplicate = json.dumps(base).replace('"kind":', '"kind":"workflow","kind":')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_manifest(duplicate.encode())

    def test_content_digest_depends_on_paths_and_all_file_bytes(self):
        version = self.package("org.test.digest")
        (version / "data").mkdir()
        (version / "data/reading.txt").write_text("reviewed text")
        pairs = [[path.relative_to(version).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()]
                 for path in sorted(version.rglob("*")) if path.is_file()]
        canonical = json.dumps(pairs, separators=(",", ":"), ensure_ascii=False).encode()
        expected = hashlib.sha256(canonical).hexdigest()
        self.assertEqual(package_digest(version), expected)
        moved = self.work / "copy"
        shutil.copytree(version, moved)
        self.assertEqual(package_digest(moved), expected)
        (moved / "data/reading.txt").rename(moved / "data/renamed.txt")
        self.assertNotEqual(package_digest(moved), expected)
        (version / "data/reading.txt").write_text("revised text")
        self.assertNotEqual(package_digest(version), expected)

    def test_rejected_package_never_runs_its_import_marker(self):
        for reason in ("unapproved", "digest", "api", "manifest", "lint", "locator"):
            with self.subTest(reason=reason):
                name = "org.test.rejected-" + reason
                marker = self.work / (reason + ".imported")
                source = self.marker_source(marker) + "def register(ctx): pass\n"
                if reason == "lint":
                    source += "import numpy\n"
                path = self.package(name, source=source, api_version="2.0" if reason == "api" else "1.0")
                if reason == "unapproved":
                    self.entries.pop()
                    self.save_catalog()
                elif reason == "digest":
                    (path / "notes.txt").write_text("new material after approval")
                elif reason == "manifest":
                    (path / "gx_plugin.json").write_text("{}")
                    self.approve(path)
                elif reason == "locator":
                    elsewhere = self.work / "outside-install"
                    path.rename(elsewhere)
                    path.symlink_to(elsewhere, target_is_directory=True)
                result = self.load(name)
                self.assertIn((name, VERSION), result.failures)
                self.assertNotIn((name, VERSION), result.order)
                self.assertFalse(marker.exists())

    def test_namespaces_include_all_three_identity_fields(self):
        name = "org.test.hello-world"
        digest = "a" * 64
        generated = namespace_for(name, VERSION, digest)
        self.assertEqual(generated, namespace_for(name, VERSION, digest))
        self.assertTrue(generated.startswith("gx_ext_") and generated.isidentifier())
        self.assertEqual(len({generated, namespace_for(name, "2.0.0", digest),
                              namespace_for(name, VERSION, "b" * 64),
                              namespace_for("org.test.hello_world", VERSION, digest)}), 4)

    def test_versions_lazy_imports_globals_and_selection_snapshots(self):
        name = "org.test.versioned"
        one, two = (name, VERSION), (name, "2.0.0")
        path1, digest1 = self.example(*one, "Hello")
        _, digest2 = self.example(*two, "Welcome")
        before_path = sys.path[:]
        bare = {key: sys.modules.get(key) for key in ("plugin", "package", "package.tools")}
        both = self.load(two, one)
        self.assertEqual(both.failures, {})
        first, second = both.get(*one, digest1), both.get(*two, digest2)
        self.assertEqual([first.namespace, second.namespace],
                         [namespace_for(*one, digest1), namespace_for(*two, digest2)])
        for loaded in (first, second):
            self.assertIn(loaded.namespace + ".package.tools", sys.modules)
            self.assertNotIn(loaded.namespace + ".package.text.messages", sys.modules)
        with self.assertRaises(ValueError):
            both.get(*one, digest2)
        first_call = first.contributions.tools["hello_research"].handler
        second_call = second.contributions.tools["hello_research"].handler
        (path1 / "package/text/messages.py").write_text("def greeting(name): return 'new disk text'\n")
        self.assertEqual(first_call(None, {"name": "Ada"}), {"greeting": "Hello Ada (1)"})
        self.assertEqual(second_call(None, {"name": "Ada"}), {"greeting": "Welcome Ada (1)"})
        self.assertEqual(first_call(None, {"name": "Lin"}), {"greeting": "Hello Lin (2)"})
        unavailable = self.load(one, two)
        self.assertIn(one, unavailable.failures)
        self.assertEqual(unavailable.order, (two,))
        self.assertEqual(self.load(two).order, (two,))
        self.assertEqual(self.load().order, ())
        self.assertEqual(both.order, (one, two))
        self.assertEqual(second_call(None, {"name": "Lin"}), {"greeting": "Welcome Lin (2)"})
        self.assertEqual(sys.path, before_path)
        self.assertEqual({key: sys.modules.get(key) for key in bare}, bare)

    def test_independent_installations_retain_their_exact_content(self):
        key = ("org.test.snapshots", VERSION)
        directory, digest1 = self.example(*key, "Original")
        original = self.load(key).get(*key, digest1)
        second_root = self.work / "other-install"
        other = second_root.joinpath(*key)
        shutil.copytree(directory, other)
        file = other / "package/text/messages.py"
        file.write_text("def greeting(name): return 'Revised ' + name\n")
        digest2 = package_digest(other)
        catalog = self.work / "other-catalog.json"
        catalog.write_text(json.dumps({"plugins": [{"path": "/".join(key), "sha256": digest2}]}))
        second = bootstrap(second_root, catalog, [Pin(*key, digest2)]).get(*key, digest2)
        self.assertNotEqual(original.namespace, second.namespace)
        self.assertEqual(original.contributions.tools["hello_research"].handler(None, {"name": "Ada"}),
                         {"greeting": "Original Ada (1)"})
        self.assertEqual(second.contributions.tools["hello_research"].handler(None, {"name": "Ada"}),
                         {"greeting": "Revised Ada"})
        self.assertEqual(bootstrap(second_root, catalog, [Pin(*key, digest1)]).order, ())
        self.assertNotEqual(original.read_file("package/text/messages.py"), second.read_file("package/text/messages.py"))

    def test_verified_source_is_used_with_existing_local_bytecode(self):
        name = "org.test.source"
        directory, _ = self.example(name, VERSION, "Original")
        messages = directory / "package/text/messages.py"
        cached = Path(py_compile.compile(str(messages), doraise=True))
        messages.write_text("def greeting(name): return 'reviewed greeting'\n")
        digest = self.approve(directory)
        before = cached.read_bytes()
        plugin = self.load(name).get(name, VERSION, digest)
        result = plugin.contributions.tools["hello_research"].handler(None, {"name": "Ada"})
        self.assertEqual(result, {"greeting": "reviewed greeting"})
        self.assertEqual(cached.read_bytes(), before)
        self.assertEqual(package_digest(directory), digest)

    def test_concurrent_registration_once_and_deep_freeze(self):
        name = "org.test.concurrent"
        source = (
            "import time\n"
            "registered = 0\n"
            "schema = {'type':'object','properties':{'name':{'type':'string'}}}\n"
            "def register(ctx):\n"
            "    global registered, registrar\n"
            "    registrar = ctx.tools\n"
            "    time.sleep(.04)\n"
            "    registered += 1\n"
            "    registrar.register(name='hello_research', schema=schema, handler=status)\n"
            "def status(*args): return registered\n"
        )
        self.package(name, source=source, extensions=[TOOL])
        barrier = threading.Barrier(6)

        def concurrent_load():
            barrier.wait(timeout=10)
            return self.load(name)

        with ThreadPoolExecutor(max_workers=6) as threads:
            results = [threads.submit(concurrent_load) for _ in range(6)]
            registries = [future.result(timeout=15) for future in results]
        registries.append(self.load(name))
        loaded = [registry.get(name, VERSION) for registry in registries]
        handler = loaded[0].contributions.tools["hello_research"].handler
        self.assertEqual(handler(), 1)
        self.assertTrue(all(plugin.contributions.tools["hello_research"].handler is handler for plugin in loaded))
        module = sys.modules[handler.__module__]
        module.schema["properties"]["name"]["type"] = "integer"
        tool = loaded[0].contributions.tools["hello_research"]
        published = tool.schema
        self.assertEqual(published["properties"]["name"]["type"], "string")
        descriptor = tool.as_spec()
        self.assertEqual(json.loads(json.dumps(descriptor))["name"], "hello_research")
        descriptor["inputSchema"]["properties"]["name"]["type"] = "number"
        self.assertEqual(tool.as_spec()["inputSchema"]["properties"]["name"]["type"], "string")
        with self.assertRaises(TypeError):
            published["properties"]["name"]["type"] = "boolean"
        with self.assertRaises((ValueError, RuntimeError)):
            module.registrar.register(name="later", schema={"type": "object"}, handler=handler)
        with self.assertRaises(TypeError):
            registries[-1].failures[(name, VERSION)] = "late"
        self.assertEqual(handler(), 1)

    def test_workflow_binding_and_capability_references_freeze(self):
        ctx = RegistrationContext()

        def label(task):
            return {"task_id": task["id"]}

        hooks = {"build_task_context": label}
        ctx.workflows.register(workflow_id="fixture", version="1", hooks=hooks)
        ctx.capabilities.add_manifest("fixture-grid@1")
        ctx.capabilities.add_manifest("capability-manifests/example.json")
        with self.assertRaises(ValueError):
            ctx.capabilities.add_manifest("../another-bundle.json")
        saved = ctx.freeze()
        self.assertIs(ctx.freeze(), saved)
        hooks.clear()
        workflow = saved.workflows[("fixture", "1")]
        self.assertEqual(workflow.hooks["build_task_context"]({"id": "task1"}), {"task_id": "task1"})
        self.assertEqual(set(saved.capabilities), {"fixture-grid@1", "capability-manifests/example.json"})
        with self.assertRaises(TypeError):
            workflow.hooks["build_task_context"] = None
        with self.assertRaises(RuntimeError):
            ctx.capabilities.add_manifest("fixture-grid@2")

    def test_failed_import_and_register_remove_only_their_modules(self):
        one = ("org.test.cleanup", VERSION)
        directory, digest = self.example(*one, "Hello")
        healthy = self.load(one).get(*one, digest)
        call = healthy.contributions.tools["hello_research"].handler
        self.assertEqual(call(None, {"name": "Ada"}), {"greeting": "Hello Ada (1)"})
        before = under_namespace(healthy.namespace)
        for version, phase in (("2.0.0", "import"), ("3.0.0", "registration")):
            with self.subTest(phase=phase):
                other = (one[0], version)
                bad, _ = self.example(*other, "Bad")
                marker = self.work / (version + ".entered")
                code = (bad / "plugin.py").read_text()
                # These failures occur after a nested module loaded or a tool was registered.
                if phase == "import":
                    code += "\n" + self.marker_source(marker) + "raise ValueError('example import failure')\n"
                else:
                    code += ("    from pathlib import Path\n" + f"    Path({str(marker)!r}).touch()\n"
                             "    raise ValueError('example registration failure')\n")
                (bad / "plugin.py").write_text(code)
                bad_digest = self.approve(bad)
                dependent = "org.test.consumer-" + phase
                self.package(dependent, dependencies=[{"id": other[0], "version": other[1]}])
                result = self.load(dependent, other, one)
                self.assertTrue(marker.is_file(), result.failures)
                self.assertIn("example " + phase + " failure", result.failures[other])
                self.assertIn((dependent, VERSION), result.failures)
                self.assertEqual(result.order, (one,))
                self.assertFalse(under_namespace(namespace_for(*other, bad_digest)))
                self.assertEqual(under_namespace(healthy.namespace), before)
        self.assertEqual(call(None, {"name": "Ada"}), {"greeting": "Hello Ada (2)"})
        self.assertEqual(package_digest(directory), digest)

    def test_dependency_order_diamond_and_optional_absence(self):
        root, left, right, common, missing = [
            "org.test.graph." + leaf for leaf in ("root", "left", "right", "common", "optional")]
        self.package(root, dependencies=[dependency(right), dependency(missing, optional=True), dependency(left)])
        self.package(right, dependencies=[dependency(common)])
        self.package(left, dependencies=[dependency(common)])
        self.package(common)
        expected = tuple((name, VERSION) for name in (common, left, right, root))
        first = self.load(root, left)
        self.assertEqual(first.failures, {})
        self.assertEqual(first.order, expected)
        self.entries.reverse()
        self.save_catalog()
        self.assertEqual(self.load(left, root).order, expected)
        self.package(missing, gx_compat=">=4.0")
        broken = self.load(root, left)
        self.assertIn((root, VERSION), broken.failures)
        self.assertIn(missing, broken.failures[(root, VERSION)])
        self.assertIn((left, VERSION), broken.order)

    def test_missing_dependency_cycle_and_unrelated_selection(self):
        a, b, c, missing, independent = [
            "org.test.resolve." + leaf for leaf in ("a", "b", "c", "missing", "independent")]
        marker = self.work / "consumer-imported"
        self.package(a, dependencies=[dependency(b)], source=self.marker_source(marker) + "def register(ctx): pass\n")
        self.package(b, dependencies=[dependency(c)])
        self.package(c, dependencies=[dependency(a)])
        self.package(missing, dependencies=[dependency("org.test.no-such-plugin")])
        self.package(independent)
        result = self.load(missing, independent, a)
        self.assertEqual(result.order, ((independent, VERSION),))
        self.assertFalse(marker.exists())
        self.assertIn("org.test.no-such-plugin", result.failures[(missing, VERSION)])
        self.assertRegex(result.failures[(a, VERSION)], f"{a}.*{b}.*{c}.*{a}")
        self.assertEqual(self.load(independent).failures, {})

    def test_entire_dependency_closure_checked_before_import(self):
        root, first, missing = "org.test.closure.root", "org.test.closure.a", "org.test.closure.z"
        marker = self.work / "first-imported"
        self.package(first, source=self.marker_source(marker) + "def register(ctx): pass\n")
        self.package(root, dependencies=[dependency(first), dependency(missing)])
        incomplete = self.load(root)
        self.assertIn(missing, incomplete.failures[(root, VERSION)])
        self.assertFalse(marker.exists())
        self.package(missing)
        wrong_pin = Pin(first, VERSION, "0" * 64)
        mismatch = bootstrap(self.root, self.catalog, [(root, VERSION), wrong_pin])
        self.assertIn((root, VERSION), mismatch.failures)
        self.assertFalse(marker.exists())
        self.assertEqual(self.load(root).failures, {})
        self.assertTrue(marker.is_file())

    def test_declaration_and_registration_agree(self):
        self.package("org.test.no-contribution", extensions=[TOOL])
        source = "def register(ctx): ctx.tools.register(name='hello_research', schema={'type':'object'}, handler=str)\n"
        self.package("org.test.undeclared", source=source)
        self.package("org.test.duplicate", extensions=[TOOL], source=(
            "def register(ctx):\n" +
            "    ctx.tools.register(name='hello_research', schema={'type':'object'}, handler=str)\n" * 2))
        for name in ("org.test.no-contribution", "org.test.undeclared", "org.test.duplicate"):
            with self.subTest(name=name):
                result = self.load(name)
                self.assertIn((name, VERSION), result.failures)
                self.assertEqual(result.order, ())

    def test_v21_catalog_without_plugin_metadata(self):
        legacy = Path(__file__).resolve().parents[1] / "config/workflow_packages.json"
        original = json.loads(legacy.read_bytes())
        original.pop("plugins", None)
        self.catalog.write_text(json.dumps(original))
        before = self.catalog.read_bytes()
        result = self.load()
        self.assertEqual(result.order, ())
        self.assertEqual(result.failures, {})
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertFalse(self.root.exists())
