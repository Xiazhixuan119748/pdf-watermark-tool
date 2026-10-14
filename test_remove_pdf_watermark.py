"""Regression tests for independent watermarks, shared Forms and Pattern state."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, ContentStream, DecodedStreamObject,
                           DictionaryObject, NameObject, NumberObject)
import remove_pdf_watermark as tool


def stream(data, **entries):
    obj = DecodedStreamObject()
    obj.set_data(data)
    for key, value in entries.items():
        obj[NameObject('/'+key)] = value
    return obj


def image(writer, transparent=False):
    obj = stream(b'\x90'*300, Type=NameObject('/XObject'), Subtype=NameObject('/Image'),
                 Width=NumberObject(10), Height=NumberObject(10),
                 BitsPerComponent=NumberObject(8), ColorSpace=NameObject('/DeviceRGB'))
    if transparent:
        mask = stream(b'\x2e'*10+b'\0'*90, Type=NameObject('/XObject'),
                      Subtype=NameObject('/Image'), Width=NumberObject(10), Height=NumberObject(10),
                      BitsPerComponent=NumberObject(8), ColorSpace=NameObject('/DeviceGray'))
        obj[NameObject('/SMask')] = writer._add_object(mask)
    return writer._add_object(obj)


def fixture(path, nested=False, transparent=True):
    writer = PdfWriter()
    watermark = image(writer, transparent)
    photo = image(writer)
    resources = DictionaryObject({NameObject('/XObject'): DictionaryObject({
        NameObject('/WM'): watermark, NameObject('/Photo'): photo})})
    body = b'q 200 0 0 300 0 0 cm /WM Do Q q 20 0 0 20 5 5 cm /Photo Do Q'
    if nested:
        form = stream(body, Type=NameObject('/XObject'), Subtype=NameObject('/Form'),
                      BBox=ArrayObject([NumberObject(v) for v in (0, 0, 200, 300)]),
                      Resources=resources)
        resources = DictionaryObject({NameObject('/XObject'): DictionaryObject({
            NameObject('/Shared'): writer._add_object(form)})})
        body = b'q /Shared Do Q'
    shared = writer._add_object(resources)
    for _ in range(2):
        page = writer.add_blank_page(200, 300)
        page[NameObject('/Resources')] = shared
        page.replace_contents(stream(body))
    writer.write(path)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root/'input.pdf'
        self.output = self.root/'output.pdf'

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *flags):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return tool.main([str(self.source), '-o', str(self.output), *flags])

    def names(self, reader, page):
        res = tool.resolved(reader.pages[page]['/Resources'])
        return [name for _, name, _, _ in tool.walk(reader.pages[page].get_contents(), res, reader)]

    def test_auto_keeps_photo_and_removes_layer(self):
        fixture(self.source)
        self.assertEqual(self.run_cli(), 0)
        reader = PdfReader(self.output)
        self.assertEqual(len(reader.pages), 2)
        for p in range(2):
            self.assertEqual(self.names(reader, p), ['/Photo'])
            self.assertNotIn('/WM', reader.pages[p]['/Resources']['/XObject'])
        # Removed image and alpha bytes must no longer be stored as orphan streams.
        self.assertEqual(len(reader.pages[0].images), 1)

    def test_page_selection_does_not_modify_shared_resources(self):
        fixture(self.source)
        self.assertEqual(self.run_cli('--pages', '1'), 0)
        reader = PdfReader(self.output)
        self.assertEqual(self.names(reader, 0), ['/Photo'])
        self.assertEqual(self.names(reader, 1), ['/WM', '/Photo'])

    def test_nested_form_shared_across_pages(self):
        fixture(self.source, nested=True)
        self.assertEqual(self.run_cli('--pages', '1'), 0)
        reader = PdfReader(self.output)
        self.assertNotIn('/WM', self.names(reader, 0))
        self.assertIn('/Photo', self.names(reader, 0))
        self.assertIn('/WM', self.names(reader, 1))

    def test_indirect_resource_tables(self):
        fixture(self.source)
        writer = PdfWriter(clone_from=self.source)
        for page in writer.pages:
            res = page['/Resources']
            res[NameObject('/XObject')] = writer._add_object(tool.resolved(res['/XObject']))
        writer.write(self.source)
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.names(PdfReader(self.output), 0), ['/Photo'])

    def test_explicit_form_and_image(self):
        fixture(self.source, nested=True)
        self.assertEqual(self.run_cli('--xobject', 'Shared'), 0)
        reader = PdfReader(self.output)
        self.assertEqual(self.names(reader, 0), [])

    def test_digest_selection(self):
        fixture(self.source)
        reader = PdfReader(self.source)
        digest = next(i['sha256'] for i in tool.inventory(reader, [1, 2]) if i['name'] == '/WM')
        self.assertEqual(self.run_cli('--sha256', digest[:16]), 0)
        self.assertEqual(self.names(PdfReader(self.output), 0), ['/Photo'])

    def test_opaque_scan_has_no_automatic_match(self):
        fixture(self.source, transparent=False)
        self.assertEqual(self.run_cli(), 1)
        self.assertFalse(self.output.exists())

    def test_inspect_and_dry_run_do_not_write(self):
        fixture(self.source)
        for flag in ('--inspect', '--dry-run'):
            self.assertEqual(self.run_cli(flag), 0)
            self.assertFalse(self.output.exists())

    def test_input_and_existing_output_protected(self):
        fixture(self.source)
        original = self.source.read_bytes()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(tool.main([str(self.source), '-o', str(self.source), '--force']), 2)
        self.assertEqual(self.source.read_bytes(), original)
        self.output.write_bytes(b'KEEP')
        self.assertEqual(self.run_cli(), 2)
        self.assertEqual(self.output.read_bytes(), b'KEEP')

    def test_pattern_fill_preserves_stroke_and_graphics_state(self):
        writer = PdfWriter()
        page = writer.add_blank_page(200, 300)
        pattern = stream(b'0.8 g 0 0 10 10 re f', Type=NameObject('/Pattern'),
                         PatternType=NumberObject(1), PaintType=NumberObject(1), TilingType=NumberObject(1),
                         BBox=ArrayObject([NumberObject(v) for v in (0, 0, 10, 10)]),
                         XStep=NumberObject(10), YStep=NumberObject(10), Resources=DictionaryObject())
        res = DictionaryObject({NameObject('/Pattern'): DictionaryObject({NameObject('/P1'): writer._add_object(pattern)})})
        page[NameObject('/Resources')] = res
        page.replace_contents(stream(b'q /Pattern cs /P1 scn 0 0 200 300 re B Q 0 g 10 10 20 20 re f'))
        writer.write(self.source)
        self.assertEqual(self.run_cli('--pattern', 'P1'), 0)
        reader = PdfReader(self.output)
        ops = ContentStream(reader.pages[0].get_contents(), reader).operations
        self.assertIn(([], b'S'), ops)
        self.assertEqual([op for _, op in ops].count(b'f'), 1)
        self.assertIn(([], b'Q'), ops)

    def test_watermark_annotation_does_not_remove_links(self):
        writer = PdfWriter()
        page = writer.add_blank_page(200, 300)
        annots = []
        for kind in ('/Watermark', '/Link'):
            obj = DictionaryObject({NameObject('/Type'): NameObject('/Annot'),
                                    NameObject('/Subtype'): NameObject(kind),
                                    NameObject('/Rect'): ArrayObject([NumberObject(v) for v in (0, 0, 20, 20)])})
            annots.append(writer._add_object(obj))
        page[NameObject('/Annots')] = ArrayObject(annots)
        writer.write(self.source)
        self.assertEqual(self.run_cli('--watermark-annots'), 0)
        reader = PdfReader(self.output)
        self.assertEqual([ref.get_object()['/Subtype'] for ref in reader.pages[0]['/Annots']], ['/Link'])


if __name__ == '__main__':
    unittest.main()
