"""papers 路由测试（Phase G G2 起建）：GET /api/papers/{id}/citation-graph 端点。

覆盖 spec 3.2 行为契约：
- 200：返回以该文献为中心的 1 跳子图 {nodes, edges}（出边 + 入边），
  nodes 项为 {id, title, year}，edges 项为 {citing, cited}；
- 无边文献：nodes 仅中心文献，edges 为空；
- 404：文献不存在。

paper_citations 表归另一并行代理（G1），本文件按契约在内存 SQLite 中 DDL 造表。
"""

import pytest
from sqlalchemy import text

from app.models import Paper

# paper_citations 契约表结构（与 G1 ensure_schema 迁移分支同构，见 spec 3.1）
_CITATION_DDL = """
CREATE TABLE IF NOT EXISTS paper_citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    citing_id INTEGER NOT NULL,
    cited_id INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (citing_id, cited_id)
)
"""


@pytest.fixture()
def citation_table(db):
    """按契约在内存库造 paper_citations 表（用例间隔离：先 DROP 再 CREATE）。"""
    db.execute(text("DROP TABLE IF EXISTS paper_citations"))
    db.execute(text(_CITATION_DDL))
    db.commit()
    yield db
    db.execute(text("DROP TABLE IF EXISTS paper_citations"))
    db.commit()


def _make_paper(db, title, year=2024):
    paper = Paper(
        title=title,
        authors="测试作者",
        year=year,
        file_path=f"papers/{title}.pdf",
        filename=f"{title}.pdf",
    )
    db.add(paper)
    db.flush()
    return paper


def _add_edge(db, citing_id, cited_id):
    db.execute(
        text("INSERT INTO paper_citations (citing_id, cited_id) VALUES (:a, :b)"),
        {"a": citing_id, "b": cited_id},
    )
    db.commit()


class TestCitationGraphEndpoint:
    """GET /api/papers/{id}/citation-graph：1 跳子图结构契约。"""

    def test_200_structure_out_and_in_edges(self, client, citation_table, db):
        """出边（中心引用别人）与入边（别人引用中心）都进子图。"""
        center = _make_paper(db, "中心文献", 2023)
        cited = _make_paper(db, "被引文献", 2020)
        citing = _make_paper(db, "施引文献", 2025)
        unrelated = _make_paper(db, "无关文献", 2021)
        _add_edge(db, center.id, cited.id)  # 出边
        _add_edge(db, citing.id, center.id)  # 入边
        _add_edge(db, citing.id, unrelated.id)  # 与中心无关的边不进子图

        resp = client.get(f"/api/papers/{center.id}/citation-graph")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"nodes", "edges"}

        nodes = {n["id"]: n for n in body["nodes"]}
        assert set(nodes.keys()) == {center.id, cited.id, citing.id}
        assert nodes[center.id]["title"] == "中心文献"
        assert nodes[center.id]["year"] == 2023
        assert nodes[cited.id]["title"] == "被引文献"
        assert nodes[citing.id]["title"] == "施引文献"

        edges = {(e["citing"], e["cited"]) for e in body["edges"]}
        assert edges == {(center.id, cited.id), (citing.id, center.id)}

    def test_node_and_edge_field_shape(self, client, citation_table, db):
        """nodes 项恰为 {id, title, year}，edges 项恰为 {citing, cited}。"""
        center = _make_paper(db, "中心文献")
        other = _make_paper(db, "被引文献")
        _add_edge(db, center.id, other.id)

        resp = client.get(f"/api/papers/{center.id}/citation-graph")
        assert resp.status_code == 200
        body = resp.json()
        assert all(set(n.keys()) == {"id", "title", "year"} for n in body["nodes"])
        assert all(set(e.keys()) == {"citing", "cited"} for e in body["edges"])

    def test_no_edges_returns_center_only(self, client, citation_table, db):
        """无边文献：nodes 仅中心文献本身，edges 为空列表。"""
        loner = _make_paper(db, "孤立文献", 2022)
        resp = client.get(f"/api/papers/{loner.id}/citation-graph")
        assert resp.status_code == 200
        body = resp.json()
        assert body["nodes"] == [{"id": loner.id, "title": "孤立文献", "year": 2022}]
        assert body["edges"] == []

    def test_404_when_paper_not_found(self, client, citation_table, db):
        resp = client.get("/api/papers/99999/citation-graph")
        assert resp.status_code == 404
