"""Batch 22E：Parent-Child 评测入口与快照审计 RED。"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Chunk, Paper
from app.services.parent_child import build_parent_map
from eval import run


def _database(path):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Paper(
        id=1, title="fixture", doi="10.1/fixture",
        filename="x.pdf", file_path="papers/x.pdf",
    ))
    db.add(Chunk(
        paper_id=1, chunk_index=0, page_number=1,
        page_start=0, page_end=20, content="parent content",
    ))
    db.commit()
    return engine, db


def test_parent_child_cli_requires_explicit_parent_snapshot_and_full_train(tmp_path):
    parser = run.build_parser()
    common = [
        "--retrieval-profile", "parent-child-v1",
        "--database", str(tmp_path / "child.db"),
        "--corpus-root", str(tmp_path / "corpus"),
        "--vector-dir", str(tmp_path / "vectors"),
        "--split", "train", "--evidence-resolver", "page-span-v2",
    ]
    args = parser.parse_args(common)
    assert "--parent-database" in run._validate_cli_args(args)

    args = parser.parse_args(common + [
        "--parent-database", str(tmp_path / "parent.db"),
        "--qa-id", "private-train-001",
    ])
    assert "--qa-id" in run._validate_cli_args(args)

    args = parser.parse_args(common + [
        "--parent-database", str(tmp_path / "parent.db"),
    ])
    assert run._validate_cli_args(args) is None


def test_parent_database_is_rejected_for_other_profiles(tmp_path):
    args = run.build_parser().parse_args([
        "--parent-database", str(tmp_path / "parent.db"),
        "--vector-dir", str(tmp_path / "vectors"),
    ])
    assert "仅适用于 parent-child-v1" in run._validate_cli_args(args)


def test_parent_child_contract_is_frozen_and_hashed():
    contract = run.parent_child_contract_metadata()
    assert contract["route_limit"] == 40
    assert contract["rrf_k"] == 60
    assert contract["parent_weights"] == [1.0, 0.5, 0.25]
    assert contract["max_scoring_children"] == 3
    assert contract["routing"] == "parent-round-robin"
    assert len(contract["contract_sha256"]) == 64


class _Collection:
    def __init__(self, ids, dimensions=1024):
        self.ids = ids
        self.dimensions = dimensions

    def get(self, include):
        assert include == ["embeddings"]
        return {
            "ids": list(self.ids),
            "embeddings": [[0.0] * self.dimensions for _ in self.ids],
        }


def test_parent_child_snapshot_audit_checks_id_parity_and_dimension(tmp_path):
    parent_engine, parent_db = _database(tmp_path / "parent.db")
    child_engine, child_db = _database(tmp_path / "child.db")
    mapping = build_parent_map(child_db, parent_db)
    store = SimpleNamespace(collection=_Collection(["p1_c0"]))
    try:
        audit = run._audit_parent_child_snapshot(
            child_db, store, parent_db, mapping
        )
        assert audit["database_chunk_count"] == 1
        assert audit["vector_count"] == 1
        assert audit["missing_vector_ids"] == 0
        assert audit["extra_vector_ids"] == 0
        assert audit["embedding_dimension"] == 1024
        assert audit["mapped_child_count"] == 1
        assert len(audit["parent_manifest_sha256"]) == 64
        assert len(audit["mapping_manifest_sha256"]) == 64

        with pytest.raises(ValueError, match="向量维度"):
            run._audit_parent_child_snapshot(
                child_db,
                SimpleNamespace(collection=_Collection(["p1_c0"], 3)),
                parent_db,
                mapping,
            )
        with pytest.raises(ValueError, match="ID 不一致"):
            run._audit_parent_child_snapshot(
                child_db,
                SimpleNamespace(collection=_Collection(["p1_c9"])),
                parent_db,
                mapping,
            )
    finally:
        child_db.close()
        parent_db.close()
        child_engine.dispose()
        parent_engine.dispose()

