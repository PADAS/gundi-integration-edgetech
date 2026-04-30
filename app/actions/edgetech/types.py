import logging
from datetime import datetime
from typing import Any, List, Optional

import pydantic

logger = logging.getLogger(__name__)

TRAP_DEPLOYMENT_EVENT = "trap_deployed"
TRAP_RETRIEVED_EVENT = "trap_retrieved"


class GeoLocation(pydantic.BaseModel):
    latitude: float
    longitude: float


class ChangeRecordEntry(pydantic.BaseModel):
    key: str
    oldValue: Any
    newValue: Any


class ChangeRecord(pydantic.BaseModel):
    type: str
    timestamp: datetime
    changes: List[ChangeRecordEntry]


class CurrentState(pydantic.BaseModel):
    etag: str
    isDeleted: bool
    serialNumber: str
    releaseCommand: str
    statusCommand: str
    idCommand: str
    isNfcTag: Optional[bool]
    modelNumber: Optional[str]
    dateOfManufacture: Optional[datetime]
    dateOfBatteryChange: Optional[datetime]
    dateDeployed: Optional[datetime]
    isDeployed: Optional[bool]
    dateRecovered: Optional[datetime]
    recoveredLatDeg: Optional[float]
    recoveredLonDeg: Optional[float]
    recoveredRangeM: Optional[float]
    dateStatus: Optional[datetime]
    statusRangeM: Optional[float]
    statusIsTilted: Optional[bool]
    statusBatterySoC: Optional[int]
    lastUpdated: datetime
    latDeg: Optional[float]
    lonDeg: Optional[float]
    endLatDeg: Optional[float]
    endLonDeg: Optional[float]
    isTwoUnitLine: Optional[bool]
    endUnit: Optional[str]
    startUnit: Optional[str]

    class Config:
        json_encoders = {datetime: lambda val: val.isoformat()}

    @pydantic.validator("etag")
    def sanitize_etag(cls, val):
        return val.strip('"') if val else val


class Buoy(pydantic.BaseModel):
    """Represents a ropeless buoy device and its change records."""

    currentState: CurrentState
    serialNumber: str
    userId: str
    changeRecords: List[ChangeRecord]

    @property
    def has_location(self) -> bool:
        """Check if the buoy has a valid location."""

        has_retrieved_location = (
            self.currentState.recoveredLatDeg is not None
            and self.currentState.recoveredLonDeg is not None
        )

        has_deployed_location = (
            self.currentState.latDeg is not None
            and self.currentState.lonDeg is not None
        )

        has_deployed_end_location = (
            self.currentState.endLatDeg is not None
            and self.currentState.endLonDeg is not None
        )

        return (
            has_retrieved_location or has_deployed_location or has_deployed_end_location
        )

    @property
    def is_end_unit_record(self) -> bool:
        # A two-unit gearset is sent as two records: the start record carries
        # endUnit (pointing at the end's serial), the end record carries startUnit
        # (pointing back at the start's serial). The start record is the canonical
        # gearset; its serialNumber is the gearset id. End-unit records exist only
        # to supply the end-unit location and should not produce their own gearset.
        # Note: only meaningful while deployed — after a haul, EdgeTech nulls out
        # both startUnit and endUnit, so this returns False for hauled records.
        return bool(self.currentState.startUnit)
