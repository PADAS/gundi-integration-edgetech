import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pydantic

logger = logging.getLogger(__name__)

SOURCE_TYPE = "ropeless_buoy"
SUBJECT_SUBTYPE = "ropeless_buoy_device"
GEAR_DEPLOYED_EVENT = "gear_deployed"
GEAR_RETRIEVED_EVENT = "gear_retrieved"


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

    class Config:
        json_encoders = {datetime: lambda val: val.isoformat()}

    @pydantic.validator("etag")
    def sanitize_etag(cls, val):
        return val.strip('"') if val else val


class Buoy(pydantic.BaseModel):
    """Represents a ropeless buoy device and its change records."""

    currentState: CurrentState
    serialNumber: str
    changeRecords: List[ChangeRecord]

    @property
    def deployed(self) -> bool:
        isDeployed = self.currentState.isDeployed or False
        isDeleted = self.currentState.isDeleted or False
        return isDeployed and not isDeleted

    def _create_device_record(
        self,
        label: str,
        latitude: float,
        longitude: float,
        subject_name: str,
        last_updated: str,
    ) -> Dict[str, Any]:
        """Return a device record with the given parameters."""
        return {
            "label": label,
            "location": {"latitude": latitude, "longitude": longitude},
            "device_id": subject_name,
            "last_updated": last_updated,
        }

    def _create_observation_record(
        self,
        subject_name: str,
        lat: float,
        lon: float,
        devices: List[Dict[str, Any]],
        is_active: bool,
        last_updated: str,
    ) -> Dict[str, Any]:
        """Return an observation record with the given parameters."""
        devices_names = [device["device_id"] for device in devices]
        concatenated = "".join(devices_names)
        display_id = hashlib.sha256(concatenated.encode("utf-8")).hexdigest()[:12]

        return {
            "name": subject_name,
            "source": subject_name,
            "type": SOURCE_TYPE,
            "subject_type": SUBJECT_SUBTYPE,
            "is_active": is_active,
            "recorded_at": last_updated,
            "location": {"lat": lat, "lon": lon},
            "additional": {
                "subject_name": subject_name,
                "edgetech_serial_number": self.serialNumber,
                "display_id": display_id,
                "subject_is_active": is_active,
                "event_type": GEAR_DEPLOYED_EVENT
                if is_active
                else GEAR_RETRIEVED_EVENT,
                "devices": devices,
            },
        }

    def _build_observations(
        self,
        is_deployed: bool,
        recorded_at: datetime,
        prefix: str,
        start_lat: float,
        start_lon: float,
        end_lat: Optional[float],
        end_lon: Optional[float],
        subject_status: Optional[bool] = None,
        was_part_of_trawl: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return one or two observations for deployment or retrieval events,
        including associated device records.
        """
        observations = []
        iso_time = recorded_at.isoformat()

        is_trawl = end_lat is not None and end_lon is not None

        devices = []
        subject_name_a = f"{prefix}{self.serialNumber}_A"
        subject_name_b = f"{prefix}{self.serialNumber}_B"
        device_a = self._create_device_record(
            "a", start_lat, start_lon, subject_name_a, iso_time
        )
        devices.append(device_a)
        if is_trawl or (not is_deployed and was_part_of_trawl):
            if end_lat is None or end_lon is None:
                end_lat = start_lat
                end_lon = start_lon
            device_b = self._create_device_record(
                "b", end_lat, end_lon, subject_name_b, iso_time
            )
            devices.append(device_b)

        observation_a = self._create_observation_record(
            subject_name=subject_name_a,
            lat=start_lat,
            lon=start_lon,
            devices=devices,
            is_active=is_deployed,
            last_updated=iso_time,
        )
        observations.append(observation_a)

        if is_trawl or (not is_deployed and was_part_of_trawl):
            observation_b = self._create_observation_record(
                subject_name=subject_name_b,
                lat=end_lat or start_lat,
                lon=end_lon or start_lon,
                devices=devices,
                is_active=is_deployed,
                last_updated=iso_time,
            )
            observations.append(observation_b)

        # If the subject status is True, is_deployed is True
        # Add an observation a minute earlier hauling the buoy
        if subject_status and is_deployed:
            if was_part_of_trawl and len(devices) == 1:
                # If the buoy was part of a trawl, we need to add the second device
                # to the observation
                device_b = self._create_device_record(
                    "b", start_lat, start_lon, subject_name_b, iso_time
                )
                devices.append(device_b)

            hauling_observation_timestamp = recorded_at - timedelta(minutes=1)
            hauling_observation_a = self._create_observation_record(
                subject_name=subject_name_a,
                lat=start_lat,
                lon=start_lon,
                devices=devices,
                is_active=False,
                last_updated=hauling_observation_timestamp.isoformat(),
            )
            observations.append(hauling_observation_a)
            if len(devices) > 1:
                hauling_observation_b = self._create_observation_record(
                    subject_name=subject_name_b,
                    lat=start_lat,
                    lon=start_lon,
                    devices=devices,
                    is_active=False,
                    last_updated=hauling_observation_timestamp.isoformat(),
                )
                observations.append(hauling_observation_b)

        return observations

    def generate_observations_from_change_records(
        self,
        prefix: str,
        subject_status: Optional[bool],
        last_observation_timestamp: Optional[datetime] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[GeoLocation], Optional[GeoLocation]]:
        """
        Generate observations from deployment or retrieval changes in the changeRecords.
        Skips records that are older than or equal to last_observation_timestamp if provided.
        """
        observations = []
        change_records = sorted(self.changeRecords, key=lambda x: x.timestamp)
        last_know_position = None
        last_know_end_position = None
        for record in change_records:
            if record.type != "MODIFY":
                logger.debug(
                    "Skipping change record of type %s (Serial %s)",
                    record.type,
                    self.serialNumber,
                )
                continue

            changes = {
                change.key: {"old": change.oldValue, "new": change.newValue}
                for change in record.changes
            }

            wasDeployed = changes.get("dateDeployed", {}).get("new") is not None
            wasRecovered = changes.get("dateRecovered", {}).get("new") is not None
            if not (wasDeployed or wasRecovered):
                continue

            recorded_str = changes.get("dateDeployed", {}).get("new") or changes.get(
                "dateRecovered", {}
            ).get("new")
            if not recorded_str:
                continue

            recorded_str = recorded_str.replace("Z", "+00:00")
            recorded_at = datetime.fromisoformat(recorded_str).replace(microsecond=0)

            # Skip if the event in change record is the same as the current state
            current_state_observation_recorded_at = (
                self.currentState.dateDeployed
                or self.currentState.dateRecovered
                or self.currentState.lastUpdated
            ).replace(microsecond=0)
            if (recorded_at == current_state_observation_recorded_at) and (
                self.currentState.endLatDeg or self.currentState.recoveredLatDeg
            ):
                continue
            # Skip if the event is older than or equal to last_observation_timestamp if provided
            if last_observation_timestamp and recorded_at <= last_observation_timestamp:
                continue

            if wasRecovered:
                lat = changes.get("latDeg", {}).get("old")
                lon = changes.get("lonDeg", {}).get("old")
                end_lat = changes.get("endLatDeg", {}).get("old")
                end_lon = changes.get("endLonDeg", {}).get("old")
                is_deployed = False
            else:
                lat = changes.get("latDeg", {}).get("new")
                lon = changes.get("lonDeg", {}).get("new")
                end_lat = changes.get("endLatDeg", {}).get("new")
                end_lon = changes.get("endLonDeg", {}).get("new")
                is_deployed = True

            if lat is None or lon is None:
                continue

            last_know_position = GeoLocation(latitude=lat, longitude=lon)
            if end_lat is not None and end_lon is not None:
                last_know_end_position = GeoLocation(
                    latitude=end_lat, longitude=end_lon
                )

            event_observations = self._build_observations(
                is_deployed=is_deployed,
                recorded_at=recorded_at,
                prefix=prefix,
                start_lat=lat,
                start_lon=lon,
                end_lat=end_lat,
                end_lon=end_lon,
                subject_status=subject_status,
            )
            observations.extend(event_observations)

        return observations, last_know_position, last_know_end_position

    def create_observations(
        self,
        prefix: str,
        subject_status: Optional[bool] = None,
        last_observation_timestamp: Optional[datetime] = None,
        last_know_position_previous_states: Optional[GeoLocation] = None,
        last_know_end_position_previous_states: Optional[GeoLocation] = None,
        was_part_of_trawl: Optional[bool] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[GeoLocation], Optional[GeoLocation]]:
        """
        Return observations from the current state or from changeRecords if available.
        Skips if the event is older than or equal to last_observation_timestamp when provided.
        """
        observations: List[Dict[str, Any]] = []
        last_known_position = last_know_position_previous_states
        last_known_end_position = last_know_end_position_previous_states
        if self.changeRecords:
            observations, last_known_position, last_known_end_position = (
                self.generate_observations_from_change_records(
                    prefix=prefix,
                    last_observation_timestamp=last_observation_timestamp,
                    subject_status=subject_status,
                )
            )

        current = self.currentState
        is_deployed = current.isDeployed or False

        recorded_at = (
            current.dateDeployed or current.dateRecovered or current.lastUpdated
        ).replace(microsecond=0)

        if last_observation_timestamp and recorded_at <= last_observation_timestamp:
            return observations, last_known_position, last_known_end_position

        start_lat = current.latDeg or current.recoveredLatDeg
        start_lon = current.lonDeg or current.recoveredLonDeg
        end_lat = current.endLatDeg
        end_lon = current.endLonDeg

        if last_know_position_previous_states is not None:
            start_lat = start_lat or last_know_position_previous_states.latitude
            start_lon = start_lon or last_know_position_previous_states.longitude

        if start_lat is None or start_lon is None:
            return observations, last_known_position, last_known_end_position

        last_known_position = GeoLocation(latitude=start_lat, longitude=start_lon)

        if (
            end_lat is not None
            and end_lon is not None
            and last_known_end_position is None
        ):
            last_known_end_position = GeoLocation(latitude=end_lat, longitude=end_lon)
        elif (
            end_lat is None and end_lon is None and last_known_end_position is not None
        ):
            last_known_end_position = None

        observations.extend(
            self._build_observations(
                is_deployed=is_deployed,
                recorded_at=recorded_at,
                prefix=prefix,
                start_lat=start_lat,
                start_lon=start_lon,
                end_lat=end_lat,
                end_lon=end_lon,
                was_part_of_trawl=was_part_of_trawl,
                subject_status=subject_status,
            )
        )

        return observations, last_known_position, last_known_end_position
