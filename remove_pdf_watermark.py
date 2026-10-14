#!/usr/bin/env python3
"""Remove separable PDF watermark objects without rasterizing page content."""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, ContentStream, DecodedStreamObject, NameObject

IDENTITY = (1., 0., 0., 1., 0., 0.)
PAINT = {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}


def concat(m, c):
    a, b, d, e, x, y = map(float, m)
    A, B, D, E, X, Y = c
    return (a*A+b*D, a*B+b*E, d*A+e*D, d*B+e*E, x*A+y*D+X, x*B+y*E+Y)


def resolved(value):
    return value.get_object() if value is not None else None


def resource_table(resources, key):
    return resolved(resources.get(key)) or {}


def fingerprint(obj):
    """Digest decoded content plus interpretation; never identify by object number."""
    h = hashlib.sha256()
    for key in ("/Subtype", "/Width", "/Height", "/BitsPerComponent", "/ColorSpace",
                "/Decode", "/ImageMask", "/BBox", "/Matrix"):
        h.update(f"{key}={resolved(obj.get(key))!r}\n".encode())
    h.update(obj.get_data())
    mask = resolved(obj.get("/SMask"))
    if mask is not None:
        for key in ("/Width", "/Height", "/BitsPerComponent", "/ColorSpace", "/Decode"):
            h.update(f"mask:{key}={resolved(mask.get(key))!r}\n".encode())
        h.update(mask.get_data())
    return h.hexdigest()


def alpha_stats(obj):
    """Read only simple 8-bit gray masks; unsupported masks cannot auto-match."""
    mask = resolved(obj.get("/SMask"))
    if not mask or mask.get("/BitsPerComponent") != 8 or mask.get("/ColorSpace") != "/DeviceGray":
        return None
    width, height = int(mask.get("/Width", 0)), int(mask.get("/Height", 0))
    if width*height > 100_000_000 or width*height <= 0:
        return None
    data = mask.get_data()
    if len(data) != width*height or mask.get("/Decode", [0, 1]) != [0, 1]:
        return None
    hist = [0]*256
    for n in data:
        hist[n] += 1
    return {"max_alpha": max(i for i, n in enumerate(hist) if n),
            "nonzero_fraction": round(1-hist[0]/len(data), 6)}


def walk(stream, resources, pdf, ctm=IDENTITY, ancestors=(), path="", depth=0):
    """Inventory invocations, including nested Form XObjects. Clipping is retained."""
    if stream is None or depth > 32:
        return
    ops = ContentStream(stream, pdf).operations
    stack = []
    for args, op in ops:
        if op == b"q":
            stack.append(ctm)
        elif op == b"Q":
            if stack:
                ctm = stack.pop()
        elif op == b"cm":
            ctm = concat(args, ctm)
        elif op == b"Do" and args:
            name = str(args[0])
            obj = resolved(resource_table(resources, "/XObject").get(name))
            if obj is None:
                continue
            current_path = path + name
            yield current_path, name, obj, ctm
            key = id(obj)
            if obj.get("/Subtype") == "/Form" and key not in ancestors:
                yield from walk(obj, resolved(obj.get("/Resources")) or resources, pdf,
                                concat(obj.get("/Matrix", IDENTITY), ctm),
                                ancestors+(key,), current_path, depth+1)


def pattern_names(resources):
    return list(resource_table(resources, "/Pattern").keys())


def inventory(reader, pages):
    items, by_hash = [], defaultdict(list)
    cache = {}
    for number in pages:
        page = reader.pages[number-1]
        resources = resolved(page.get("/Resources")) or {}
        box = page.cropbox
        area = float(box.width*box.height)
        for path, name, obj, ctm in walk(page.get_contents(), resources, reader):
            key = id(obj)
            if key not in cache:
                cache[key] = (fingerprint(obj), alpha_stats(obj) if obj.get("/Subtype") == "/Image" else None)
            digest, alpha = cache[key]
            a, b, c, d, _, _ = ctm
            coverage = abs(a*d-b*c)/area if area and obj.get("/Subtype") == "/Image" else None
            item = {"page": number, "path": path, "name": name,
                    "type": str(obj.get("/Subtype")), "sha256": digest,
                    "coverage": round(coverage, 4) if coverage is not None else None,
                    "alpha": alpha, "auto_candidate": False}
            if obj.get("/Subtype") == "/Image":
                item["size"] = [int(obj.get("/Width", 0)), int(obj.get("/Height", 0))]
            items.append(item)
            by_hash[digest].append(item)
        # Pattern resource names are local to their owner, including nested Forms.
        containers = [("", resources)]
        containers += [(path, resolved(obj.get("/Resources")) or resources)
                       for path, _, obj, _ in walk(page.get_contents(), resources, reader)
                       if obj.get("/Subtype") == "/Form"]
        for prefix, res in containers:
            for name in pattern_names(res):
                items.append({"page": number, "path": prefix+str(name), "name": str(name),
                              "type": "/Pattern", "auto_candidate": False})
        for ref in page.get("/Annots", []):
            annot = resolved(ref)
            if annot.get("/Subtype") == "/Watermark":
                items.append({"page": number, "type": "/Watermark", "auto_candidate": False})
    for group in by_hash.values():
        repeated_pages = len({item["page"] for item in group})
        for item in group:
            item["repeated_pages"] = repeated_pages
            alpha = item["alpha"]
            # This is a structural heuristic, not recognition of watermark wording.
            item["auto_candidate"] = bool(
                item["type"] == "/Image" and item["coverage"] >= .8 and alpha
                and 0 < alpha["max_alpha"] <= 80
                and .005 <= alpha["nonzero_fraction"] <= .25
                and (repeated_pages >= 2 or len(reader.pages) == 1))
    return items


def is_pattern_space(name, resources):
    if str(name) == "/Pattern":
        return True
    space = resolved(resource_table(resources, "/ColorSpace").get(name))
    return isinstance(space, (ArrayObject, list)) and len(space) > 0 and space[0] == "/Pattern"


def rewrite(stream, resources, pdf, selected, patterns, ancestors=(), depth=0):
    """Return a rewritten stream and removed paint count; retain graphics state."""
    if stream is None:
        return None, 0
    if depth > 32:
        raise ValueError("Form nesting exceeds 32 levels; refusing partial output")
    content = ContentStream(stream, pdf)
    result, removed, stack, replaced_names = [], 0, [], set()
    fill_space, fill_pattern = "/DeviceGray", None
    for args, op in content.operations:
        if op == b"q":
            stack.append((fill_space, fill_pattern))
        elif op == b"Q":
            if stack:
                fill_space, fill_pattern = stack.pop()
        elif op == b"cs":
            fill_space, fill_pattern = args[0], None
        elif op in (b"rg", b"g", b"k"):
            fill_space, fill_pattern = "/DeviceGray", None
        elif op in (b"sc", b"scn"):
            fill_pattern = next((str(v) for v in args if isinstance(v, NameObject)), None)
        if op == b"Do" and args:
            name = str(args[0])
            obj = resolved(resource_table(resources, "/XObject").get(name))
            if obj is not None:
                if selected(name, obj):
                    removed += 1
                    continue
                key = id(obj)
                if obj.get("/Subtype") == "/Form" and key not in ancestors:
                    child_resources = copy(resolved(obj.get("/Resources")) or resources)
                    child, count = rewrite(obj, child_resources,
                                           pdf, selected, patterns, ancestors+(key,), depth+1)
                    if count:
                        # Do not mutate a shared Form: it may be used on an unselected page.
                        new_obj = DecodedStreamObject()
                        for k, v in obj.items():
                            if k not in ("/Length", "/Filter", "/DecodeParms"):
                                new_obj[k] = v
                        new_obj[NameObject("/Resources")] = child_resources
                        new_obj.set_data(child.get_data())
                        xobjects = copy(resolved(resources.get("/XObject")))
                        new_name = NameObject(f"/WMForm{len(xobjects)}")
                        while new_name in xobjects:
                            new_name = NameObject(str(new_name)+"x")
                        xobjects[new_name] = pdf._add_object(new_obj)
                        resources[NameObject("/XObject")] = xobjects
                        args = [new_name]
                        replaced_names.add(name)
                        removed += count
        elif op in PAINT and fill_pattern in patterns and is_pattern_space(fill_space, resources):
            # Finish the path; preserve an accompanying stroke and pending clip.
            op = b"S" if op in (b"B", b"B*") else b"s" if op in (b"b", b"b*") else b"n"
            removed += 1
        result.append((args, op))
    content.operations = result
    if removed:
        xobjects = copy(resolved(resources.get("/XObject")) or {})
        used = {str(a[0]) for a, op in result if op == b"Do" and a}
        # A Form without /Resources can inherit these names; keep its dependencies.
        inherited = any(resolved(ref).get("/Subtype") == "/Form"
                        and not resolved(ref).get("/Resources")
                        for name, ref in xobjects.items() if str(name) in used)
        for name, ref in list(xobjects.items()):
            if selected(str(name), resolved(ref)) or (str(name) in replaced_names and str(name) not in used and not inherited):
                xobjects.pop(name)
        if xobjects:
            resources[NameObject("/XObject")] = xobjects
        elif "/XObject" in resources:
            del resources["/XObject"]
    replacement = DecodedStreamObject()
    replacement.set_data(content.get_data())
    return replacement, removed


def page_selection(spec, total):
    if not spec:
        return list(range(1, total+1))
    pages = set()
    for part in spec.split(","):
        bounds = part.split("-")
        if len(bounds) == 1:
            pages.add(int(bounds[0]))
        elif len(bounds) == 2:
            start, end = map(int, bounds)
            if end < start:
                raise ValueError("Page range must be ascending")
            pages.update(range(start, end+1))
        else:
            raise ValueError("Invalid page range")
    if not pages or min(pages) < 1 or max(pages) > total:
        raise ValueError(f"Pages must be within 1..{total}")
    return sorted(pages)


def atomic_write(writer, path, force):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".watermark-", suffix=".pdf", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            writer.write(handle)
        check = PdfReader(temporary)
        if len(check.pages) != len(writer.pages):
            raise ValueError("Page count changed during writing")
        if force:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # Atomic no-clobber, including concurrent writers.
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def process(source, destination, args):
    reader = PdfReader(source)
    if reader.is_encrypted and not reader.decrypt(os.environ.get("PDF_PASSWORD", "")):
        raise ValueError("Encrypted PDF: set PDF_PASSWORD to a valid password")
    pages = page_selection(args.pages, len(reader.pages))
    items = [item for item in inventory(reader, range(1, len(reader.pages)+1)) if item["page"] in pages]
    auto = args.auto or not (args.xobject or args.sha256 or args.pattern or args.watermark_annots)
    hashes = {item["sha256"] for item in items if auto and item.get("auto_candidate")}
    explicit_names = {"/"+name.lstrip("/") for name in args.xobject}
    patterns = {"/"+name.lstrip("/") for name in args.pattern}
    for item in items:
        item["selected"] = bool(
            item.get("sha256") in hashes or (item["type"] in ("/Image", "/Form") and item.get("name") in explicit_names)
            or any(item.get("sha256", "!").startswith(d) for d in args.sha256)
            or (item["type"] == "/Pattern" and item["name"] in patterns)
            or (item["type"] == "/Watermark" and args.watermark_annots))
    report = {"input": str(source), "output": str(destination), "pages": pages,
              "automatic": auto, "objects": items, "removed_paints": 0, "written": False}
    if args.inspect or args.dry_run:
        return report
    if destination.resolve() == source.resolve() or (destination.exists() and os.path.samefile(source, destination)):
        raise ValueError("Output must not be the input file")
    if destination.exists() and not args.force:
        raise FileExistsError(f"Output exists: {destination}; use --force to replace it")
    def selected(name, obj):
        return (name in explicit_names or fingerprint(obj) in hashes
                or any(fingerprint(obj).startswith(d) for d in args.sha256))
    writer = PdfWriter(clone_from=reader)
    for number in pages:
        page = writer.pages[number-1]
        resources = copy(resolved(page.get("/Resources")) or {})
        replacement, count = rewrite(page.get_contents(), resources, writer, selected, patterns)
        if count:
            page[NameObject("/Resources")] = resources
            page.replace_contents(replacement)
        if args.watermark_annots:
            annots = page.get("/Annots", [])
            kept = [ref for ref in annots if resolved(ref).get("/Subtype") != "/Watermark"]
            count += len(annots)-len(kept)
            if len(kept) != len(annots):
                page[NameObject("/Annots")] = ArrayObject(kept)
        report["removed_paints"] += count
    if not report["removed_paints"]:
        return report
    # Drop orphan objects after pruning selected XObject resource references.
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    atomic_write(writer, destination, args.force)
    report["written"] = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="删除可分离的 PDF 水印对象，保留原始正文；先用 --inspect 检查。")
    parser.add_argument("inputs", nargs="+", type=Path, help="一个或多个输入 PDF")
    parser.add_argument("-o", "--output", type=Path, help="单文件输出路径")
    parser.add_argument("--output-dir", type=Path, help="批量输出目录")
    parser.add_argument("--inspect", action="store_true", help="列出对象与自动候选，不写 PDF")
    parser.add_argument("--dry-run", action="store_true", help="列出将删除的对象，不写 PDF")
    parser.add_argument("--auto", action="store_true", help="筛选大面积、低透明度、稀疏的重复图片层；无显式选择时默认开启")
    parser.add_argument("--xobject", action="append", default=[], help="删除指定图片/Form资源名，可重复，如 X136")
    parser.add_argument("--sha256", action="append", default=[], help="按对象指纹删除；至少12位十六进制前缀，可重复")
    parser.add_argument("--pattern", action="append", default=[], help="删除指定 Pattern 的填充绘制，可重复，如 P1")
    parser.add_argument("--watermark-annots", action="store_true", help="删除 /Watermark 注释")
    parser.add_argument("--pages", help="只处理选定页，如 1,3-5；页码从1开始")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有输出，仍禁止覆盖输入")
    parser.add_argument("--report", type=Path, help="另外保存 JSON 分析报告")
    args = parser.parse_args(argv)
    if args.output and (len(args.inputs) != 1 or args.output_dir):
        parser.error("-o 仅支持单输入，且不能与 --output-dir 同时使用")
    args.sha256 = [d.lower() for d in args.sha256]
    if any(len(d) < 12 or len(d) > 64 or any(c not in "0123456789abcdef" for c in d) for d in args.sha256):
        parser.error("--sha256 需要12至64位十六进制前缀")
    outputs = [args.output or (args.output_dir or p.parent)/(p.stem+".clean.pdf") for p in args.inputs]
    if len(set(p.resolve() for p in outputs)) != len(outputs):
        parser.error("多个输入产生同名输出，请分别处理或选择不同文件名")
    protected = {p.resolve() for p in args.inputs} | {p.resolve() for p in outputs}
    if args.report and args.report.resolve() in protected:
        parser.error("报告不能覆盖输入/输出 PDF")
    if args.report and args.report.exists() and not args.force:
        parser.error("报告已存在；换一个路径，或用 --force 覆盖")
    reports, status = [], 0
    for source, destination in zip(args.inputs, outputs):
        try:
            if destination.resolve() in {p.resolve() for p in args.inputs}:
                raise ValueError("Output conflicts with an input file")
            report = process(source, destination, args)
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not (args.inspect or args.dry_run) and not report["written"]:
                print(f"没有匹配的可删除水印，未生成文件：{source}", file=sys.stderr)
                status = max(status, 1)
        except Exception as exc:
            print(f"处理失败 {source}: {exc}", file=sys.stderr)
            reports.append({"input": str(source), "error": str(exc)})
            status = 2
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("w" if args.force else "x", encoding="utf-8") as handle:
            json.dump(reports, handle, ensure_ascii=False, indent=2)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
