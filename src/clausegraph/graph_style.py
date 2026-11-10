"""Shared document labels and Neo4j Browser styles."""

import colorsys
import hashlib
import re


def document_label(document_id):
    slug = re.sub(r"[^A-Za-z0-9]+", "_", document_id).strip("_")[:24] or "file"
    digest = hashlib.sha256(document_id.encode()).hexdigest()[:12]
    return f"Doc_{slug}_{digest}"


def browser_style(document_labels):
    rules = []

    def add(label, color, size):
        rules.append(f'node.{label} {{ color: {color}; diameter: {size}px; caption: "{{label}}"; }}')

    for index, label in enumerate(sorted(set(document_labels))):
        if not re.fullmatch(r"Doc_[A-Za-z0-9_]+_[0-9a-f]{12}", label):
            raise ValueError("无效的文档标签")
        hue = (0.60 + index * 0.46) % 1 if index < 2 else (0.60 + index * 0.618034) % 1
        color = "#" + "".join(f"{round(channel * 255):02x}" for channel in colorsys.hls_to_rgb(hue, 0.49, 0.65))
        add(label, color, 42)

    add("TypeConcept", "#7654af", 50)
    add("ContentConcept", "#228574", 46)
    add("Clause", "#667a91", 42)
    add("Document", "#42566e", 52)
    add("ClauseGraphNode", "#8795a6", 40)
    return "\n".join(rules) + "\n"
