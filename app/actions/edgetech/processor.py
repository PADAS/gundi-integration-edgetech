import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from app.actions.buoy import BuoyClient
from app.actions.buoy.types import BuoyDevice, BuoyGear
from app.actions.edgetech.types import Buoy
from app.actions.utils import get_hashed_user_id

logger = logging.getLogger(__name__)


class EdgeTechProcessor:
    def __init__(
        self,
        data: List[Buoy],
        er_token: str,
        er_url: str,
        filters: Optional[dict] = None,
    ):
        """
        Initialize an EdgetTechProcessor instance.

        This constructor parses raw data records into Buoy objects, initializes an ER client for
        further processing, and sets up filters for processing buoy data.

        Args:
            data (List[Buoy]): A list of Buoy objects (parsed from raw data).
            er_token (str): Authentication token for the ER client.
            er_url (str): URL endpoint for the ER client.
            filters (dict, optional): A dictionary containing filter criteria (e.g., start_date and end_date).
                                      If not provided, default filters covering the last 180 days are used.
        """
        self._data = [Buoy.parse_obj(record) for record in data]
        self._er_client = BuoyClient(er_token, er_url)
        self._filters = filters or self._get_default_filters()
        self._prefix = "edgetech_"

    def _get_default_filters(self) -> Dict[str, Any]:
        """
        Generate default filter criteria for processing buoy data.

        By default, this method defines a time window starting 30 minutes before the current UTC time.

        Returns:
            Dict[str, Any]: A dictionary with 'start_datetime' keys defining the filter window.
        """
        start_datetime = datetime.now(timezone.utc) - timedelta(minutes=30)
        return {"start_datetime": start_datetime}

    @staticmethod
    def _utcnow() -> datetime:
        """Return the current UTC time. Extracted for testability."""
        return datetime.now(timezone.utc)

    def _remove_milliseconds(self, dt: datetime) -> datetime:
        """
        Remove milliseconds from a datetime object.

        Args:
            dt: The datetime object to process.
        """
        return dt.replace(microsecond=0)

    async def _create_gear_payload(
        self,
        buoy: Buoy,
        device_status: str,
        manufacturer_id_to_source_id: Dict[str, str],
        end_unit_buoy: Optional[Buoy] = None,
        end_unit_device_from_er: Optional[BuoyDevice] = None,
        set_id: Optional[str] = None,
        include_initial_deployment: bool = True,
    ) -> Dict[str, Any]:
        """
        Create a gear payload directly from Buoy data.

        Args:
            buoy: The main Buoy object
            device_status: Status of the device (deployed/hauled)
            manufacturer_id_to_source_id: Mapping of manufacturer_id to source_id for existing sources
            end_unit_buoy: Optional second buoy for two-unit lines (from EdgeTech sync window)
            end_unit_device_from_er: Optional end-unit device from ER when end_unit_buoy is not
                in the sync window; used for updates and deployments (e.g. re-deployments or
                recovery deployments where the end unit wasn't in the EdgeTech sync window).
            set_id: Optional gear set ID (auto-generated if not provided)
            include_initial_deployment: Whether to include initial_deployment_date

        Returns:
            Dict in the format expected by /api/v1/gear/ POST endpoint
        """
        hashed_user_id = get_hashed_user_id(buoy.userId)

        last_updated = buoy.currentState.lastUpdated
        last_deployed = buoy.currentState.dateDeployed or last_updated
        # For initial deployments, use dateDeployed as recorded_at.
        # For updates (position changes), use current time so that recorded_at
        # differs from previous syncs.  EdgeTech can update latDeg/lonDeg
        # without changing lastUpdated, so reusing lastUpdated as recorded_at
        # could collide with a previously-accepted observation and be rejected
        # by ER's (device_id, recorded_at) unique constraint.
        if include_initial_deployment:
            deployment_recorded_at = last_deployed or self._utcnow()
        else:
            deployment_recorded_at = self._utcnow()

        # Create devices list
        devices = []

        # Main device
        main_device_id = f"{buoy.serialNumber}_{hashed_user_id}"

        secondary_device_id = None
        # Two-unit gearset branches lead so they win even if a two-unit start
        # record ever populates endLatDeg/endLonDeg (which today it does not).
        if end_unit_buoy:
            secondary_device_id = f"{end_unit_buoy.serialNumber}_{hashed_user_id}"
            secondary_latitude = end_unit_buoy.currentState.latDeg
            secondary_longitude = end_unit_buoy.currentState.lonDeg
            secondary_last_deployed = (
                end_unit_buoy.currentState.dateDeployed or last_updated
            )
            # Use same logic as main device for recorded_at
            if include_initial_deployment:
                secondary_recorded_at = secondary_last_deployed or self._utcnow()
            else:
                secondary_recorded_at = deployment_recorded_at
            secondary_device_additional_data = json.loads(end_unit_buoy.json())
            secondary_device_additional_data.pop("changeRecords", None)
        elif end_unit_device_from_er:
            # End unit not in EdgeTech sync window (e.g. location-only update on start unit,
            # or re-deployment where end unit wasn't updated in EdgeTech);
            # use current state from ER so we can still send the payload.
            secondary_device_id = end_unit_device_from_er.mfr_device_id
            secondary_latitude = end_unit_device_from_er.location.latitude
            secondary_longitude = end_unit_device_from_er.location.longitude
            # For initial deployments (including re-deployments), use the start
            # unit's deployment time so both devices reflect the new deployment.
            # For updates, use the ER value (we're not re-deploying).
            if include_initial_deployment:
                secondary_last_deployed = last_deployed
            else:
                secondary_last_deployed = (
                    end_unit_device_from_er.last_deployed or last_updated
                )
            # Use same recorded_at as start unit so we don't resend an existing
            # (device_id, recorded_at) pair and so both devices are consistent.
            # ER-sourced last_updated is kept in device_additional_data for traceability.
            secondary_recorded_at = deployment_recorded_at
            secondary_device_additional_data = {
                "serialNumber": secondary_device_id.split("_")[0],
                "lastUpdated": (
                    end_unit_device_from_er.last_updated.isoformat()
                    if end_unit_device_from_er.last_updated
                    else None
                ),
            }
        elif buoy.currentState.endLatDeg and buoy.currentState.endLonDeg:
            # Single-unit buoy with start+end coordinates: emit two devices
            # under one gearset (suffixed _A / _B).
            main_device_id += "_A"
            secondary_device_id = f"{buoy.serialNumber}_{hashed_user_id}_B"
            secondary_latitude = buoy.currentState.endLatDeg
            secondary_longitude = buoy.currentState.endLonDeg
            secondary_last_deployed = last_deployed
            secondary_recorded_at = deployment_recorded_at
            secondary_device_additional_data = json.loads(buoy.json())
            secondary_device_additional_data.pop("changeRecords", None)

        main_device = {
            "device_id": manufacturer_id_to_source_id.get(main_device_id)
            or str(uuid4()),
            "mfr_device_id": main_device_id,
            "last_deployed": self._remove_milliseconds(last_deployed).isoformat(),
            "last_updated": self._remove_milliseconds(last_updated).isoformat(),
            "recorded_at": self._remove_milliseconds(
                deployment_recorded_at
            ).isoformat(),
            "device_status": device_status,
            "location": {
                "latitude": buoy.currentState.latDeg,
                "longitude": buoy.currentState.lonDeg,
            },
        }

        # Add raw data - convert to JSON-serializable format
        raw_data = json.loads(buoy.json())
        raw_data.pop("changeRecords", None)
        main_device["device_additional_data"] = raw_data

        devices.append(main_device)

        if secondary_device_id:
            secondary_device = {
                "device_id": manufacturer_id_to_source_id.get(secondary_device_id)
                or str(uuid4()),
                "mfr_device_id": secondary_device_id,
                "last_deployed": self._remove_milliseconds(
                    secondary_last_deployed
                ).isoformat(),
                "last_updated": self._remove_milliseconds(last_updated).isoformat(),
                "recorded_at": self._remove_milliseconds(
                    secondary_recorded_at
                ).isoformat(),
                "device_status": device_status,
                "location": {
                    "latitude": secondary_latitude,
                    "longitude": secondary_longitude,
                },
                "device_additional_data": secondary_device_additional_data,
            }
            devices.append(secondary_device)

        # Determine deployment type
        deployment_type = "trawl" if len(devices) > 1 else "single"

        # Build payload
        payload = {
            "set_id": set_id or str(uuid4()),
            "owner_id": buoy.userId,
            "manufacturer_name": "EdgeTech",
            "deployment_type": deployment_type,
            "devices_in_set": len(devices),
            "devices": devices,
        }

        if include_initial_deployment:
            payload["initial_deployment_date"] = self._remove_milliseconds(
                last_deployed
            ).isoformat()
        payload_json = json.dumps(payload, default=str)
        payload = json.loads(payload_json)
        return payload

    def _create_haul_payload(
        self,
        er_gear: BuoyGear,
        edgetech_buoy: Optional[Buoy] = None,
        is_redeployment: bool = False,
    ) -> Dict[str, Any]:
        """
        Create a haul payload from an existing ER gear.

        Attempts to use recovery location from EdgeTech data if available,
        otherwise falls back to the last known deployed location from ER.

        Args:
            er_gear: The existing gear from ER
            edgetech_buoy: Optional EdgeTech buoy data with potential recovery location
            is_redeployment: Whether this haul is part of a re-deployment sequence.
                When True and no dateRecovered is found, uses dateDeployed - 1s
                as a deterministic recorded_at instead of falling back to
                lastUpdated, which could collide with the subsequent deploy
                payload's recorded_at after millisecond truncation.

        Returns:
            Dict in the format expected by /api/v1/gear/ POST endpoint
        """
        devices = []

        # Check if EdgeTech provides recovery location
        recovery_location_available = False
        recovery_lat = None
        recovery_lon = None

        # Determine the recorded_at timestamp for the haul event
        # Priority: dateRecovered from currentState → dateRecovered from changeRecords
        # → dateDeployed - 1s (re-deployments without dateRecovered) → lastUpdated.
        #
        # For re-deployments, only consider changeRecord dateRecovered values that
        # occurred *after* the most recent deployment among the ER gear's devices.
        # Using max() (not min()) guarantees haul_recorded_at >= every device's
        # last_deployed — Buoy API stores [last_deployed, recorded_at] as a
        # tstzrange per device and rejects an inverted range. Devices in the
        # same gearset can legitimately have different last_deployed values
        # (e.g. end unit's own dateDeployed), so the earliest date is an unsafe
        # floor: a recovery between the earliest and latest deploys passes
        # min() but produces an inverted range for the later-deployed device.
        haul_recorded_at = self._utcnow()
        er_deployment_dates = [
            d.last_deployed
            for d in er_gear.devices
            if isinstance(getattr(d, "last_deployed", None), datetime)
        ]
        max_er_deployment_date: Optional[datetime] = (
            max(er_deployment_dates) if er_deployment_dates else None
        )
        change_record_min_date: Optional[datetime] = (
            max_er_deployment_date if is_redeployment else None
        )

        if edgetech_buoy:
            if edgetech_buoy.currentState.dateRecovered:
                haul_recorded_at = edgetech_buoy.currentState.dateRecovered
            else:
                # Look for dateRecovered in changeRecords (re-deployment: currentState
                # was overwritten by the redeploy, but the haul timestamp is preserved
                # in the change history)
                recovered_at_from_changes = (
                    self._get_date_recovered_from_change_records(
                        edgetech_buoy, min_date=change_record_min_date
                    )
                )
                if recovered_at_from_changes:
                    haul_recorded_at = recovered_at_from_changes
                    logger.info(
                        f"Using dateRecovered from changeRecords for haul of "
                        f"{edgetech_buoy.serialNumber}: {recovered_at_from_changes}"
                    )
                elif is_redeployment and edgetech_buoy.currentState.dateDeployed:
                    # For re-deployments without dateRecovered, lastUpdated and
                    # dateDeployed are typically in the same second (both reflect
                    # the new deployment). Use dateDeployed - 1s as a deterministic
                    # haul timestamp that is guaranteed not to collide with the
                    # deploy payload's recorded_at after millisecond truncation.
                    haul_recorded_at = (
                        edgetech_buoy.currentState.dateDeployed - timedelta(seconds=1)
                    )
                elif edgetech_buoy.currentState.lastUpdated:
                    haul_recorded_at = edgetech_buoy.currentState.lastUpdated

        # Safety clamp: Buoy API builds a per-device tstzrange
        # [last_deployed, recorded_at] and rejects inverted ranges with
        # "range lower bound must be less than or equal to range upper bound".
        # If upstream data produced a haul_recorded_at older than any device's
        # last_deployed (e.g. anomalous ER state where devices in the same
        # gearset have disparate deployment dates), bump recorded_at to
        # max(last_deployed) + 1s. The +1s avoids colliding with the original
        # deploy event on the (device_id, recorded_at) uniqueness constraint
        # after the millisecond-truncation step in the payload builder.
        if (
            max_er_deployment_date is not None
            and haul_recorded_at <= max_er_deployment_date
        ):
            clamped = max_er_deployment_date + timedelta(seconds=1)
            logger.warning(
                "Haul recorded_at (%s) is not after max device last_deployed (%s) "
                "for gear %s; clamping to %s to keep the deploy/haul range valid.",
                haul_recorded_at,
                max_er_deployment_date,
                er_gear.display_id,
                clamped,
            )
            haul_recorded_at = clamped

        if edgetech_buoy:
            if (
                edgetech_buoy.currentState.recoveredLatDeg is not None
                and edgetech_buoy.currentState.recoveredLonDeg is not None
            ):
                recovery_location_available = True
                recovery_lat = edgetech_buoy.currentState.recoveredLatDeg
                recovery_lon = edgetech_buoy.currentState.recoveredLonDeg
            else:
                # Re-deployment: recovery location may be in changeRecords
                # (currentState was overwritten by the redeploy)
                rec_lat, rec_lon = self._get_recovery_location_from_change_records(
                    edgetech_buoy, min_date=change_record_min_date
                )
                if rec_lat is not None and rec_lon is not None:
                    recovery_location_available = True
                    recovery_lat = rec_lat
                    recovery_lon = rec_lon

            if recovery_location_available:
                logger.info(
                    f"Using recovery location for gear {er_gear.display_id}: "
                    f"({recovery_lat}, {recovery_lon})"
                )

        for device in er_gear.devices:
            # Use recovery location if available, otherwise use deployed location from ER
            if recovery_location_available:
                location_lat = recovery_lat
                location_lon = recovery_lon
            else:
                location_lat = device.location.latitude
                location_lon = device.location.longitude
                logger.info(
                    f"No recovery location available for device {device.mfr_device_id}, "
                    f"using last deployed location from ER: ({location_lat}, {location_lon})"
                )

            haul_device = {
                "device_id": device.device_id,
                "mfr_device_id": device.mfr_device_id,
                "last_deployed": (
                    device.last_deployed.isoformat()
                    if device.last_deployed
                    else device.last_updated.isoformat()
                ),
                "last_updated": self._remove_milliseconds(self._utcnow()).isoformat(),
                "recorded_at": self._remove_milliseconds(haul_recorded_at).isoformat(),
                "device_status": "hauled",
                "location": {
                    "latitude": location_lat,
                    "longitude": location_lon,
                },
            }
            devices.append(haul_device)

        payload = {
            "deployment_type": er_gear.type,
            "manufacturer_name": "EdgeTech",
            "set_id": er_gear.display_id,
            "devices": devices,
        }

        return payload

    @staticmethod
    def _expected_device_locations(
        buoy: Buoy,
        end_unit_buoy: Optional[Buoy],
        end_unit_device_from_er: Optional[BuoyDevice],
        hashed_user_id: str,
    ) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
        # Map every ER mfr_device_id we expect to publish for this gearset to
        # its intended (lat, lon) from the current EdgeTech state. The update
        # loop diffs this against the live ER gear to decide whether anything
        # actually moved — covers single-device, single-unit-trawl (A/B), and
        # two-unit (paired records) shapes uniformly.
        expected: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        primary_id = f"{buoy.serialNumber}_{hashed_user_id}"
        primary_loc = (buoy.currentState.latDeg, buoy.currentState.lonDeg)

        if end_unit_buoy is not None:
            expected[primary_id] = primary_loc
            expected[f"{end_unit_buoy.serialNumber}_{hashed_user_id}"] = (
                end_unit_buoy.currentState.latDeg,
                end_unit_buoy.currentState.lonDeg,
            )
        elif end_unit_device_from_er is not None:
            # End record absent from sync window; only the start side carries
            # new data so we only diff the primary device.
            expected[primary_id] = primary_loc
        elif (
            buoy.currentState.endLatDeg is not None
            and buoy.currentState.endLonDeg is not None
        ):
            expected[f"{primary_id}_A"] = primary_loc
            expected[f"{primary_id}_B"] = (
                buoy.currentState.endLatDeg,
                buoy.currentState.endLonDeg,
            )
        else:
            expected[primary_id] = primary_loc

        return expected

    @staticmethod
    def _gearset_location_changed(
        er_gear: BuoyGear,
        expected_locations: Dict[str, Tuple[Optional[float], Optional[float]]],
    ) -> bool:
        # Match each ER device to an expected entry by mfr_device_id, tolerating
        # `_A`/`_B` suffix mismatches: a gearset previously deployed as
        # single-unit-trawl (devices stored with `_A`/`_B`) may now be reporting
        # as two-unit (no suffix) and vice versa. The serial-prefix is the
        # stable identifier across shape transitions.
        for er_device in er_gear.devices:
            mfr_id = er_device.mfr_device_id
            expected = expected_locations.get(mfr_id)
            if expected is None and (mfr_id.endswith("_A") or mfr_id.endswith("_B")):
                expected = expected_locations.get(mfr_id[:-2])
            if expected is None:
                stripped_candidates = (f"{mfr_id}_A", f"{mfr_id}_B")
                for candidate in stripped_candidates:
                    if candidate in expected_locations:
                        expected = expected_locations[candidate]
                        break
            if expected is None:
                continue
            if (er_device.location.latitude, er_device.location.longitude) != expected:
                return True
        return False

    def _is_hauled_or_recovered(self, record: Buoy) -> bool:
        """
        Determine if a buoy record indicates a hauled/recovered state.

        A buoy is considered hauled/recovered if:
        - isDeleted is True, OR
        - isDeployed is False, OR
        - dateRecovered is present

        Args:
            record (Buoy): The buoy record to check.

        Returns:
            bool: True if the buoy should be treated as hauled/recovered.
        """
        return (
            record.currentState.isDeleted
            or not record.currentState.isDeployed
            or record.currentState.dateRecovered is not None
        )

    @staticmethod
    def _get_date_recovered_from_change_records(
        buoy: Buoy, min_date: Optional[datetime] = None
    ) -> Optional[datetime]:
        """
        Find the most recent dateRecovered value from a buoy's changeRecords.

        This is needed for re-deployment scenarios where the buoy was hauled and
        immediately redeployed — the currentState no longer has dateRecovered
        (cleared by the redeploy), but the changeRecords preserve the haul timestamp.

        Args:
            buoy: The buoy record to search.
            min_date: If provided, only consider dateRecovered values that are
                after this date.  Used during re-deployments to exclude stale
                recovery dates from a previous deploy/haul lifecycle.

        Returns:
            The most recent dateRecovered datetime, or None if not found.
        """
        # recovered_dt is always tz-aware (we append +00:00), but min_date can
        # come from BuoyDevice.last_deployed which pydantic does not coerce to
        # tz-aware. Normalize naive → UTC to avoid a TypeError on comparison.
        if min_date is not None and min_date.tzinfo is None:
            min_date = min_date.replace(tzinfo=timezone.utc)
        most_recent = None
        for record in buoy.changeRecords:
            for change in record.changes:
                if change.key == "dateRecovered" and change.newValue is not None:
                    try:
                        recovered_dt = datetime.fromisoformat(
                            str(change.newValue).replace("Z", "+00:00")
                        )
                        if min_date and recovered_dt < min_date:
                            continue
                        if most_recent is None or recovered_dt > most_recent:
                            most_recent = recovered_dt
                    except (ValueError, TypeError):
                        continue
        return most_recent

    @staticmethod
    def _get_recovery_location_from_change_records(
        buoy: Buoy,
        min_date: Optional[datetime] = None,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Find the most recent recovery location from a buoy's changeRecords.

        Needed for re-deployment scenarios where recoveredLatDeg/recoveredLonDeg
        in currentState were cleared by the redeploy.

        Args:
            buoy: The buoy record to search.
            min_date: If provided, only consider recovery records whose
                dateRecovered value is after this date.  Used during
                re-deployments to exclude stale data from a prior lifecycle.

        Returns:
            Tuple of (latitude, longitude) or (None, None) if not found.
        """
        # recovered_dt is always tz-aware (we append +00:00), but min_date can
        # come from BuoyDevice.last_deployed which pydantic does not coerce to
        # tz-aware. Normalize naive → UTC to avoid a TypeError on comparison.
        if min_date is not None and min_date.tzinfo is None:
            min_date = min_date.replace(tzinfo=timezone.utc)
        # Find the changeRecord that set dateRecovered most recently —
        # its sibling entries will have the recovery coordinates.
        best_timestamp = None
        best_lat = None
        best_lon = None
        for record in buoy.changeRecords:
            lat = None
            lon = None
            recovered_dt = None
            for change in record.changes:
                if change.key == "dateRecovered" and change.newValue is not None:
                    try:
                        recovered_dt = datetime.fromisoformat(
                            str(change.newValue).replace("Z", "+00:00")
                        )
                    except (ValueError, TypeError):
                        pass
                elif change.key == "recoveredLatDeg" and change.newValue is not None:
                    lat = change.newValue
                elif change.key == "recoveredLonDeg" and change.newValue is not None:
                    lon = change.newValue
            if recovered_dt is None:
                continue
            if min_date and recovered_dt < min_date:
                continue
            if lat is not None and lon is not None:
                if best_timestamp is None or record.timestamp > best_timestamp:
                    best_timestamp = record.timestamp
                    best_lat = lat
                    best_lon = lon
        return best_lat, best_lon

    def _should_skip_buoy(self, record: Buoy) -> Tuple[bool, Optional[str]]:
        """
        Determine if a buoy record should be skipped during processing.

        Buoys are kept in the dataset for processing if:
        1. They have location data (deployed or recovery location), OR
        2. They indicate a hauled/recovered state (even without recovery location)

        This ensures that hauled gears without recovery coordinates can still be
        updated to hauled status in EarthRanger.

        Args:
            record (Buoy): The buoy record to check.

        Returns:
            Tuple[bool, Optional[str]]: A tuple containing:
                - bool: True if the record should be skipped, False otherwise
                - Optional[str]: The reason for skipping, or None if not skipped
        """
        # Don't skip if buoy should be hauled - we need to process it even without recovery location
        if self._is_hauled_or_recovered(record):
            if not record.has_location:
                logger.info(
                    f"Processing hauled/recovered buoy {record.serialNumber} without recovery location "
                    f"(isDeleted={record.currentState.isDeleted}, "
                    f"isDeployed={record.currentState.isDeployed}, "
                    f"dateRecovered={record.currentState.dateRecovered}). "
                    f"Will use fallback location from deployed state or ER."
                )
            return False, None

        # For deployed buoys, location data is required
        if not record.has_location:
            return (
                True,
                f"Skipping buoy record with serial number {record.serialNumber} that has no location data "
                f"and is not in hauled/recovered state. "
                f"Last updated at {record.currentState.lastUpdated}. "
                f"(isDeleted={record.currentState.isDeleted}, isDeployed={record.currentState.isDeployed})",
            )

        return False, None

    def _filter_edgetech_buoys_data(self, data: List[Buoy]) -> List[Buoy]:
        """
        Filter buoy data records to include only processable records.

        This method keeps:
        - Deployed/active buoys with location data
        - Deleted/non-deployed buoys (for haul detection), even without recovery location

        Only skips buoys that are deployed but lack location data.

        Returns:
            List[Buoy]: A list of Buoy objects that can be processed.
        """
        filtered_data: List[Buoy] = []
        skipped_serial_numbers: Set[str] = set()

        for record in data:
            should_skip, skip_reason = self._should_skip_buoy(record)

            if should_skip:
                logger.warning(skip_reason)
                skipped_serial_numbers.add(record.serialNumber)
                continue

            filtered_data.append(record)

        return filtered_data

    def _get_latest_buoy_states(self, data: List[Buoy]) -> List[Buoy]:
        """
        Retrieve the latest buoy states from the parsed data.

        Returns:
            List[Buoy]: A list of Buoy objects representing the latest states.
        """
        latest: Dict[str, Buoy] = {}

        for record in data:
            key = f"{record.serialNumber}/{record.userId}"
            prev = latest.get(key)
            if (
                prev is None
                or record.currentState.lastUpdated > prev.currentState.lastUpdated
            ):
                latest[key] = record

        return list(latest.values())

    async def _identify_buoys(
        self,
        er_gears_devices_id_to_gear: Dict[str, BuoyGear],
        serial_number_to_edgetech_buoy: Dict[str, Buoy],
    ) -> Tuple[Set[str], Set[str], Set[str]]:
        """
        Determines which buoys need to be inserted (deployed), updated, or hauled.

        This method only processes buoys that appear in the current sync window from EdgeTech.
        It explicitly checks the buoy's status flags (isDeleted, isDeployed) to determine
        if a haul event should be generated, rather than inferring hauling from absence in the dataset.

        Process:
        - `to_deploy`: Buoys that are deployed in EdgeTech but not yet in ER, or re-deployments
          (EdgeTech dateDeployed newer than ER gear's deployment: haul existing then deploy new).
        - `to_update`: Buoys that exist in ER and have changes (location, status, or newer data).
        - `to_haul`: Buoys that are explicitly marked as deleted or not deployed in EdgeTech, or re-deployments
          (close existing gear before new deployment).

        Args:
            er_gears_devices_id_to_gear (Dict[str, BuoyGear]): Mapping of ER device IDs to gear objects.
            serial_number_to_edgetech_buoy (Dict[str, Buoy]): Mapping of serial numbers to EdgeTech buoy objects.

        Returns:
            Tuple[Set[str], Set[str], Set[str]]: Three sets containing:
                - `to_deploy`: Serial numbers of buoys to deploy.
                - `to_haul`: Serial numbers of buoys to haul.
                - `to_update`: Serial numbers of buoys to update.
        """
        to_deploy: Set[str] = set()
        to_haul: Set[str] = set()
        to_update: Set[str] = set()

        # Process only the buoys present in the current sync window
        for serial_number_user_id in serial_number_to_edgetech_buoy.keys():
            serial_number, hashed_user_id = serial_number_user_id.split("/", 2)
            primary_subject_name = f"{serial_number}_{hashed_user_id}_A"
            secondary_subject_name = f"{serial_number}_{hashed_user_id}_B"
            standard_subject_name = f"{serial_number}_{hashed_user_id}"

            edgetech_buoy = serial_number_to_edgetech_buoy[serial_number_user_id]

            # Check if gear exists in ER
            er_gear = (
                er_gears_devices_id_to_gear.get(primary_subject_name)
                or er_gears_devices_id_to_gear.get(standard_subject_name)
                or er_gears_devices_id_to_gear.get(secondary_subject_name)
            )

            if er_gear is None:
                # Gear doesn't exist in ER - check if it should be deployed
                if (
                    edgetech_buoy.currentState.isDeployed
                    and not edgetech_buoy.currentState.isDeleted
                ):
                    to_deploy.add(serial_number_user_id)
                    logger.info(
                        f"Buoy {serial_number_user_id} marked for deployment (not in ER, deployed in EdgeTech)"
                    )
                else:
                    logger.info(
                        f"Buoy {serial_number_user_id} skipped (not in ER, not deployed or deleted in EdgeTech)"
                    )
            else:
                # Gear exists in ER - determine if it needs update or haul

                # Check for explicit haul conditions from EdgeTech
                if (
                    edgetech_buoy.currentState.isDeleted
                    or not edgetech_buoy.currentState.isDeployed
                ):
                    # Buoy is explicitly marked as deleted or not deployed in EdgeTech
                    # Only mark for haul if ER still shows it as deployed
                    if er_gear.status == "deployed":
                        to_haul.add(serial_number_user_id)
                        logger.info(
                            f"Buoy {serial_number_user_id} marked for haul "
                            f"(isDeleted={edgetech_buoy.currentState.isDeleted}, "
                            f"isDeployed={edgetech_buoy.currentState.isDeployed})"
                        )
                    else:
                        logger.info(
                            f"Buoy {serial_number_user_id} already hauled in ER, skipping"
                        )
                elif (
                    er_gear.status != "deployed"
                    and edgetech_buoy.currentState.isDeployed
                ):
                    # ER gear was hauled (e.g. previous re-deployment haul succeeded
                    # but the deploy failed), and EdgeTech shows it as deployed now.
                    # Treat as a new deployment so the gear gets created in ER.
                    to_deploy.add(serial_number_user_id)
                    logger.info(
                        f"Buoy {serial_number_user_id} marked for deployment "
                        f"(ER gear status={er_gear.status} but EdgeTech isDeployed=True; "
                        f"recovering missed deployment)"
                    )
                else:
                    # Buoy is still deployed - check for re-deployment first (new deployment
                    # with same serials: close previous gear and create new one)
                    er_gear_deployment_dates = [
                        d.last_deployed
                        for d in er_gear.devices
                        if isinstance(getattr(d, "last_deployed", None), datetime)
                    ]
                    er_gear_deployment_date = (
                        min(er_gear_deployment_dates)
                        if er_gear_deployment_dates
                        else None
                    )
                    edgetech_date_deployed = edgetech_buoy.currentState.dateDeployed
                    # Re-deployment: EdgeTech dateDeployed is meaningfully later than ER's
                    # (e.g. same units deployed again). Use 1-minute threshold to avoid
                    # treating microsecond/timestamp precision differences as re-deploy.
                    is_redeployment = (
                        er_gear_deployment_date is not None
                        and edgetech_date_deployed is not None
                        and (edgetech_date_deployed - er_gear_deployment_date)
                        > timedelta(minutes=1)
                        and er_gear.status == "deployed"
                    )
                    if is_redeployment:
                        to_haul.add(serial_number_user_id)
                        to_deploy.add(serial_number_user_id)
                        logger.info(
                            f"Buoy {serial_number_user_id} marked for re-deployment "
                            f"(EdgeTech dateDeployed={edgetech_date_deployed} > ER gear deployment "
                            f"{er_gear_deployment_date}); will haul existing gear then deploy new."
                        )
                        continue

                    # Check if it needs updating (location or newer data)
                    edgetech_buoy_current_location = (
                        edgetech_buoy.currentState.latDeg,
                        edgetech_buoy.currentState.lonDeg,
                    )
                    er_gear_current_location = [
                        (device.location.latitude, device.location.longitude)
                        for device in er_gear.devices
                    ]
                    location_changed = (
                        edgetech_buoy_current_location not in er_gear_current_location
                    )

                    # Check if EdgeTech data is newer than ER data
                    edgetech_last_updated = edgetech_buoy.currentState.lastUpdated
                    er_last_updated = er_gear.last_updated
                    has_newer_data = edgetech_last_updated > er_last_updated

                    # No recorded_at dedup check needed here: position-only
                    # updates now use _utcnow() as recorded_at, which makes
                    # collisions with previously-accepted observations unlikely,
                    # though updates processed within the same second could still
                    # collide after _remove_milliseconds truncation.

                    if location_changed or has_newer_data:
                        target_key = serial_number_user_id
                        if edgetech_buoy.is_end_unit_record:
                            # End-unit record can't drive an update on its own
                            # (the update loop skips end-unit iterations), so
                            # redirect to its start record's key. The start's
                            # iteration diffs both devices' locations against ER
                            # and emits the gearset update if either moved.
                            candidate_start_key = f"{edgetech_buoy.currentState.startUnit}/{hashed_user_id}"
                            if candidate_start_key in serial_number_to_edgetech_buoy:
                                target_key = candidate_start_key
                            else:
                                logger.warning(
                                    "End-unit record %s flagged for update but its start "
                                    "record %s is not in the sync window; update skipped.",
                                    serial_number_user_id,
                                    candidate_start_key,
                                )
                                continue
                        to_update.add(target_key)
                        logger.info(
                            f"Buoy {serial_number_user_id} marked for update "
                            f"(location_changed={location_changed}, has_newer_data={has_newer_data}; "
                            f"target_key={target_key})"
                        )

        logger.info(f"Buoys to deploy: {len(to_deploy)} - {to_deploy}")
        logger.info(f"Buoys to haul: {len(to_haul)} - {to_haul}")
        logger.info(f"Buoys to update: {len(to_update)} - {to_update}")

        return to_deploy, to_haul, to_update

    def _get_circular_two_unit_start_keys_to_skip(
        self, serial_number_to_edgetech_buoy: Dict[str, Buoy]
    ) -> Set[str]:
        """
        Detect circular two-unit line configs: same two devices each configured as
        start unit with the other as end (e.g. A.endUnit=B and B.endUnit=A). That
        would create two gearsets for one physical pair. We keep one canonical
        start (lower serial number) and skip the other.

        Returns:
            Set of buoy keys (serialNumber/hashedUserId) to skip when processing
            deployments/updates for two-unit lines.
        """
        skip_keys: Set[str] = set()
        seen_pairs: Set[Tuple[str, str]] = set()  # (min_serial, max_serial) for dedup

        for buoy in serial_number_to_edgetech_buoy.values():
            if not buoy.currentState.isTwoUnitLine or not buoy.currentState.endUnit:
                continue
            if buoy.currentState.startUnit:
                continue  # This is an end-unit record, not a start
            partner_serial = buoy.currentState.endUnit
            hashed_user_id = get_hashed_user_id(buoy.userId)
            partner_key = f"{partner_serial}/{hashed_user_id}"
            partner = serial_number_to_edgetech_buoy.get(partner_key)
            if not partner or not partner.currentState.isTwoUnitLine:
                continue
            # Circular: partner also has endUnit pointing back to this buoy (so both are "start")
            if partner.currentState.endUnit != buoy.serialNumber:
                continue
            if partner.currentState.startUnit:
                continue  # Partner is end unit, not start; no conflict
            pair = tuple(sorted([buoy.serialNumber, partner_serial]))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            # Skip the one with the larger serial so we have a single canonical gearset
            skip_serial = max(buoy.serialNumber, partner_serial)
            skip_key = f"{skip_serial}/{hashed_user_id}"
            skip_keys.add(skip_key)
            logger.warning(
                "Circular two-unit line detected: devices %s and %s each configured as "
                "start with the other as end. Skipping duplicate start for %s (canonical lead: %s). "
                "Only one gearset will be created for this pair.",
                buoy.serialNumber,
                partner_serial,
                skip_serial,
                min(buoy.serialNumber, partner_serial),
            )
        return skip_keys

    async def process(self) -> List[Dict[str, Any]]:
        """
        Process buoy data to generate gear payloads for the Buoy API.

        This method processes only the buoys present in the current sync window from EdgeTech.
        It determines the appropriate action for each buoy based on its explicit status flags
        and comparison with the current state in EarthRanger.

        Processing steps:
            1. Retrieves and filters the latest buoy states grouped by serial number.
            2. Fetches existing ER gears and creates mappings for efficient lookups.
            3. Fetches all sources once for manufacturer ID to source ID mapping.
            4. Categorizes buoys based on explicit status checks:
                - Deploy: Buoys marked as deployed in EdgeTech but not yet in ER, or re-deployments.
                - Update: Buoys in ER with location changes or newer data from EdgeTech.
                - Haul: Buoys explicitly marked as deleted or not deployed in EdgeTech, or re-deployments
                  (close existing gear so a new deployment can be created).
            5. Creates gear payloads: hauls first (close old gear), then deployments, then updates.

        Important: Absence of a buoy from the sync window does NOT imply it was hauled.
        Haul events are only generated when EdgeTech explicitly marks a buoy as deleted
        or not deployed.

        Returns:
            List[dict]: A list of gear payloads ready to be sent to the Buoy API.
        """
        # Get the latest state for each buoy in the sync window
        # Note: self._data already contains only buoys updated within the sync window
        # as filtered by the EdgeTechClient based on start_datetime
        edgetech_deployed_buoys = self._get_latest_buoy_states(self._data)

        # Filter out buoys without location data (we keep deleted/non-deployed for haul detection)
        edgetech_deployed_buoys = self._filter_edgetech_buoys_data(
            edgetech_deployed_buoys
        )

        serial_number_to_edgetech_buoy = {
            f"{buoy.serialNumber}/{get_hashed_user_id(buoy.userId)}": buoy
            for buoy in edgetech_deployed_buoys
        }

        logger.info(
            f"Processing {len(serial_number_to_edgetech_buoy)} buoys from EdgeTech sync window"
        )

        # Detect circular two-unit configs (same pair each as start) and skip duplicate
        circular_two_unit_skip_keys = self._get_circular_two_unit_start_keys_to_skip(
            serial_number_to_edgetech_buoy
        )

        # Fetch all existing gears from ER to compare against EdgeTech data.
        # The /gear/ endpoint defaults to state=deployed, so hauled gears must
        # be requested explicitly — we need them both for the source-ID map
        # (re-deployments reuse source UUIDs that live on the previously-hauled
        # gear) and for the hauled-vs-deployed dedup below (line ~908).
        logger.info("Fetching all gears from EarthRanger (deployed + hauled)...")
        deployed_gears = await self._er_client.get_er_gears(
            params={"page_size": 10000}, state="deployed"
        )
        hauled_gears = await self._er_client.get_er_gears(
            params={"page_size": 10000}, state="hauled"
        )
        er_gears = deployed_gears + hauled_gears
        logger.info(
            f"Fetched {len(deployed_gears)} deployed and {len(hauled_gears)} "
            f"hauled gears from EarthRanger"
        )

        # Build mfr_device_id → source UUID map from gear devices across both
        # states. Avoids a separate /sources/ call that would require extra
        # permissions. If the same mfr_device_id appears on multiple gears
        # (e.g. hauled-then-redeployed), every occurrence maps to the same
        # underlying source UUID, so dict overwrite is fine.
        manufacturer_id_to_source_id = {
            device.mfr_device_id: device.device_id
            for gear in er_gears
            for device in gear.devices
        }
        logger.info(
            f"Derived {len(manufacturer_id_to_source_id)} source mappings from ER gears"
        )

        # Multiple ER gears can share a mfr_device_id (e.g. a hauled gear from a
        # prior lifecycle plus a currently-deployed gear). Prefer the deployed
        # gear; among same-status gears, prefer the most recently updated.
        # Without this, a leftover hauled gear could win the lookup and trick
        # _identify_buoys into creating a duplicate deployment.
        er_gears_devices_id_to_gear: Dict[str, BuoyGear] = {}
        for gear in er_gears:
            for device in gear.devices:
                existing = er_gears_devices_id_to_gear.get(device.mfr_device_id)
                if existing is None:
                    er_gears_devices_id_to_gear[device.mfr_device_id] = gear
                    continue
                existing_deployed = existing.status == "deployed"
                gear_deployed = gear.status == "deployed"
                if existing_deployed and not gear_deployed:
                    continue
                if gear_deployed and not existing_deployed:
                    er_gears_devices_id_to_gear[device.mfr_device_id] = gear
                    continue
                if gear.last_updated > existing.last_updated:
                    er_gears_devices_id_to_gear[device.mfr_device_id] = gear

        to_deploy, to_haul, to_update = await self._identify_buoys(
            er_gears_devices_id_to_gear,
            serial_number_to_edgetech_buoy,
        )

        gear_payloads = []

        # Process hauls first (so re-deployments close old gear before creating new)
        haul_gears_processed = set()

        for serial_number_user_id in to_haul:
            serial_number, hashed_user_id = serial_number_user_id.split("/", 2)
            primary_device_name = f"{serial_number}_{hashed_user_id}_A"
            single_device_name = f"{serial_number}_{hashed_user_id}"

            # Get the EdgeTech buoy data for potential recovery location
            edgetech_buoy = serial_number_to_edgetech_buoy.get(serial_number_user_id)

            # Find the corresponding ER gear
            er_gear = er_gears_devices_id_to_gear.get(
                primary_device_name
            ) or er_gears_devices_id_to_gear.get(single_device_name)

            if not er_gear:
                logger.warning(
                    "No ER gear found for buoy %s (tried %s and %s), skipping haul.",
                    serial_number_user_id,
                    primary_device_name,
                    single_device_name,
                )
                continue

            # Skip if we already processed this gear set
            if er_gear.display_id in haul_gears_processed:
                logger.info(
                    f"Gear set {er_gear.display_id} already processed for haul, "
                    f"skipping buoy {serial_number_user_id}"
                )
                continue

            try:
                payload = self._create_haul_payload(
                    er_gear=er_gear,
                    edgetech_buoy=edgetech_buoy,
                    is_redeployment=serial_number_user_id in to_deploy,
                )
                gear_payloads.append(payload)
                haul_gears_processed.add(er_gear.display_id)
                logger.info(
                    f"Created haul payload for gear set {er_gear.display_id} "
                    f"(buoy {serial_number_user_id})"
                )

            except Exception as e:
                logger.exception(
                    "Failed to create haul payload for gear set %s (buoy %s). Error: %s",
                    er_gear.display_id if er_gear else "unknown",
                    serial_number_user_id,
                    str(e),
                )

        # Process deployments (new gear sets)
        for serial_number_user_id in to_deploy:
            edgetech_buoy = serial_number_to_edgetech_buoy[serial_number_user_id]

            if edgetech_buoy.is_end_unit_record:
                # Partner record of a two-unit gearset; the start unit's iteration
                # builds the full payload using this record's location.
                continue

            try:
                # Get end unit buoy if this is a two-unit line
                end_unit_buoy = None
                end_unit_device_from_er = None
                if (
                    edgetech_buoy.currentState.isTwoUnitLine
                    and edgetech_buoy.currentState.endUnit
                ):
                    end_unit_buoy_key = f"{edgetech_buoy.currentState.endUnit}/{get_hashed_user_id(edgetech_buoy.userId)}"
                    end_unit_buoy = serial_number_to_edgetech_buoy.get(
                        end_unit_buoy_key
                    )

                    if not end_unit_buoy:
                        # End unit not in sync window. Try to get it from ER
                        # (covers re-deployments where haul already happened,
                        # and recovery deployments where ER gear was hauled).
                        end_unit_mfr_id = (
                            f"{edgetech_buoy.currentState.endUnit}"
                            f"_{get_hashed_user_id(edgetech_buoy.userId)}"
                        )
                        er_gear = er_gears_devices_id_to_gear.get(
                            f"{serial_number_user_id.replace('/', '_')}_A"
                        ) or er_gears_devices_id_to_gear.get(
                            serial_number_user_id.replace("/", "_")
                        )
                        if er_gear:
                            for er_device in er_gear.devices:
                                if er_device.mfr_device_id == end_unit_mfr_id:
                                    end_unit_device_from_er = er_device
                                    break
                        if end_unit_device_from_er:
                            logger.info(
                                "End unit %s not in sync window; "
                                "using ER state for deployment of %s",
                                edgetech_buoy.currentState.endUnit,
                                serial_number_user_id,
                            )
                        else:
                            logger.warning(
                                "End unit buoy %s not found for %s, "
                                "skipping deployment.",
                                edgetech_buoy.currentState.endUnit,
                                serial_number_user_id,
                            )
                            continue

                if serial_number_user_id in circular_two_unit_skip_keys:
                    logger.warning(
                        "Skipping deployment for %s (circular two-unit duplicate).",
                        serial_number_user_id,
                    )
                    continue

                payload = await self._create_gear_payload(
                    buoy=edgetech_buoy,
                    device_status="deployed",
                    manufacturer_id_to_source_id=manufacturer_id_to_source_id,
                    end_unit_buoy=end_unit_buoy,
                    end_unit_device_from_er=(
                        end_unit_device_from_er if not end_unit_buoy else None
                    ),
                    include_initial_deployment=True,
                )
                gear_payloads.append(payload)
                logger.info(f"Created deployment payload for {serial_number_user_id}")

            except Exception as e:
                logger.exception(
                    "Failed to create gear payload for deployment %s. Error: %s",
                    serial_number_user_id,
                    str(e),
                )

        # Process updates (existing gear sets with location changes)
        for serial_number_user_id in to_update:
            edgetech_buoy = serial_number_to_edgetech_buoy[serial_number_user_id]

            if edgetech_buoy.is_end_unit_record:
                # Partner record of a two-unit gearset; the start unit's iteration
                # builds the full update payload.
                continue

            primary_device_name = f"{serial_number_user_id.replace('/', '_')}_A"
            single_device_name = f"{serial_number_user_id.replace('/', '_')}"
            er_gear = er_gears_devices_id_to_gear.get(
                primary_device_name
            ) or er_gears_devices_id_to_gear.get(single_device_name)

            if not er_gear:
                logger.warning(
                    f"ER gear not found for {serial_number_user_id}, skipping update."
                )
                continue

            try:
                hashed_user_id = get_hashed_user_id(edgetech_buoy.userId)
                end_unit_buoy = None
                end_unit_device_from_er = None
                if edgetech_buoy.currentState.isTwoUnitLine:
                    if edgetech_buoy.currentState.endUnit:
                        end_unit_buoy_key = (
                            f"{edgetech_buoy.currentState.endUnit}/{hashed_user_id}"
                        )
                        end_unit_buoy = serial_number_to_edgetech_buoy.get(
                            end_unit_buoy_key
                        )

                        if not end_unit_buoy:
                            # End unit not in sync window (e.g. only start unit had location update).
                            # Use end unit's current state from ER so we can still push the update.
                            # Match by mfr_device_id: same format as when we create the payload.
                            end_unit_mfr_id = (
                                f"{edgetech_buoy.currentState.endUnit}_{hashed_user_id}"
                            )
                            for er_device in er_gear.devices:
                                if er_device.mfr_device_id == end_unit_mfr_id:
                                    end_unit_device_from_er = er_device
                                    break
                            if not end_unit_device_from_er:
                                logger.warning(
                                    "End unit buoy %s not found and no end unit device in ER gear for %s, skipping update.",
                                    edgetech_buoy.currentState.endUnit,
                                    serial_number_user_id,
                                )
                                continue
                            logger.info(
                                "End unit %s not in sync window; using current state from ER for update of %s",
                                edgetech_buoy.currentState.endUnit,
                                serial_number_user_id,
                            )

                # Diff every device in the gearset, not just the primary —
                # single-unit-trawl Device B (endLatDeg/endLonDeg) and
                # two-unit end records can move while the start holds.
                expected_locations = self._expected_device_locations(
                    edgetech_buoy,
                    end_unit_buoy,
                    end_unit_device_from_er,
                    hashed_user_id,
                )
                if not self._gearset_location_changed(er_gear, expected_locations):
                    logger.info(
                        "No change in location for buoy %s (no device moved), skipping update.",
                        serial_number_user_id,
                    )
                    continue

                if serial_number_user_id in circular_two_unit_skip_keys:
                    logger.warning(
                        "Skipping update for %s (circular two-unit duplicate).",
                        serial_number_user_id,
                    )
                    continue

                payload = await self._create_gear_payload(
                    buoy=edgetech_buoy,
                    device_status="deployed",
                    manufacturer_id_to_source_id=manufacturer_id_to_source_id,
                    end_unit_buoy=end_unit_buoy,
                    end_unit_device_from_er=end_unit_device_from_er,
                    set_id=er_gear.id,
                    include_initial_deployment=False,
                )
                gear_payloads.append(payload)
                logger.info(f"Created update payload for {serial_number_user_id}")

            except Exception as e:
                logger.exception(
                    "Failed to create gear payload for update %s. Error: %s",
                    serial_number_user_id,
                    str(e),
                )

        logger.info(
            "Generated %d gear payload(s):\n%s",
            len(gear_payloads),
            json.dumps(gear_payloads, indent=4, default=str),
        )

        return gear_payloads
