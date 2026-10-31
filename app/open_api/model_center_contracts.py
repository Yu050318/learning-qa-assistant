"""Published HTTP contracts, independent of controllers."""
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field
from app.entity.dto.model_center import ManagedModels, Provider
from app.entity.bo.model_catalog import ModelCatalog, ProfileId
from app.core.errors import AppError

class ConfigurationWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    configuration: ManagedModels


class CatalogWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    catalog: ModelCatalog
