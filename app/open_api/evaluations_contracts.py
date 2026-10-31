"""Published HTTP contracts, independent of controllers."""
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict
from app.entity.evaluation.evaluation_dataset import WorkbenchDataset

class DatasetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: WorkbenchDataset
