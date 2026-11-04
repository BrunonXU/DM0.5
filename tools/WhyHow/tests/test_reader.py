import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("whyhow_server", Path(__file__).parents[1] / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.article = self.docs / "lesson.md"
        self.article.write_text("# 教学标题\n\n## 动作窗口\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
                                "```mermaid\nflowchart LR\nA-->B\n```\n\n- [x] 已核验\n")
        catalog = self.root / "catalog.json"
        catalog.write_text(json.dumps({"items": [{"id": "lesson", "kind": "markdown",
                           "title": "教学", "description": "说明", "category": "数据",
                           "source": str(self.article)}]}))
        self.project_patch = patch.object(server, "PROJECTS", self.root)
        self.roots_patch = patch.object(server, "DOCUMENT_ROOTS", [self.docs])
        self.project_patch.start()
        self.roots_patch.start()
        self.lib = server.Library(catalog)

    def tearDown(self):
        self.roots_patch.stop()
        self.project_patch.stop()
        self.temp.cleanup()

    def test_markdown_table_mermaid_toc_and_task_list(self):
        doc = self.lib.document(ident="lesson")
        self.assertIn("<table>", doc["html"])
        self.assertIn('class="language-mermaid"', doc["html"])
        self.assertIn('checked="checked"', doc["html"])
        self.assertEqual(doc["toc"][1]["id"], "动作窗口")

    def test_interactive_theme_preserves_original_data_and_script(self):
        lesson = self.docs / "lesson.html"
        original = '<html><head><style>body{color:blue}</style></head><body><script>const frames=[100,101];</script></body></html>'
        lesson.write_text(original)
        served = server.themed_interactive(lesson).decode()
        self.assertIn('/web/interactive.css', served)
        self.assertIn('<script>const frames=[100,101];</script>', served)
        self.assertGreater(served.index('/web/interactive.css'), served.index('</style>'))
        self.assertEqual(lesson.read_text(), original)

    def test_live_source_is_read_again(self):
        self.lib.document(ident="lesson")
        self.article.write_text("# 已更新\n")
        self.assertIn("已更新", self.lib.document(ident="lesson")["html"])

    def test_document_path_cannot_escape_roots(self):
        outside = self.root / "private.md"
        outside.write_text("private")
        for path in ["private.md", "docs/../private.md", "/etc/passwd", "docs/missing.md"]:
            with self.assertRaises(FileNotFoundError):
                self.lib.document(relative=path)

    def test_symlink_cannot_escape_roots(self):
        outside = self.root / "secret.md"
        outside.write_text("secret")
        (self.docs / "linked.md").symlink_to(outside)
        with self.assertRaises(FileNotFoundError):
            self.lib.document(relative="docs/linked.md")

    def test_active_html_and_event_attributes_removed(self):
        self.article.write_text('# Safe\n\n<script>alert("unsafe")</script>\n\n'
                                '<img src="javascript:alert(1)" onerror="alert(2)">'
                                '<a href="javascript:alert(3)">label</a>\n')
        rendered = self.lib.document(ident="lesson")["html"]
        for forbidden in ["<script", "javascript:", "onerror", "unsafe"]:
            self.assertNotIn(forbidden, rendered)
        self.assertIn("label", rendered)

    def test_local_document_and_attachment_links(self):
        sibling = self.docs / "other.md"
        sibling.write_text("# Other")
        photo = self.docs / "plot.png"
        photo.write_bytes(b"sample")
        self.article.write_text("# Read\n\n[other](other.md#段落)\n\n![plot](plot.png)")
        rendered = self.lib.document(ident="lesson")["html"]
        self.assertIn("/?path=docs%2Fother.md", rendered)
        self.assertIn("/resource/", rendered)
        self.assertEqual(list(self.lib.resources.values()), [photo])

    def test_external_links_isolated_and_external_images_omitted(self):
        self.article.write_text('# Read\n\n[docs](https://example.com/docs)\n\n'
                                '![tracker](https://example.com/pixel.png)')
        rendered = self.lib.document(ident="lesson")["html"]
        self.assertIn('rel="noopener noreferrer"', rendered)
        self.assertNotIn("pixel.png", rendered)

    def test_external_teaching_source_keeps_reader_urls_and_restricts_access(self):
        with tempfile.TemporaryDirectory() as external:
            source = Path(external)
            content = source / "content"
            content.mkdir()
            article = content / "lesson.md"
            article.write_text("# External lesson\n\n[Next](next.md)")
            (content / "next.md").write_text("# Next")
            (source / "private.md").write_text("private")
            (content / "escape.md").symlink_to(source / "private.md")
            catalog = source / "catalog.json"
            catalog.write_text(json.dumps({"items": [{"id": "external", "kind": "markdown",
                               "title": "External", "description": "Lesson", "category": "Teaching",
                               "source": str(article)}]}))
            with patch.object(server, "WHYHOW_SOURCE", source), \
                    patch.object(server, "DOCUMENT_ROOTS", [self.docs, content]):
                library = server.Library(catalog)
                label = "WhyHow/content/lesson.md"
                self.assertEqual(library.catalog()["items"][0]["source_label"], label)
                document = library.document(relative=label)
                self.assertEqual(document["source_label"], label)
                self.assertIn("/?path=WhyHow%2Fcontent%2Fnext.md", document["html"])
                for path in ["WhyHow/private.md", "WhyHow/content/../private.md", "WhyHow/content/escape.md"]:
                    with self.assertRaises(FileNotFoundError):
                        library.document(relative=path)


if __name__ == "__main__":
    unittest.main()
