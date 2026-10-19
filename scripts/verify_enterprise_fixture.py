"""Verify the frozen synthetic assets without ingestion, network or generation."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.application.evaluation_dataset import WorkbenchDataset


def verify(directory: Path):
    directory = directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Fixture hash mismatch or unsafe path: {name}")
    allowlist = set(manifest["ingestion_allowlist"])
    actual = {path.relative_to(directory).as_posix() for path in (directory / "upload_corpus").iterdir() if path.is_file()}
    if allowlist != actual or len(allowlist) != 12 or any(Path(name).parent.as_posix() != "upload_corpus" for name in allowlist):
        raise ValueError("Only the twelve declared business documents may be ingested")
    dataset = WorkbenchDataset.model_validate_json((directory / "dataset.json").read_text(encoding="utf-8"))
    if len(dataset.cases) != 80 or dataset.fingerprint != manifest["dataset_fingerprint"]:
        raise ValueError("Frozen dataset identity changed")
    splits = json.loads((directory / "splits.json").read_text(encoding="utf-8"))
    for split, count in (("dev", 20), ("frozen", 60)):
        ids = [case.id for case in dataset.cases if case.split == split]
        if len(ids) != count or set(ids) != set(splits[split]):
            raise ValueError("Development/frozen split changed")
    for case in dataset.cases:
        for anchor in case.gold_anchors:
            if f"upload_corpus/{anchor.canonical_name}" not in allowlist:
                raise ValueError("Gold anchor references a non-ingestible document")
    return {"documents": len(allowlist), "cases": len(dataset.cases), "dev": 20, "frozen": 60,
            "dataset_fingerprint": dataset.fingerprint}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=ROOT / "evals/rag/synthetic_enterprise")
    print(json.dumps(verify(parser.parse_args().directory), indent=2))
