"""python -m app.preflight [--allow-missing-worker]"""
import argparse
import json
import sys

from app.service.preflight import deployment_preflight
from app.core.config import Settings
from app.dao.postgres.database import Database
from app.dao.milvus import MilvusVectorStore


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="只读部署预检，不创建表、文件或调用付费模型")
    parser.add_argument("--allow-missing-worker", action="store_true", help="仅用于 Worker 启动前的部署准备检查")
    options = parser.parse_args()
    database = None
    try:
        settings = Settings()
        database = Database(settings)
        result = deployment_preflight(settings, database, MilvusVectorStore(settings), require_worker=not options.allow_missing_worker)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 1
    except Exception as error:
        print(json.dumps({"ready": False, "code": "PREFLIGHT_CONFIGURATION_INVALID", "error_type": type(error).__name__}))
        return 1
    finally:
        if database:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
