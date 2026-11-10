"""Local browser interface for the project file library."""

import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .graph_style import browser_style
from .projects import create_snapshot, import_document, init_project, list_project_files
from .publishing import neo4j_driver

WEB = Path(__file__).with_name("web")
UPLOAD_LIMIT = 32 * 1024 * 1024


class SnapshotInput(BaseModel):
    version_ids: list[str] = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, strict=True)


def _stage(upload, directory, allowed):
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(422, "不支持的文件格式")
    upload.file.seek(0, 2)
    if upload.file.tell() > UPLOAD_LIMIT:
        raise HTTPException(413, "单个文件不能超过 32 MiB")
    upload.file.seek(0)
    path = Path(directory) / f"upload{suffix}"
    with path.open("wb") as target:
        shutil.copyfileobj(upload.file, target)
    return path


def create_app():
    app = FastAPI(title="条款图项目工作台")
    connection = {"neo4j_uri": os.getenv("CLAUSEGRAPH_NEO4J_URI", "bolt://127.0.0.1:7688"),
                  "database": os.getenv("CLAUSEGRAPH_NEO4J_DATABASE", "neo4j")}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/projects")
    def projects():
        with neo4j_driver(connection["neo4j_uri"]) as driver, driver.session(database=connection["database"]) as session:
            return [row["id"] for row in session.run(
                "MATCH (p:ClauseGraphProject) RETURN p.id AS id ORDER BY id"
            )]

    @app.post("/api/projects")
    def new_project(project: Annotated[str, Form()], ontology: Annotated[UploadFile, File()],
                    route3: Annotated[bool, Form()] = False):
        try:
            with TemporaryDirectory() as directory:
                return init_project(project.strip(), _stage(ontology, directory, {".ttl"}),
                                    enable_route3=route3, **connection)
        except (ValueError, SyntaxError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/projects/{project:path}/files")
    def files(project: str, history: bool = False):
        try:
            return list_project_files(project, history=history, **connection)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/projects/{project:path}/files")
    async def add_file(project: str, document: Annotated[UploadFile, File()],
                       document_id: Annotated[str, Form()], document_type: Annotated[str, Form()] = "",
                       replace: Annotated[bool, Form()] = False, route3: Annotated[bool | None, Form()] = None):
        if not document_id.strip():
            raise HTTPException(422, "请输入文档 ID")
        try:
            with TemporaryDirectory() as directory:
                path = _stage(document, directory, {".docx", ".md", ".markdown", ".json"})
                return await import_document(project, path, document_id.strip(),
                                             document_type=document_type.strip() or None,
                                             replace=replace, enable_route3=route3, **connection)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/projects/{project:path}/snapshots")
    def snapshots(project: str):
        with neo4j_driver(connection["neo4j_uri"]) as driver, driver.session(database=connection["database"]) as session:
            rows = session.run("""
                MATCH (:ClauseGraphProject {id:$project})-[:HAS_SNAPSHOT]->(s:ClauseGraphSnapshot)
                OPTIONAL MATCH (s)-[:USES_DOCUMENT]->(d:ClauseGraphDocumentVersion)
                WITH s.id AS id, s.created_at AS created, collect(d.document_id) AS documents
                ORDER BY created DESC, id LIMIT 50
                RETURN id, toString(created) AS created_at, documents
            """, project=project)
            return [row.data() for row in rows]

    @app.get("/api/snapshots/{snapshot_id}/style.grass", response_class=PlainTextResponse)
    def snapshot_style(snapshot_id: str):
        with neo4j_driver(connection["neo4j_uri"]) as driver, driver.session(database=connection["database"]) as session:
            record = session.run("""
                MATCH (s:ClauseGraphSnapshot {id:$id})
                OPTIONAL MATCH (s)-[:HAS_NODE]->(d:Document)
                RETURN s.id AS id, collect(labels(d)) AS label_sets
            """, id=snapshot_id).single()
        if record is None:
            raise HTTPException(404, "快照不存在")
        labels = [label for group in record["label_sets"] for label in group if label.startswith("Doc_")]
        return PlainTextResponse(browser_style(labels), headers={
            "Content-Disposition": f'attachment; filename="snapshot-{record["id"][:16]}.grass"'
        })

    @app.post("/api/projects/{project:path}/snapshots")
    def snapshot(project: str, request: SnapshotInput):
        try:
            return create_snapshot(project, version_ids=request.version_ids, top_k=request.top_k, **connection)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
