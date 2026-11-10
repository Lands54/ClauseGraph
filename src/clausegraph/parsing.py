"""Convert structurally marked Markdown and DOCX into the same tree."""

import hashlib
import io
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from markdown_it import MarkdownIt

from .contracts import Block, Clause, ClauseTree, Location, fingerprint

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PARSER_VERSION = "1.0"


def _markdown(data):
    source = data.decode("utf-8-sig")
    lines = source.splitlines()
    tokens = MarkdownIt("commonmark").enable("table").parse(source)
    for index, token in enumerate(tokens):
        if token.map is None:
            continue
        loc = Location(unit="markdown_line", start=token.map[0] + 1, end=token.map[1])
        if token.type == "heading_open" and token.level == 0:
            yield int(token.tag[1:]), tokens[index + 1].content, loc
        elif token.type == "table_open" and token.level == 0:
            rows = []
            for cell in tokens[index + 1:]:
                if cell.type == "table_close":
                    break
                if cell.type == "tr_open":
                    rows.append([])
                if cell.type == "inline":
                    rows[-1].append(cell.content)
            yield Block(kind="table", location=loc, rows=rows)
        elif token.level == 0:
            # Preserve outer source blocks once, including lists and blockquotes.
            if token.type in {"fence", "code_block"}:
                content = token.content.rstrip("\n")
            else:
                content = "\n".join(lines[token.map[0]:token.map[1]])
            if content.strip():
                yield Block(kind="paragraph", location=loc, text=content)


def _word_text(element):
    text = {W + "tab": "\t", W + "br": "\n", W + "cr": "\n"}
    return "".join(
        item.text or "" if item.tag == W + "t" else text.get(item.tag, "")
        for item in element.iter()
    )


def _outline(element):
    node = element.find(f"{W}pPr/{W}outlineLvl")
    if node is None:
        return None
    value = int(node.attrib[W + "val"])
    if not 0 <= value <= 9:
        raise ValueError("DOCX 大纲级别必须在 0 至 9 之间")
    return value + 1 if value < 9 else 0


def _heading_level(paragraph, styles, default_style):
    level = _outline(paragraph)
    if level is not None:
        return level
    ref = paragraph.find(f"{W}pPr/{W}pStyle")
    style_id = ref.get(W + "val") if ref is not None else default_style
    seen = set()
    while style_id:
        if style_id in seen:
            raise ValueError("DOCX 标题样式继承成环")
        seen.add(style_id)
        style = styles.get(style_id)
        if style is not None:
            level = _outline(style)
            if level is not None:
                return level
        name = style.find(W + "name") if style is not None else None
        for label in (style_id, name.get(W + "val", "") if name is not None else ""):
            heading = re.fullmatch(r"(?:Heading|标题)\s*([1-9])", label, re.IGNORECASE)
            if heading:
                return int(heading[1])
        base = style.find(W + "basedOn") if style is not None else None
        style_id = base.get(W + "val") if base is not None else None
    return 0


def _docx(data):
    with ZipFile(io.BytesIO(data)) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        styles = {}
        default_style = None
        if "word/styles.xml" in archive.namelist():
            style_doc = ET.fromstring(archive.read("word/styles.xml"))
            for style in style_doc.findall(W + "style"):
                if style.get(W + "type") == "paragraph":
                    style_id = style.get(W + "styleId")
                    styles[style_id] = style
                    if style.get(W + "default") in {"1", "true", "on"}:
                        default_style = style_id
    body = document.find(W + "body")
    if body is None:
        raise ValueError("DOCX 缺少文档主体")
    for position, element in enumerate(body, 1):
        loc = Location(unit="docx_block", start=position, end=position)
        if element.tag == W + "p":
            text = _word_text(element)
            if text.strip():
                level = _heading_level(element, styles, default_style)
                yield (level, text, loc) if level else Block(kind="paragraph", location=loc, text=text)
        elif element.tag == W + "tbl":
            rows = [
                ["\n".join(_word_text(p) for p in cell.findall(W + "p")) for cell in row.findall(W + "tc")]
                for row in element.findall(W + "tr")
            ]
            if rows:
                yield Block(kind="table", location=loc, rows=rows)
        elif element.tag != W + "sectPr":
            if _word_text(element).strip() or element.find(".//" + W + "tbl") is not None:
                raise ValueError(f"DOCX 主体块 {position} 使用了暂不支持的内容容器")


def parse_document(path, document_id, document_type=None):
    """Parse original bytes or import a JSON ClauseTree."""
    path = Path(path)
    try:
        data = path.read_bytes()
        suffix = path.suffix.lower()
        if suffix == ".json":
            tree = ClauseTree.model_validate_json(data)
            if tree.document_id != document_id or (document_type is not None and tree.document_type != document_type):
                raise ValueError("导入条款树的文档 ID 或类型与清单不一致")
            return tree
        parser = {".docx": _docx, ".md": _markdown, ".markdown": _markdown}.get(suffix)
        if parser is None:
            raise ValueError(f"不支持的格式 {suffix}")
        version = hashlib.sha256(data).hexdigest()

        def make_clause(order, title, level, location=None):
            return Clause(clause_id=fingerprint([document_id, version, PARSER_VERSION, order]),
                          order=order, title=title, level=level, location=location)

        root = make_clause(0, "", 0)
        stack = [root]
        diagnostics = []
        order = 0
        for item in parser(data):
            if isinstance(item, Block):
                stack[-1].blocks.append(item)
                continue
            level, title, loc = item
            while stack[-1].level >= level:
                stack.pop()
            if level > stack[-1].level + 1:
                diagnostics.append(f"{loc.unit} {loc.start}：标题跳级至 {level}")
            order += 1
            node = make_clause(order, title, level, loc)
            stack[-1].children.append(node)
            stack.append(node)
        if not root.children:
            raise ValueError("文档没有可识别的标题")
        return ClauseTree(document_id=document_id, source_sha256=version, parser_version=PARSER_VERSION,
                          document_type=document_type, root=root, diagnostics=diagnostics)
    except (OSError, ValueError, BadZipFile, ET.ParseError, KeyError) as exc:
        raise ValueError(f"{path}：{exc}") from exc
